import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import jwt
import pytest

from src.config import Configuracoes
from src.execucao.dominio.entidades import (
    ControleSagaExecucao,
    EstadoControleSaga,
    ReservaEstoque,
    StatusReserva,
)
from src.main import criar_app
from src.mensageria.contratos import validar_comando_execucao_v1
from src.shared.observabilidade import configurar_observabilidade, correlacao_ctx


def _gerar_par_rsa_e_jwks(kid: str = "exec-kid-1"):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_numbers = private_key.public_key().public_numbers()

    def _b64(n: int) -> str:
        b = n.to_bytes((n.bit_length() + 7) // 8, byteorder="big")
        return base64.urlsafe_b64encode(b).decode("utf-8").rstrip("=")

    jwk = {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _b64(public_numbers.n),
        "e": _b64(public_numbers.e),
    }
    return private_key, json.dumps({"keys": [jwk]})


PRIVATE_KEY, JWKS_JSON = _gerar_par_rsa_e_jwks()


def _emitir_jwt(
    *,
    sub: UUID,
    actor_type: str,
    iss: str,
    aud: str = "oficina-backend-homolog",
    perfil: str | None = None,
    kid: str = "exec-kid-1",
) -> str:
    agora = datetime.now(timezone.utc)
    payload = {
        "sub": str(sub),
        "actor_type": actor_type,
        "iss": iss,
        "aud": aud,
        "iat": int(agora.timestamp()),
        "exp": int((agora + timedelta(minutes=30)).timestamp()),
    }
    if perfil:
        payload["perfil"] = perfil
    return jwt.encode(
        payload,
        PRIVATE_KEY,
        algorithm="RS256",
        headers={"kid": kid, "alg": "RS256"},
    )


@pytest.fixture
def client():
    cfg = Configuracoes(
        use_in_memory_store=True,
        jwks_public_keys_json=JWKS_JSON,
        dd_trace_enabled=False,
    )
    app = criar_app(cfg)
    return TestClient(app)


def test_healthchecks_e_observabilidade_execucao(client):
    assert client.get("/health").status_code == 200
    assert client.get("/health/live").json()["status"] == "alive"
    ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["firestore"] == "connected"

    token = correlacao_ctx.set({"correlation_id": "c-1", "saga_id": "s-1"})
    try:
        logger = configurar_observabilidade(
            service="oficina-execucao",
            env="homolog",
            version="0.1.0",
            trace_enabled=False,
        )
        logger.info("Log correlacionado oficina-execucao")
    finally:
        correlacao_ctx.reset(token)


def test_ciclo_execucao_diagnostico_inicio_conclusao_e_rbac(client):
    tok_mecanico = _emitir_jwt(
        sub=uuid4(),
        actor_type="FUNCIONARIO",
        iss="oficina-admin-homolog",
        perfil="MECANICO",
    )
    tok_cliente = _emitir_jwt(
        sub=uuid4(),
        actor_type="CLIENTE",
        iss="oficina-auth-homolog",
    )

    os_id = uuid4()
    saga_id = uuid4()
    exec_id = f"{os_id}_1"

    # Cliente não pode operar rotas de execução (403)
    r_forb = client.get(
        "/v1/execucoes", headers={"Authorization": f"Bearer {tok_cliente}"}
    )
    assert r_forb.status_code == 403

    # Mecânico registra diagnóstico
    r_diag = client.post(
        f"/v1/execucoes/{exec_id}/diagnostico",
        headers={"Authorization": f"Bearer {tok_mecanico}"},
        json={
            "os_id": str(os_id),
            "saga_id": str(saga_id),
            "rodada": 1,
            "laudo": "Troca de correia dentada",
            "itens_servico": [
                {
                    "servico_id": str(uuid4()),
                    "descricao": "Mão de obra correia",
                    "preco_unitario_centavos": 25000,
                }
            ],
            "itens_peca": [],
        },
    )
    assert r_diag.status_code == 201
    assert r_diag.json()["status"] == "AGUARDANDO_EXECUCAO"

    # Listagem
    r_list = client.get(
        "/v1/execucoes", headers={"Authorization": f"Bearer {tok_mecanico}"}
    )
    assert r_list.status_code == 200
    assert len(r_list.json()) == 1

    # Concluir antes de iniciar retorna 409
    r_conc_antes = client.post(
        f"/v1/execucoes/{exec_id}/concluir",
        headers={"Authorization": f"Bearer {tok_mecanico}"},
    )
    assert r_conc_antes.status_code == 409

    # Iniciar antes do enfileiramento e reserva ativa retorna 409
    r_ini_antes = client.post(
        f"/v1/execucoes/{exec_id}/iniciar",
        headers={"Authorization": f"Bearer {tok_mecanico}"},
    )
    assert r_ini_antes.status_code == 409

    # Simula reserva ativa + pagamento confirmado + enfileiramento antes de iniciar
    client.app.state.store.salvar(
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
    exec_doc = client.app.state.store.buscar("execucoes", exec_id)
    assert exec_doc is not None
    exec_doc["pagamento_id"] = str(uuid4())
    client.app.state.store.salvar("execucoes", exec_id, exec_doc)
    ctrl_doc = client.app.state.store.buscar("controle_saga", str(saga_id))
    assert ctrl_doc is not None
    ctrl_doc["estado_controle"] = EstadoControleSaga.EM_FILA.value
    client.app.state.store.salvar("controle_saga", str(saga_id), ctrl_doc)

    # Iniciar reparo
    r_ini = client.post(
        f"/v1/execucoes/{exec_id}/iniciar",
        headers={"Authorization": f"Bearer {tok_mecanico}"},
    )
    assert r_ini.status_code == 200
    assert r_ini.json()["status"] == "EM_EXECUCAO"

    # Concluir reparo
    r_conc = client.post(
        f"/v1/execucoes/{exec_id}/concluir",
        headers={"Authorization": f"Bearer {tok_mecanico}"},
    )
    assert r_conc.status_code == 200
    assert r_conc.json()["status"] == "CONCLUIDA"


def test_barreira_cancelamento_tombstone_e_disputa_com_reparo_iniciado():
    saga_id = uuid4()
    os_id = uuid4()
    evt_cancel_id = uuid4()

    # Caso 1: Cancelamento vence antes de iniciar reparo -> grava tombstone e permite liberar reserva
    controle = ControleSagaExecucao(saga_id=saga_id, os_id=os_id, rodada=1)
    res_cancel = controle.cancelar_com_tombstone(
        cancelamento_event_id=evt_cancel_id, motivo="FALHA_ENFILEIRAMENTO"
    )
    assert res_cancel["event_type"] == "ExecucaoCancelada.v1"
    assert res_cancel["tombstone_persistido"] is True
    assert controle.estado_controle == EstadoControleSaga.CANCELADO_TOMBSTONE

    # Tentativa tardia de iniciar reparo após tombstone deve ser bloqueada
    with pytest.raises(ValueError, match="saga cancelada por tombstone"):
        controle.iniciar_reparo()

    reserva = ReservaEstoque(saga_id=saga_id, os_id=os_id, rodada=1)
    res_lib = reserva.liberar_apos_barreira(
        controle=controle, execucao_cancelada_event_id=evt_cancel_id
    )
    assert res_lib["event_type"] == "ReservaLiberada.v1"
    assert reserva.status == StatusReserva.LIBERADA

    # Caso 2: Reparo já iniciado -> recusa cancelamento automático e bloqueia liberação de reserva
    controle_em_reparo = ControleSagaExecucao(
        saga_id=uuid4(),
        os_id=os_id,
        rodada=1,
        estado_controle=EstadoControleSaga.EM_FILA,
    )
    controle_em_reparo.iniciar_reparo()
    recusa = controle_em_reparo.cancelar_com_tombstone(
        cancelamento_event_id=uuid4(), motivo="FALHA_ENFILEIRAMENTO"
    )
    assert recusa["event_type"] == "ExecucaoCancelamentoRecusado.v1"
    assert recusa["motivo"] == "REPARO_INICIADO"

    reserva_ativa = ReservaEstoque(
        saga_id=controle_em_reparo.saga_id,
        os_id=os_id,
        rodada=1,
        status=StatusReserva.CONSUMIDA,
    )
    with pytest.raises(ValueError, match="Barreira violada"):
        reserva_ativa.liberar_apos_barreira(
            controle=controle_em_reparo, execucao_cancelada_event_id=uuid4()
        )


def test_contratos_mensageria_execucao_exige_barreira_em_liberar_reserva():
    fixtures = json.loads(
        Path("docs/contratos/fixtures/fluxo-feliz-e-compensacao-v1.json").read_text()
    )
    cmd_cancel_exec = fixtures["compensacao_falha_fila_apos_pagamento"][1]
    cmd_liberar = fixtures["compensacao_falha_fila_apos_pagamento"][3]

    assert (
        validar_comando_execucao_v1(cmd_cancel_exec).event_type == "CancelarExecucao.v1"
    )
    assert validar_comando_execucao_v1(cmd_liberar).event_type == "LiberarReserva.v1"

    sem_barreira = {
        **cmd_liberar,
        "payload": {
            **cmd_liberar["payload"],
            "execucao_cancelada_event_id": "",
        },
    }
    with pytest.raises(ValueError, match="exige execucao_cancelada_event_id"):
        validar_comando_execucao_v1(sem_barreira)
