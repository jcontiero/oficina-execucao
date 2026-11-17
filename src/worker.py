from datetime import datetime, timedelta, timezone
import json
import logging
import os
import time
from typing import Any

from google.cloud import pubsub_v1

from src.config import Configuracoes
from src.execucao.infraestrutura.firestore_store import (
    DocumentoStore,
    GoogleFirestoreStore,
    InMemoryFirestoreStore,
)
from src.mensageria.consumidor import ExecucaoConsumidor
from src.shared.observabilidade import configurar_observabilidade

logger = logging.getLogger("oficina-execucao-worker")


def publicar_lote_outbox(
    store: DocumentoStore,
    publisher: Any,
    *,
    project_id: str,
    lease_seconds: int = 30,
    agora: datetime | None = None,
) -> int:
    ref = agora or datetime.now(timezone.utc)
    ref_iso = ref.isoformat()
    docs = store.listar("outbox")
    elegiveis = [
        d
        for d in docs
        if d.get("status") in {"PENDENTE", "FALHA_AGENDADA"}
        and str(d.get("next_attempt_at", "")) <= ref_iso
        and (not d.get("locked_until") or str(d.get("locked_until")) <= ref_iso)
    ]
    if not elegiveis:
        return 0

    lease_expira = (ref + timedelta(seconds=lease_seconds)).isoformat()
    for d in elegiveis:
        d["locked_until"] = lease_expira
        store.salvar("outbox", str(d["id"]), d)

    publicados = 0
    for d in elegiveis:
        try:
            topic_path = publisher.topic_path(project_id, str(d["topic"]))
            envelope = d["envelope"]
            data = json.dumps(envelope).encode("utf-8")
            attrs = {
                "event_type": str(d["tipo_evento"]),
                "producer": "oficina-execucao",
                "schema_version": "1",
            }
            if d.get("traceparent"):
                attrs["traceparent"] = str(d["traceparent"])
            publisher.publish(topic_path, data, **attrs).result(timeout=10)
            d["status"] = "PROCESSADO"
            d["processado_em"] = datetime.now(timezone.utc).isoformat()
            d["locked_until"] = None
            d["erro"] = None
            publicados += 1
        except Exception as exc:  # noqa: BLE001
            tentativas = int(d.get("tentativas", 0)) + 1
            atraso = min(60, 2 ** min(tentativas, 5))
            d["tentativas"] = tentativas
            d["status"] = "FALHA_AGENDADA"
            d["next_attempt_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=atraso)
            ).isoformat()
            d["locked_until"] = None
            d["erro"] = str(exc)
            logger.error("Erro ao publicar outbox execucao %s: %s", d["id"], exc)
        store.salvar("outbox", str(d["id"]), d)
    return publicados


def consumir_comandos_pull(
    store: DocumentoStore,
    subscriber: Any,
    *,
    project_id: str,
    subscription_name: str,
    environment: str = "homolog",
    max_messages: int = 20,
) -> int:
    sub_path = subscriber.subscription_path(project_id, subscription_name)
    resp = subscriber.pull(
        request={"subscription": sub_path, "max_messages": max_messages},
        timeout=5,
    )
    if not resp.received_messages:
        return 0

    cons = ExecucaoConsumidor(store, environment=environment)
    processadas = 0
    for rec in resp.received_messages:
        ack_id = rec.ack_id
        try:
            dados = json.loads(rec.message.data.decode("utf-8"))
            traceparent = (rec.message.attributes or {}).get("traceparent")
            cons.processar_comando(dados, traceparent=traceparent)
            subscriber.acknowledge(
                request={"subscription": sub_path, "ack_ids": [ack_id]}
            )
            processadas += 1
        except Exception:  # noqa: BLE001
            logger.exception("Erro ao processar comando na Execução")
            try:
                subscriber.modify_ack_deadline(
                    request={
                        "subscription": sub_path,
                        "ack_ids": [ack_id],
                        "ack_deadline_seconds": 0,
                    }
                )
            except Exception:  # noqa: BLE001
                pass
    return processadas


def main() -> None:
    cfg = Configuracoes()
    configurar_observabilidade(
        service="oficina-execucao-worker",
        env=cfg.dd_env,
        version=cfg.dd_version,
        trace_enabled=cfg.dd_trace_enabled,
    )
    store: DocumentoStore = (
        InMemoryFirestoreStore()
        if cfg.use_in_memory_store
        else GoogleFirestoreStore(cfg)
    )
    project_id = cfg.gcp_project_id
    env = cfg.environment
    sub_name = os.getenv(
        "EXECUCAO_COMANDOS_SUB", f"oficina-{env}-execucao-comandos-sub"
    )
    publisher = pubsub_v1.PublisherClient()
    subscriber = pubsub_v1.SubscriberClient()

    logger.info("Worker Execução iniciado (env=%s, sub=%s)", env, sub_name)
    while True:
        try:
            publicar_lote_outbox(store, publisher, project_id=project_id)
            consumir_comandos_pull(
                store,
                subscriber,
                project_id=project_id,
                subscription_name=sub_name,
                environment=env,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Erro no loop do worker Execução: %s", exc)
        time.sleep(0.5)


if __name__ == "__main__":
    main()
