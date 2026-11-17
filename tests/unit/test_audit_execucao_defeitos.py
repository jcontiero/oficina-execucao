from uuid import uuid4

from fastapi.testclient import TestClient

from src.config import Configuracoes
from src.execucao.dominio.entidades import (
    StatusReserva,
)
from src.execucao.infraestrutura.firestore_store import (
    ExecucaoRepositorioFirestore,
    InMemoryFirestoreStore,
)
from src.main import criar_app
from src.mensageria.consumidor import ExecucaoConsumidor
from tests.unit.test_execucao_p2 import _emitir_jwt, JWKS_JSON


def _cmd_envelope(
    event_type: str,
    *,
    saga_id,
    os_id,
    payload: dict,
) -> dict:
    return {
        "event_id": str(uuid4()),
        "event_type": event_type,
        "schema_version": 1,
        "occurred_at": "2026-11-17T10:00:00+00:00",
        "producer": "oficina-os",
        "saga_id": str(saga_id),
        "os_id": str(os_id),
        "correlation_id": str(saga_id),
        "causation_id": str(uuid4()),
        "aggregate_version": 1,
        "payload": payload,
    }


def _client_execucao() -> tuple[TestClient, InMemoryFirestoreStore, str]:
    cfg = Configuracoes(
        use_in_memory_store=True,
        environment="homolog",
        jwks_public_keys_json=JWKS_JSON,
        dd_trace_enabled=False,
    )
    app = criar_app(cfg)
    token = _emitir_jwt(
        sub=uuid4(),
        actor_type="FUNCIONARIO",
        iss="oficina-admin-homolog",
        perfil="MECANICO",
    )
    return TestClient(app), app.state.store, token


def test_r02_iniciar_execucao_exige_em_fila_pagamento_e_reserva() -> None:
    client, store, token = _client_execucao()
    os_id = uuid4()
    saga_id = uuid4()
    exec_id = f"{os_id}_1"
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Diagnóstico sem enfileiramento/pagamento/reserva não pode permitir /iniciar
    r_diag = client.post(
        f"/v1/execucoes/{exec_id}/diagnostico",
        json={
            "os_id": str(os_id),
            "saga_id": str(saga_id),
            "rodada": 1,
            "laudo": "Troca de oleo",
            "itens_servico": [
                {
                    "servico_id": str(uuid4()),
                    "descricao": "Troca de oleo",
                    "preco_unitario_centavos": 10000,
                }
            ],
            "itens_peca": [],
        },
        headers=headers,
    )
    assert r_diag.status_code == 201

    r_iniciar_precoce = client.post(f"/v1/execucoes/{exec_id}/iniciar", headers=headers)
    assert r_iniciar_precoce.status_code == 409

    # 2. Enfileira mas ainda sem reserva ativa -> continua bloqueado (409)
    cons = ExecucaoConsumidor(store, environment="homolog")
    cons.processar_comando(
        _cmd_envelope(
            "EnfileirarExecucao.v1",
            saga_id=saga_id,
            os_id=os_id,
            payload={
                "rodada": 1,
                "pagamento_id": str(uuid4()),
                "reserva_id": str(uuid4()),
                "orcamento_id": str(uuid4()),
                "itens_servico": [],
                "itens_peca": [],
            },
        )
    )
    r_sem_reserva = client.post(f"/v1/execucoes/{exec_id}/iniciar", headers=headers)
    assert r_sem_reserva.status_code == 409

    # 3. Com reserva ativa + EM_FILA + pagamento_id -> inicia (200) e segunda chamada falha (409)
    store.salvar(
        "reservas",
        str(saga_id),
        {
            "saga_id": str(saga_id),
            "os_id": str(os_id),
            "rodada": 1,
            "status": StatusReserva.ATIVA.value,
            "itens": [],
        },
    )
    r_ok = client.post(f"/v1/execucoes/{exec_id}/iniciar", headers=headers)
    assert r_ok.status_code == 200
    r_dup = client.post(f"/v1/execucoes/{exec_id}/iniciar", headers=headers)
    assert r_dup.status_code == 409


def test_r05_liberar_reserva_com_event_id_distinto_nao_repoe_estoque_duas_vezes() -> (
    None
):
    store = InMemoryFirestoreStore()
    cons = ExecucaoConsumidor(store, environment="homolog")
    os_id = uuid4()
    saga_id = uuid4()
    peca_id = str(uuid4())
    reserva_id = uuid4()

    store.salvar("pecas", peca_id, {"id": peca_id, "quantidade_disponivel": 10})
    cons.processar_comando(
        _cmd_envelope(
            "ReservarPecas.v1",
            saga_id=saga_id,
            os_id=os_id,
            payload={
                "rodada": 1,
                "reserva_id": str(reserva_id),
                "orcamento_id": str(uuid4()),
                "orcamento_versao": 1,
                "itens_peca": [{"peca_id": peca_id, "quantidade": 3}],
            },
        )
    )
    assert store.buscar("pecas", peca_id)["quantidade_disponivel"] == 7

    cons.processar_comando(
        _cmd_envelope(
            "CancelarExecucao.v1",
            saga_id=saga_id,
            os_id=os_id,
            payload={
                "rodada": 1,
                "compensacao_id": str(uuid4()),
                "motivo": "PAGAMENTO_FALHOU",
            },
        )
    )
    controle = ExecucaoRepositorioFirestore(store).buscar_controle(saga_id)
    assert controle is not None
    assert controle.cancelamento_event_id is not None

    for _ in range(2):
        cons.processar_comando(
            _cmd_envelope(
                "LiberarReserva.v1",
                saga_id=saga_id,
                os_id=os_id,
                payload={
                    "rodada": 1,
                    "compensacao_id": str(uuid4()),
                    "reserva_id": str(reserva_id),
                    "execucao_cancelada_event_id": str(controle.cancelamento_event_id),
                    "motivo": "PAGAMENTO_FALHOU",
                },
            )
        )

    assert store.buscar("pecas", peca_id)["quantidade_disponivel"] == 10


def test_r10_cancelar_barreira_usa_transacao_atomica() -> None:
    client, store, token = _client_execucao()
    os_id = uuid4()
    saga_id = uuid4()
    exec_id = f"{os_id}_1"
    headers = {"Authorization": f"Bearer {token}"}

    chamadas_tx = 0
    orig_tx = store.executar_em_transacao

    def _spy_tx(fn):
        nonlocal chamadas_tx
        chamadas_tx += 1
        return orig_tx(fn)

    store.executar_em_transacao = _spy_tx  # type: ignore[method-assign]

    res = client.post(
        f"/v1/execucoes/{exec_id}/cancelar-barreira",
        json={
            "saga_id": str(saga_id),
            "os_id": str(os_id),
            "rodada": 1,
            "cancelamento_event_id": str(uuid4()),
            "motivo": "ORCAMENTO_RECUSADO",
        },
        headers=headers,
    )
    assert res.status_code == 200
    assert chamadas_tx >= 1
