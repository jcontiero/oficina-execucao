from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from src.execucao.dominio.entidades import EstadoControleSaga, StatusReserva
from src.execucao.infraestrutura.firestore_store import (
    ExecucaoRepositorioFirestore,
    InMemoryFirestoreStore,
)
from src.mensageria.consumidor import ExecucaoConsumidor
from src.worker import consumir_comandos_pull, publicar_lote_outbox


def _cmd(
    *,
    event_type: str,
    saga_id,
    os_id,
    payload: dict,
    event_id=None,
) -> dict:
    return {
        "event_id": str(event_id or uuid4()),
        "event_type": event_type,
        "schema_version": 1,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "producer": "oficina-os",
        "saga_id": str(saga_id),
        "os_id": str(os_id),
        "correlation_id": str(saga_id),
        "causation_id": str(saga_id),
        "aggregate_version": 1,
        "payload": payload,
    }


def test_reserva_atomica_sem_parcialidade_deduplicacao_e_liberacao():
    store = InMemoryFirestoreStore()
    peca1_id = str(uuid4())
    peca2_id = str(uuid4())
    store.salvar(
        "pecas",
        peca1_id,
        {
            "id": peca1_id,
            "nome": "Pastilha",
            "codigo": "P1",
            "preco_unitario": "100.00",
            "quantidade_disponivel": 5,
            "quantidade_minima_alerta": 1,
        },
    )
    store.salvar(
        "pecas",
        peca2_id,
        {
            "id": peca2_id,
            "nome": "Disco",
            "codigo": "P2",
            "preco_unitario": "200.00",
            "quantidade_disponivel": 1,
            "quantidade_minima_alerta": 1,
        },
    )

    cons = ExecucaoConsumidor(store, environment="homolog")
    saga_falha = uuid4()
    os_id = uuid4()

    # Caso 1: peca2 não tem 3 unidades -> NENHUMA peça pode ser debitada (nem peca1!)
    cmd_recusa = _cmd(
        event_type="ReservarPecas.v1",
        saga_id=saga_falha,
        os_id=os_id,
        payload={
            "rodada": 1,
            "reserva_id": str(saga_falha),
            "orcamento_id": str(uuid4()),
            "orcamento_versao": 1,
            "itens_peca": [
                {
                    "peca_id": peca1_id,
                    "descricao": "Pastilha",
                    "quantidade": 2,
                    "preco_unitario_centavos": 10000,
                },
                {
                    "peca_id": peca2_id,
                    "descricao": "Disco",
                    "quantidade": 3,
                    "preco_unitario_centavos": 20000,
                },
            ],
        },
    )
    assert cons.processar_comando(cmd_recusa) == "PROCESSADO"
    assert store.buscar("pecas", peca1_id)["quantidade_disponivel"] == 5
    assert store.buscar("pecas", peca2_id)["quantidade_disponivel"] == 1
    out_rec = [
        d for d in store.listar("outbox") if d["tipo_evento"] == "ReservaRecusada.v1"
    ]
    assert len(out_rec) == 1

    # Caso 2: Reserva válida + reentrega (inbox deduplica) + CancelarExecucao -> LiberarReserva
    saga_ok = uuid4()
    cmd_ok = _cmd(
        event_type="ReservarPecas.v1",
        saga_id=saga_ok,
        os_id=os_id,
        payload={
            "rodada": 1,
            "reserva_id": str(saga_ok),
            "orcamento_id": str(uuid4()),
            "orcamento_versao": 1,
            "itens_peca": [
                {
                    "peca_id": peca1_id,
                    "descricao": "Pastilha",
                    "quantidade": 2,
                    "preco_unitario_centavos": 10000,
                }
            ],
        },
    )
    assert cons.processar_comando(cmd_ok) == "PROCESSADO"
    assert cons.processar_comando(cmd_ok) == "DUPLICADO"
    assert store.buscar("pecas", peca1_id)["quantidade_disponivel"] == 3
    assert store.buscar("reservas", str(saga_ok))["status"] == StatusReserva.ATIVA.value

    # Barreira de cancelamento
    cancel_evt_id = uuid4()
    cmd_cancel = _cmd(
        event_id=cancel_evt_id,
        event_type="CancelarExecucao.v1",
        saga_id=saga_ok,
        os_id=os_id,
        payload={
            "rodada": 1,
            "compensacao_id": str(uuid4()),
            "motivo": "FALHA_ENFILEIRAMENTO",
            "execucao_id": None,
        },
    )
    assert cons.processar_comando(cmd_cancel) == "PROCESSADO"
    repo = ExecucaoRepositorioFirestore(store)
    ctrl = repo.buscar_controle(saga_ok)
    assert ctrl is not None
    assert ctrl.estado_controle == EstadoControleSaga.CANCELADO_TOMBSTONE

    # Liberação após barreira devolve as 2 unidades para peca1
    cmd_liberar = _cmd(
        event_type="LiberarReserva.v1",
        saga_id=saga_ok,
        os_id=os_id,
        payload={
            "rodada": 1,
            "compensacao_id": str(uuid4()),
            "motivo": "FALHA_ENFILEIRAMENTO",
            "reserva_id": str(saga_ok),
            "execucao_cancelada_event_id": str(ctrl.cancelamento_event_id),
        },
    )
    assert cons.processar_comando(cmd_liberar) == "PROCESSADO"
    assert store.buscar("pecas", peca1_id)["quantidade_disponivel"] == 5
    assert (
        store.buscar("reservas", str(saga_ok))["status"] == StatusReserva.LIBERADA.value
    )


def test_execucao_worker_janela_publish_e_ack():
    store = InMemoryFirestoreStore()
    saga_id = uuid4()
    os_id = uuid4()
    cmd_diag = _cmd(
        event_type="SolicitarDiagnostico.v1",
        saga_id=saga_id,
        os_id=os_id,
        payload={
            "rodada": 1,
            "cliente_id": str(uuid4()),
            "veiculo_id": str(uuid4()),
            "descricao_problema": "Freio",
        },
    )
    msg = SimpleNamespace(
        ack_id="ack-e1",
        message=SimpleNamespace(
            data=json.dumps(cmd_diag).encode("utf-8"),
            attributes={"traceparent": "00-111-222-01"},
        ),
    )
    sub = MagicMock()
    sub.subscription_path.return_value = "projects/p/subscriptions/sub-e"
    sub.pull.return_value = SimpleNamespace(received_messages=[msg])

    assert (
        consumir_comandos_pull(store, sub, project_id="p", subscription_name="sub-e")
        == 1
    )
    assert sub.acknowledge.call_count == 1

    # Gera evento na outbox via EnfileirarExecucao.v1
    cmd_enf = _cmd(
        event_type="EnfileirarExecucao.v1",
        saga_id=saga_id,
        os_id=os_id,
        payload={
            "rodada": 1,
            "reserva_id": str(saga_id),
            "orcamento_id": str(uuid4()),
            "pagamento_id": str(uuid4()),
            "itens_servico": [
                {
                    "servico_id": str(uuid4()),
                    "descricao": "Rev",
                    "preco_unitario_centavos": 10000,
                }
            ],
            "itens_peca": [],
        },
    )
    ExecucaoConsumidor(store).processar_comando(cmd_enf)

    pub_err = MagicMock()
    pub_err.topic_path.return_value = "projects/p/topics/t"
    fut_err = MagicMock()
    fut_err.result.side_effect = RuntimeError("Broker indisponível")
    pub_err.publish.return_value = fut_err
    assert publicar_lote_outbox(store, pub_err, project_id="p") == 0

    out_doc = store.listar("outbox")[0]
    assert out_doc["status"] == "FALHA_AGENDADA"
    out_doc["next_attempt_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    store.salvar("outbox", out_doc["id"], out_doc)

    pub_ok = MagicMock()
    pub_ok.topic_path.return_value = "projects/p/topics/t"
    fut_ok = MagicMock()
    fut_ok.result.return_value = "msg-e1"
    pub_ok.publish.return_value = fut_ok
    assert publicar_lote_outbox(store, pub_ok, project_id="p") == 1
    assert store.buscar("outbox", out_doc["id"])["status"] == "PROCESSADO"
