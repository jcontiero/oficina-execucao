from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from src.execucao.dominio.entidades import (
    ControleSagaExecucao,
    EstadoControleSaga,
    StatusExecucao,
)
from src.execucao.infraestrutura.firestore_store import ExecucaoRepositorioFirestore
from src.shared.seguranca import ActorType, Principal, get_principal_atual

router = APIRouter()


class DiagnosticoRequest(BaseModel):
    os_id: UUID
    saga_id: UUID
    rodada: int = Field(default=1, ge=1)
    laudo: str = Field(min_length=1)
    itens_servico: list[dict[str, Any]] = Field(min_length=1)
    itens_peca: list[dict[str, Any]] = Field(default_factory=list)


def require_mecanico_ou_admin(
    principal: Principal = Depends(get_principal_atual),
) -> Principal:
    if principal.actor_type != ActorType.FUNCIONARIO or principal.perfil not in {
        "ADMIN",
        "MECANICO",
    }:
        raise HTTPException(
            status_code=403,
            detail="Acesso restrito a funcionários (MECANICO ou ADMIN)",
        )
    return principal


def get_repo(request: Request) -> ExecucaoRepositorioFirestore:
    return ExecucaoRepositorioFirestore(request.app.state.store)


@router.get("/v1/execucoes", tags=["Execução"])
def listar_execucoes(
    _principal: Principal = Depends(require_mecanico_ou_admin),
    repo: ExecucaoRepositorioFirestore = Depends(get_repo),
) -> list[dict[str, Any]]:
    return repo.listar_execucoes()


@router.post(
    "/v1/execucoes/{execucao_id}/diagnostico",
    status_code=status.HTTP_201_CREATED,
    tags=["Execução"],
)
def registrar_diagnostico(
    execucao_id: str,
    body: DiagnosticoRequest,
    _principal: Principal = Depends(require_mecanico_ou_admin),
    repo: ExecucaoRepositorioFirestore = Depends(get_repo),
) -> dict[str, Any]:
    controle = repo.buscar_controle(body.saga_id)
    if (
        controle is not None
        and controle.estado_controle == EstadoControleSaga.CANCELADO_TOMBSTONE
    ):
        raise HTTPException(
            status_code=409,
            detail="Saga cancelada por tombstone; diagnóstico não pode prosseguir",
        )

    if controle is None:
        controle = ControleSagaExecucao(
            saga_id=body.saga_id,
            os_id=body.os_id,
            rodada=body.rodada,
            estado_controle=EstadoControleSaga.ATIVO,
        )
        repo.salvar_controle(controle)

    doc = {
        "id": execucao_id,
        "os_id": str(body.os_id),
        "saga_id": str(body.saga_id),
        "rodada": body.rodada,
        "laudo": body.laudo,
        "itens_servico": body.itens_servico,
        "itens_peca": body.itens_peca,
        "status": StatusExecucao.AGUARDANDO_EXECUCAO.value,
        "diagnosticado_em": datetime.now(timezone.utc).isoformat(),
    }
    repo.salvar_execucao(execucao_id, doc)
    return doc


@router.post("/v1/execucoes/{execucao_id}/iniciar", tags=["Execução"])
def iniciar_execucao(
    execucao_id: str,
    _principal: Principal = Depends(require_mecanico_ou_admin),
    repo: ExecucaoRepositorioFirestore = Depends(get_repo),
) -> dict[str, Any]:
    doc = repo.buscar_execucao(execucao_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Execução não encontrada")

    saga_id = UUID(str(doc["saga_id"]))
    controle = repo.buscar_controle(saga_id) or ControleSagaExecucao(
        saga_id=saga_id,
        os_id=UUID(str(doc["os_id"])),
        rodada=int(doc.get("rodada", 1)),
    )
    try:
        controle.iniciar_reparo()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    repo.salvar_controle(controle)
    doc["status"] = StatusExecucao.EM_EXECUCAO.value
    doc["iniciada_em"] = datetime.now(timezone.utc).isoformat()
    repo.salvar_execucao(execucao_id, doc)
    return doc


@router.post("/v1/execucoes/{execucao_id}/concluir", tags=["Execução"])
def concluir_execucao(
    execucao_id: str,
    _principal: Principal = Depends(require_mecanico_ou_admin),
    repo: ExecucaoRepositorioFirestore = Depends(get_repo),
) -> dict[str, Any]:
    doc = repo.buscar_execucao(execucao_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Execução não encontrada")
    if doc.get("status") != StatusExecucao.EM_EXECUCAO.value:
        raise HTTPException(
            status_code=409,
            detail="Apenas execuções em andamento (EM_EXECUCAO) podem ser concluídas",
        )

    saga_id = UUID(str(doc["saga_id"]))
    controle = repo.buscar_controle(saga_id)
    if controle is not None:
        controle.estado_controle = EstadoControleSaga.CONCLUIDO
        controle.version += 1
        repo.salvar_controle(controle)

    doc["status"] = StatusExecucao.CONCLUIDA.value
    doc["concluida_em"] = datetime.now(timezone.utc).isoformat()
    repo.salvar_execucao(execucao_id, doc)
    return doc
