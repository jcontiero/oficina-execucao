from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware

from src.config import Configuracoes
from src.catalogo.apresentacao.rotas import router as catalogo_router
from src.estoque.apresentacao.rotas import router as estoque_router
from src.execucao.apresentacao.rotas import router as execucao_router
from src.execucao.infraestrutura.firestore_store import (
    GoogleFirestoreStore,
    InMemoryFirestoreStore,
)
from src.shared.observabilidade import configurar_observabilidade
from src.shared.seguranca import ValidadorJwtRs256


def criar_app(configuracoes: Configuracoes | None = None) -> FastAPI:
    config = configuracoes or Configuracoes()
    configurar_observabilidade(
        service=config.dd_service,
        env=config.dd_env,
        version=config.dd_version,
        trace_enabled=config.dd_trace_enabled,
    )

    app = FastAPI(
        title="Oficina Execução Service API",
        description="Microsserviço de Execução, Diagnóstico, Catálogo e Estoque (Firestore NoSQL)",
        version=config.dd_version,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if config.use_in_memory_store:
        store = InMemoryFirestoreStore()
    else:
        store = GoogleFirestoreStore(config)

    app.state.config = config
    app.state.store = store
    app.state.validador_jwt = ValidadorJwtRs256(config)

    @app.get("/health", tags=["health"])
    def health_check() -> dict[str, str]:
        return {"status": "ok", "service": "oficina-execucao"}

    @app.get("/health/live", tags=["health"])
    def liveness_check() -> dict[str, str]:
        return {"status": "alive", "service": "oficina-execucao"}

    @app.get("/health/ready", tags=["health"])
    def readiness_check() -> dict[str, str]:
        try:
            if not app.state.store.ping():
                raise RuntimeError("Firestore ping returned False")
            return {
                "status": "ready",
                "service": "oficina-execucao",
                "firestore": "connected",
                "database": config.firestore_database,
            }
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Firestore not ready: {exc}",
            ) from exc

    app.include_router(catalogo_router)
    app.include_router(estoque_router)
    app.include_router(execucao_router)
    return app


def _criar_app_padrao() -> FastAPI:
    cfg = Configuracoes()
    if not cfg.firestore_emulator_host and not cfg.use_in_memory_store:
        cfg.use_in_memory_store = True
    return criar_app(cfg)


app = _criar_app_padrao()
