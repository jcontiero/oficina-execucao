import base64
from datetime import datetime, timedelta, timezone
import json
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import jwt
import pytest

from src.config import Configuracoes
from src.main import criar_app


def _gerar_par_rsa_e_jwks(kid: str = "int-kid-1"):
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


def emitir_jwt_teste(
    *,
    sub: UUID,
    actor_type: str = "FUNCIONARIO",
    iss: str = "oficina-admin-homolog",
    aud: str = "oficina-backend-homolog",
    perfil: str = "ADMIN",
) -> str:
    agora = datetime.now(timezone.utc)
    payload = {
        "sub": str(sub),
        "actor_type": actor_type,
        "iss": iss,
        "aud": aud,
        "perfil": perfil,
        "iat": int(agora.timestamp()),
        "exp": int((agora + timedelta(minutes=30)).timestamp()),
    }
    return jwt.encode(
        payload,
        PRIVATE_KEY,
        algorithm="RS256",
        headers={"kid": "int-kid-1", "alg": "RS256"},
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


@pytest.fixture
def headers_admin():
    tok = emitir_jwt_teste(sub=uuid4(), actor_type="FUNCIONARIO", perfil="ADMIN")
    return {"Authorization": f"Bearer {tok}"}
