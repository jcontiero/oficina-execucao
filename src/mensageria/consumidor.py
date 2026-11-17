from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from src.execucao.dominio.entidades import (
    ControleSagaExecucao,
    EstadoControleSaga,
    ReservaEstoque,
    StatusExecucao,
    StatusReserva,
)
from src.execucao.infraestrutura.firestore_store import (
    DocumentoStore,
    TransacaoContexto,
)
from src.mensageria.contratos import validar_comando_execucao_v1


def _controle_from_dict(dados: dict[str, Any]) -> ControleSagaExecucao:
    return ControleSagaExecucao(
        saga_id=UUID(str(dados["saga_id"])),
        os_id=UUID(str(dados["os_id"])),
        rodada=int(dados["rodada"]),
        estado_controle=EstadoControleSaga(str(dados["estado_controle"])),
        cancelamento_event_id=(
            UUID(str(dados["cancelamento_event_id"]))
            if dados.get("cancelamento_event_id")
            else None
        ),
        motivo_cancelamento=dados.get("motivo_cancelamento"),
        version=int(dados.get("version", 1)),
    )


def _controle_to_dict(controle: ControleSagaExecucao) -> dict[str, Any]:
    return {
        "saga_id": str(controle.saga_id),
        "os_id": str(controle.os_id),
        "rodada": controle.rodada,
        "estado_controle": controle.estado_controle.value,
        "cancelamento_event_id": (
            str(controle.cancelamento_event_id)
            if controle.cancelamento_event_id
            else None
        ),
        "motivo_cancelamento": controle.motivo_cancelamento,
        "version": controle.version,
    }


def montar_doc_outbox(
    *,
    environment: str,
    event_type: str,
    saga_id: UUID,
    os_id: UUID,
    correlation_id: UUID,
    causation_id: UUID,
    aggregate_version: int,
    payload: dict[str, Any],
    traceparent: str | None = None,
) -> dict[str, Any]:
    event_id = str(uuid4())
    agora = datetime.now(timezone.utc).isoformat()
    envelope = {
        "event_id": event_id,
        "event_type": event_type,
        "schema_version": 1,
        "occurred_at": agora,
        "producer": "oficina-execucao",
        "saga_id": str(saga_id),
        "os_id": str(os_id),
        "correlation_id": str(correlation_id),
        "causation_id": str(causation_id),
        "aggregate_version": aggregate_version,
        "payload": payload,
    }
    return {
        "id": event_id,
        "event_id": event_id,
        "topic": f"oficina-{environment}-eventos-v1",
        "tipo_evento": event_type,
        "envelope": envelope,
        "traceparent": traceparent,
        "status": "PENDENTE",
        "tentativas": 0,
        "next_attempt_at": agora,
        "locked_until": None,
        "criado_em": agora,
    }


class ExecucaoConsumidor:
    """
    Consumidor transacional de comandos da Saga para o serviço de Execução (Firestore).
    Inbox, efeito em controle_saga/pecas/reservas/execucoes e gravação na outbox
    acontecem na mesma transação Firestore sem chamadas externas dentro dela.
    """

    def __init__(self, store: DocumentoStore, *, environment: str = "homolog") -> None:
        self.store = store
        self.environment = environment

    def processar_comando(
        self,
        dados: dict[str, Any],
        *,
        consumer: str = "execucao-comandos",
        traceparent: str | None = None,
    ) -> str:
        envelope = validar_comando_execucao_v1(dados)
        inbox_id = f"{consumer}_{envelope.event_id}"

        def _executar(tx: TransacaoContexto) -> str:
            if tx.buscar("inbox", inbox_id) is not None:
                return "DUPLICADO"

            p = envelope.payload
            evt = envelope.event_type
            saga_key = str(envelope.saga_id)
            agora = datetime.now(timezone.utc).isoformat()

            tx.salvar(
                "inbox",
                inbox_id,
                {
                    "id": inbox_id,
                    "consumer": consumer,
                    "event_id": str(envelope.event_id),
                    "event_type": evt,
                    "saga_id": saga_key,
                    "recebido_em": agora,
                },
            )

            controle_doc = tx.buscar("controle_saga", saga_key)
            controle = (
                _controle_from_dict(controle_doc)
                if controle_doc is not None
                else ControleSagaExecucao(
                    saga_id=envelope.saga_id,
                    os_id=envelope.os_id,
                    rodada=int(p["rodada"]),
                )
            )

            if evt == "SolicitarDiagnostico.v1":
                if controle_doc is None:
                    tx.salvar("controle_saga", saga_key, _controle_to_dict(controle))
                diag_id = f"{envelope.os_id}_{p['rodada']}"
                if tx.buscar("diagnosticos", diag_id) is None:
                    tx.salvar(
                        "diagnosticos",
                        diag_id,
                        {
                            "id": diag_id,
                            "os_id": str(envelope.os_id),
                            "saga_id": saga_key,
                            "rodada": int(p["rodada"]),
                            "descricao_problema": p["descricao_problema"],
                            "status": "SOLICITADO",
                            "criado_em": agora,
                        },
                    )
                return "PROCESSADO"

            if evt == "ReservarPecas.v1":
                if controle.estado_controle == EstadoControleSaga.CANCELADO_TOMBSTONE:
                    out = montar_doc_outbox(
                        environment=self.environment,
                        event_type="ReservaRecusada.v1",
                        saga_id=envelope.saga_id,
                        os_id=envelope.os_id,
                        correlation_id=envelope.correlation_id,
                        causation_id=envelope.event_id,
                        aggregate_version=controle.version,
                        payload={
                            "rodada": int(p["rodada"]),
                            "reserva_id": str(p["reserva_id"]),
                            "motivo": "SAGA_CANCELADA_TOMBSTONE",
                            "pecas_indisponiveis": [],
                        },
                        traceparent=traceparent,
                    )
                    tx.salvar("outbox", out["id"], out)
                    return "PROCESSADO"

                reserva_existente = tx.buscar("reservas", saga_key)
                if reserva_existente is None:
                    itens_peca = list(p.get("itens_peca", []))
                    pecas_carregadas: list[tuple[dict[str, Any], int]] = []
                    indisponiveis: list[dict[str, Any]] = []

                    for item in itens_peca:
                        peca_id = str(item["peca_id"])
                        qtd = int(item["quantidade"])
                        peca_doc = tx.buscar("pecas", peca_id)
                        disponivel = (
                            int(peca_doc["quantidade_disponivel"])
                            if peca_doc is not None
                            else 0
                        )
                        if peca_doc is None or disponivel < qtd:
                            indisponiveis.append(
                                {
                                    "peca_id": peca_id,
                                    "solicitado": qtd,
                                    "disponivel": disponivel,
                                }
                            )
                        else:
                            pecas_carregadas.append((peca_doc, qtd))

                    if indisponiveis:
                        out = montar_doc_outbox(
                            environment=self.environment,
                            event_type="ReservaRecusada.v1",
                            saga_id=envelope.saga_id,
                            os_id=envelope.os_id,
                            correlation_id=envelope.correlation_id,
                            causation_id=envelope.event_id,
                            aggregate_version=controle.version,
                            payload={
                                "rodada": int(p["rodada"]),
                                "reserva_id": str(p["reserva_id"]),
                                "motivo": "ESTOQUE_INSUFICIENTE",
                                "pecas_indisponiveis": indisponiveis,
                            },
                            traceparent=traceparent,
                        )
                        tx.salvar("outbox", out["id"], out)
                        return "PROCESSADO"

                    for peca_doc, qtd in pecas_carregadas:
                        peca_doc["quantidade_disponivel"] = (
                            int(peca_doc["quantidade_disponivel"]) - qtd
                        )
                        tx.salvar("pecas", str(peca_doc["id"]), peca_doc)

                    status_res = "RESERVADA" if itens_peca else "RESERVA_LOGICA_VAZIA"
                    tx.salvar(
                        "reservas",
                        saga_key,
                        {
                            "saga_id": saga_key,
                            "os_id": str(envelope.os_id),
                            "rodada": int(p["rodada"]),
                            "status": StatusReserva.ATIVA.value,
                            "itens": itens_peca,
                            "criada_em": agora,
                        },
                    )
                else:
                    status_res = (
                        "RESERVADA"
                        if reserva_existente.get("itens")
                        else "RESERVA_LOGICA_VAZIA"
                    )

                out = montar_doc_outbox(
                    environment=self.environment,
                    event_type="PecasReservadas.v1",
                    saga_id=envelope.saga_id,
                    os_id=envelope.os_id,
                    correlation_id=envelope.correlation_id,
                    causation_id=envelope.event_id,
                    aggregate_version=controle.version,
                    payload={
                        "rodada": int(p["rodada"]),
                        "reserva_id": str(p["reserva_id"]),
                        "status": status_res,
                    },
                    traceparent=traceparent,
                )
                tx.salvar("outbox", out["id"], out)
                return "PROCESSADO"

            if evt == "EnfileirarExecucao.v1":
                if controle.estado_controle == EstadoControleSaga.CANCELADO_TOMBSTONE:
                    out = montar_doc_outbox(
                        environment=self.environment,
                        event_type="ExecucaoRejeitada.v1",
                        saga_id=envelope.saga_id,
                        os_id=envelope.os_id,
                        correlation_id=envelope.correlation_id,
                        causation_id=envelope.event_id,
                        aggregate_version=controle.version,
                        payload={
                            "rodada": int(p["rodada"]),
                            "reserva_id": str(p["reserva_id"]),
                            "motivo": "SAGA_CANCELADA_TOMBSTONE",
                        },
                        traceparent=traceparent,
                    )
                    tx.salvar("outbox", out["id"], out)
                    return "PROCESSADO"

                exec_id = f"{envelope.os_id}_{p['rodada']}"
                exec_doc = tx.buscar("execucoes", exec_id)
                if exec_doc is None:
                    tx.salvar(
                        "execucoes",
                        exec_id,
                        {
                            "id": exec_id,
                            "os_id": str(envelope.os_id),
                            "saga_id": saga_key,
                            "rodada": int(p["rodada"]),
                            "pagamento_id": str(p["pagamento_id"]),
                            "itens_servico": p["itens_servico"],
                            "itens_peca": p["itens_peca"],
                            "status": StatusExecucao.AGUARDANDO_EXECUCAO.value,
                            "enfileirada_em": agora,
                        },
                    )
                else:
                    exec_doc["pagamento_id"] = str(p["pagamento_id"])
                    exec_doc["status"] = StatusExecucao.AGUARDANDO_EXECUCAO.value
                    tx.salvar("execucoes", exec_id, exec_doc)

                controle.estado_controle = EstadoControleSaga.EM_FILA
                controle.version += 1
                tx.salvar("controle_saga", saga_key, _controle_to_dict(controle))

                out = montar_doc_outbox(
                    environment=self.environment,
                    event_type="ExecucaoEnfileirada.v1",
                    saga_id=envelope.saga_id,
                    os_id=envelope.os_id,
                    correlation_id=envelope.correlation_id,
                    causation_id=envelope.event_id,
                    aggregate_version=controle.version,
                    payload={
                        "rodada": int(p["rodada"]),
                        "execucao_id": exec_id,
                        "reserva_id": str(p["reserva_id"]),
                        "status": StatusExecucao.AGUARDANDO_EXECUCAO.value,
                    },
                    traceparent=traceparent,
                )
                tx.salvar("outbox", out["id"], out)
                return "PROCESSADO"

            if evt == "CancelarExecucao.v1":
                res = controle.cancelar_com_tombstone(
                    cancelamento_event_id=envelope.event_id,
                    motivo=str(p["motivo"]),
                )
                tx.salvar("controle_saga", saga_key, _controle_to_dict(controle))
                if res["event_type"] == "ExecucaoCancelada.v1":
                    payload_resp = {
                        "rodada": int(p["rodada"]),
                        "compensacao_id": str(p["compensacao_id"]),
                        "resultado": "CANCELADA",
                        "motivo": str(p["motivo"]),
                        "execucao_id": p.get("execucao_id"),
                        "tombstone_persistido": True,
                    }
                else:
                    payload_resp = {
                        "rodada": int(p["rodada"]),
                        "compensacao_id": str(p["compensacao_id"]),
                        "resultado": "RECUSADO",
                        "motivo": "REPARO_INICIADO",
                        "execucao_id": f"{envelope.os_id}_{p['rodada']}",
                    }
                out = montar_doc_outbox(
                    environment=self.environment,
                    event_type=res["event_type"],
                    saga_id=envelope.saga_id,
                    os_id=envelope.os_id,
                    correlation_id=envelope.correlation_id,
                    causation_id=envelope.event_id,
                    aggregate_version=controle.version,
                    payload=payload_resp,
                    traceparent=traceparent,
                )
                tx.salvar("outbox", out["id"], out)
                return "PROCESSADO"

            if evt == "LiberarReserva.v1":
                barreira_id = UUID(str(p["execucao_cancelada_event_id"]))
                reserva_doc = tx.buscar("reservas", saga_key)
                if reserva_doc is None:
                    if (
                        controle.estado_controle
                        != EstadoControleSaga.CANCELADO_TOMBSTONE
                    ):
                        raise ValueError(
                            "Barreira violada: controle_saga não cancelado"
                        )
                    out = montar_doc_outbox(
                        environment=self.environment,
                        event_type="ReservaLiberada.v1",
                        saga_id=envelope.saga_id,
                        os_id=envelope.os_id,
                        correlation_id=envelope.correlation_id,
                        causation_id=envelope.event_id,
                        aggregate_version=controle.version,
                        payload={
                            "rodada": int(p["rodada"]),
                            "compensacao_id": str(p["compensacao_id"]),
                            "reserva_id": str(p["reserva_id"]),
                            "resultado": "NAO_EXISTIA",
                            "motivo": str(p["motivo"]),
                        },
                        traceparent=traceparent,
                    )
                    tx.salvar("outbox", out["id"], out)
                    return "PROCESSADO"

                reserva = ReservaEstoque(
                    saga_id=envelope.saga_id,
                    os_id=envelope.os_id,
                    rodada=int(p["rodada"]),
                    itens=list(reserva_doc.get("itens", [])),
                    status=StatusReserva(str(reserva_doc["status"])),
                )
                res = reserva.liberar_apos_barreira(
                    controle=controle,
                    execucao_cancelada_event_id=barreira_id,
                )
                if res["event_type"] == "ReservaLiberada.v1":
                    for item in reserva.itens:
                        peca_doc = tx.buscar("pecas", str(item["peca_id"]))
                        if peca_doc is not None:
                            peca_doc["quantidade_disponivel"] = int(
                                peca_doc["quantidade_disponivel"]
                            ) + int(item["quantidade"])
                            tx.salvar("pecas", str(peca_doc["id"]), peca_doc)
                    reserva_doc["status"] = StatusReserva.LIBERADA.value
                    tx.salvar("reservas", saga_key, reserva_doc)

                out = montar_doc_outbox(
                    environment=self.environment,
                    event_type=res["event_type"],
                    saga_id=envelope.saga_id,
                    os_id=envelope.os_id,
                    correlation_id=envelope.correlation_id,
                    causation_id=envelope.event_id,
                    aggregate_version=controle.version,
                    payload={
                        "rodada": int(p["rodada"]),
                        "compensacao_id": str(p["compensacao_id"]),
                        "reserva_id": str(p["reserva_id"]),
                        "resultado": res["resultado"],
                        "motivo": res["motivo"],
                    },
                    traceparent=traceparent,
                )
                tx.salvar("outbox", out["id"], out)
                return "PROCESSADO"

            return "PROCESSADO"

        return self.store.executar_em_transacao(_executar)
