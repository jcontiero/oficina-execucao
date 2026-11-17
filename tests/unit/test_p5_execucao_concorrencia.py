from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from src.config import Configuracoes
from src.execucao.infraestrutura.firestore_store import InMemoryFirestoreStore
from src.main import criar_app
from src.mensageria.consumidor import ExecucaoConsumidor
from src.shared.seguranca import ActorType, Principal, get_principal_atual


def _cmd(event_type: str, *, saga_id, os_id, payload: dict) -> dict:
    return {
        "event_id": str(uuid4()),
        "event_type": event_type,
        "schema_version": 1,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "producer": "oficina-os",
        "saga_id": str(saga_id),
        "os_id": str(os_id),
        "correlation_id": str(saga_id),
        "causation_id": str(uuid4()),
        "aggregate_version": 1,
        "payload": payload,
    }


def _client_staff(store: InMemoryFirestoreStore) -> TestClient:
    cfg = Configuracoes(environment="homolog", dd_trace_enabled=False)
    app = criar_app(cfg)
    app.state.store = store
    app.dependency_overrides[get_principal_atual] = lambda: Principal(
        id=uuid4(), actor_type=ActorType.FUNCIONARIO, perfil="MECANICO"
    )
    return TestClient(app)


def test_concorrencia_reparo_iniciado_antes_cancelamento() -> None:
    store = InMemoryFirestoreStore()
    cons = ExecucaoConsumidor(store, environment="homolog")
    saga_id = uuid4()
    os_id = uuid4()

    assert (
        cons.processar_comando(
            _cmd(
                "ReservarPecas.v1",
                saga_id=saga_id,
                os_id=os_id,
                payload={
                    "rodada": 1,
                    "reserva_id": str(saga_id),
                    "orcamento_id": str(uuid4()),
                    "orcamento_versao": 1,
                    "itens_peca": [],
                },
            )
        )
        == "PROCESSADO"
    )
    assert (
        cons.processar_comando(
            _cmd(
                "EnfileirarExecucao.v1",
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
                            "descricao": "Revisão",
                            "preco_unitario_centavos": 10000,
                        }
                    ],
                    "itens_peca": [],
                },
            )
        )
        == "PROCESSADO"
    )

    client = _client_staff(store)
    exec_id = f"{os_id}_1"
    r_init = client.post(f"/v1/execucoes/{exec_id}/iniciar")
    assert r_init.status_code == 200

    # Agora CancelarExecucao.v1 chega e deve ser recusado (barreira exige intervenção manual)
    assert (
        cons.processar_comando(
            _cmd(
                "CancelarExecucao.v1",
                saga_id=saga_id,
                os_id=os_id,
                payload={
                    "rodada": 1,
                    "compensacao_id": str(uuid4()),
                    "motivo": "SOLICITACAO_CLIENTE",
                },
            )
        )
        == "PROCESSADO"
    )
    out_evts = [d["tipo_evento"] for d in store.listar("outbox")]
    assert "ExecucaoCancelamentoRecusado.v1" in out_evts


def test_concorrencia_cancelamento_antes_iniciar_e_rejeicao_fila() -> None:
    store = InMemoryFirestoreStore()
    cons = ExecucaoConsumidor(store, environment="homolog")
    saga_id = uuid4()
    os_id = uuid4()

    assert (
        cons.processar_comando(
            _cmd(
                "CancelarExecucao.v1",
                saga_id=saga_id,
                os_id=os_id,
                payload={
                    "rodada": 1,
                    "compensacao_id": str(uuid4()),
                    "motivo": "PAGAMENTO_RECUSADO",
                },
            )
        )
        == "PROCESSADO"
    )

    # EnfileirarExecucao.v1 após tombstone deve emitir ExecucaoRejeitada.v1
    assert (
        cons.processar_comando(
            _cmd(
                "ReservarPecas.v1",
                saga_id=saga_id,
                os_id=os_id,
                payload={
                    "rodada": 1,
                    "reserva_id": str(saga_id),
                    "orcamento_id": str(uuid4()),
                    "orcamento_versao": 1,
                    "itens_peca": [],
                },
            )
        )
        == "PROCESSADO"
    )
    assert (
        cons.processar_comando(
            _cmd(
                "EnfileirarExecucao.v1",
                saga_id=saga_id,
                os_id=os_id,
                payload={
                    "rodada": 1,
                    "reserva_id": str(saga_id),
                    "orcamento_id": str(uuid4()),
                    "pagamento_id": str(uuid4()),
                    "itens_servico": [],
                    "itens_peca": [],
                },
            )
        )
        == "PROCESSADO"
    )
    out_evts = [d["tipo_evento"] for d in store.listar("outbox")]
    assert "ExecucaoCancelada.v1" in out_evts
    assert "ExecucaoRejeitada.v1" in out_evts
