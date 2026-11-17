import json
from dataclasses import dataclass
from enum import Enum
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.config import Configuracoes

security = HTTPBearer()


class ActorType(str, Enum):
    FUNCIONARIO = "FUNCIONARIO"
    CLIENTE = "CLIENTE"
    SISTEMA = "SISTEMA"


@dataclass(frozen=True)
class Principal:
    id: UUID
    actor_type: ActorType
    perfil: str | None = None


class ValidadorJwtRs256:
    """
    Valida tokens JWT RS256 emitidos por emissores confiáveis mapeados estaticamente:
    - Clientes: iss = oficina-auth-{env} -> CLIENT_JWKS_URL
    - Funcionários: iss = oficina-admin-{env} -> ADMIN_JWKS_URL
    Não compartilha SECRET_KEY simétrica e nunca usa URL de chave vinda do token.
    """

    def __init__(self, config: Configuracoes) -> None:
        self.config = config
        self._jwks_clients: dict[str, jwt.PyJWKClient] = {
            config.jwt_client_issuer: jwt.PyJWKClient(config.client_jwks_url),
            config.jwt_admin_issuer: jwt.PyJWKClient(config.admin_jwks_url),
        }
        self._static_keys: dict[str, str] = {}
        if config.jwks_public_keys_json:
            dados = json.loads(config.jwks_public_keys_json)
            for jwk_dict in dados.get("keys", []):
                kid = jwk_dict.get("kid")
                if kid:
                    self._static_keys[kid] = jwt.algorithms.RSAAlgorithm.from_jwk(
                        json.dumps(jwk_dict)
                    )

    def _obter_chave_publica(self, token: str, issuer: str):
        header = jwt.get_unverified_header(token)
        alg = header.get("alg")
        kid = header.get("kid")
        if alg != "RS256" or not kid:
            raise ValueError("Token deve usar RS256 e conter kid no cabeçalho")

        if issuer not in self._jwks_clients:
            raise ValueError(f"Emissor não confiável: {issuer}")

        if kid in self._static_keys:
            return self._static_keys[kid]

        signing_key = self._jwks_clients[issuer].get_signing_key_from_jwt(token)
        return signing_key.key

    def decodificar(self, token: str) -> Principal:
        try:
            unverified = jwt.decode(
                token,
                options={"verify_signature": False},  # NOSONAR
                algorithms=["RS256"],
            )
            issuer = str(unverified.get("iss", ""))
            chave = self._obter_chave_publica(token, issuer)
            payload = jwt.decode(
                token,
                chave,
                algorithms=["RS256"],
                audience=self.config.jwt_audience,
                issuer=[self.config.jwt_client_issuer, self.config.jwt_admin_issuer],
                options={"require": ["sub", "exp", "iat", "iss", "aud", "actor_type"]},
            )
        except (jwt.PyJWTError, ValueError, KeyError) as exc:
            raise HTTPException(
                status_code=401, detail=f"Token inválido ou expirado: {exc}"
            ) from exc

        actor_raw = payload.get("actor_type")
        if actor_raw == ActorType.CLIENTE.value:
            if issuer != self.config.jwt_client_issuer:
                raise HTTPException(
                    status_code=401, detail="Emissor inválido para token de CLIENTE"
                )
            return Principal(
                id=UUID(str(payload["sub"])),
                actor_type=ActorType.CLIENTE,
                perfil="CLIENTE",
            )

        if actor_raw == ActorType.FUNCIONARIO.value:
            if issuer != self.config.jwt_admin_issuer:
                raise HTTPException(
                    status_code=401, detail="Emissor inválido para token de FUNCIONARIO"
                )
            perfil = payload.get("perfil")
            if perfil not in {"ADMIN", "MECANICO"}:
                raise HTTPException(
                    status_code=403, detail="Perfil de funcionário inválido"
                )
            return Principal(
                id=UUID(str(payload["sub"])),
                actor_type=ActorType.FUNCIONARIO,
                perfil=str(perfil),
            )

        raise HTTPException(status_code=401, detail="actor_type inválido no token")


def get_principal_atual(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> Principal:
    validador: ValidadorJwtRs256 = request.app.state.validador_jwt
    return validador.decodificar(credentials.credentials)
