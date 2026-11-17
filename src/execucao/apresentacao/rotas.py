from src.shared.observabilidade import traceparent_atual
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from src.execucao.dominio.entidades import (
    ControleSagaExecucao,
    EstadoControleSaga,
    StatusExecucao,
    StatusReserva,
)
from src.execucao.infraestrutura.firestore_store import ExecucaoRepositorioFirestore
from src.mensageria.consumidor import montar_doc_outbox
from src.shared.seguranca import ActorType, Principal, get_principal_atual

router = APIRouter()


class DiagnosticoRequest(BaseModel):
    os_id: UUID
    saga_id: UUID
    rodada: int = Field(default=1, ge=1)
    laudo: str = Field(min_length=1)
    itens_servico: list[dict[str, Any]] = Field(min_length=1)
    itens_peca: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("itens_servico", "itens_peca")
    @classmethod
    def validar_itens(cls, itens, info):
        campo = "servico_id" if info.field_name == "itens_servico" else "peca_id"
        for item in itens:
            UUID(str(item.get(campo) or item.get("id")))
            if (
                int(item.get("quantidade", 1)) <= 0
                or int(item.get("preco_unitario_centavos", -1)) < 0
            ):
                raise ValueError(
                    "Quantidade deve ser positiva e preço não pode ser negativo"
                )
        return itens


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


@router.get("/v1/execucoes/{execucao_id}", tags=["Execução"])
def buscar_execucao(
    execucao_id: str,
    _principal: Principal = Depends(require_mecanico_ou_admin),
    repo: ExecucaoRepositorioFirestore = Depends(get_repo),
) -> dict[str, Any]:
    doc = repo.buscar_execucao(execucao_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Execução não encontrada")
    return doc


@router.post("/v1/execucoes/{execucao_id}/cancelar-barreira", tags=["Execução"])
def cancelar_barreira_execucao(
    execucao_id: str,
    body: dict[str, Any],
    _principal: Principal = Depends(require_mecanico_ou_admin),
    repo: ExecucaoRepositorioFirestore = Depends(get_repo),
) -> dict[str, Any]:
    saga_id = UUID(str(body["saga_id"]))
    os_id = UUID(str(body["os_id"]))
    rodada = int(body.get("rodada", 1))
    cancelamento_event_id = UUID(str(body["cancelamento_event_id"]))
    motivo = str(body.get("motivo", "FALHA_ENFILEIRAMENTO"))

    def _tx(tx):
        controle_doc = tx.buscar("controle_saga", str(saga_id))
        controle = (
            ControleSagaExecucao(
                saga_id=saga_id,
                os_id=os_id,
                rodada=int(controle_doc.get("rodada", rodada)),
                estado_controle=EstadoControleSaga(
                    str(controle_doc["estado_controle"])
                ),
                cancelamento_event_id=(
                    UUID(str(controle_doc["cancelamento_event_id"]))
                    if controle_doc.get("cancelamento_event_id")
                    else None
                ),
                motivo_cancelamento=controle_doc.get("motivo_cancelamento"),
                version=int(controle_doc.get("version", 1)),
            )
            if controle_doc is not None
            else ControleSagaExecucao(saga_id=saga_id, os_id=os_id, rodada=rodada)
        )
        resultado = controle.cancelar_com_tombstone(
            cancelamento_event_id=cancelamento_event_id,
            motivo=motivo,
        )
        tx.salvar(
            "controle_saga",
            str(saga_id),
            {
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
            },
        )
        doc = tx.buscar("execucoes", execucao_id)
        if doc is not None and resultado["resultado"] == "CANCELADA":
            doc["status"] = StatusExecucao.CANCELADA.value
            tx.salvar("execucoes", execucao_id, doc)
        return resultado

    return repo.store.executar_em_transacao(_tx)


@router.post(
    "/v1/execucoes/{execucao_id}/diagnostico",
    status_code=status.HTTP_201_CREATED,
    tags=["Execução"],
)
def registrar_diagnostico(
    execucao_id: str,
    body: DiagnosticoRequest,
    request: Request,
    _principal: Principal = Depends(require_mecanico_ou_admin),
    repo: ExecucaoRepositorioFirestore = Depends(get_repo),
) -> dict[str, Any]:
    env = request.app.state.config.environment
    traceparent = traceparent_atual(request.headers.get("traceparent"))

    def _tx(tx):
        controle_doc = tx.buscar("controle_saga", str(body.saga_id))
        if (
            controle_doc is not None
            and controle_doc.get("estado_controle")
            == EstadoControleSaga.CANCELADO_TOMBSTONE.value
        ):
            raise HTTPException(
                status_code=409,
                detail="Saga cancelada por tombstone; diagnóstico não pode prosseguir",
            )
        if controle_doc is None:
            tx.salvar(
                "controle_saga",
                str(body.saga_id),
                {
                    "saga_id": str(body.saga_id),
                    "os_id": str(body.os_id),
                    "rodada": body.rodada,
                    "estado_controle": EstadoControleSaga.ATIVO.value,
                    "cancelamento_event_id": None,
                    "motivo_cancelamento": None,
                    "version": 1,
                },
            )
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
        tx.salvar("execucoes", execucao_id, doc)
        out = montar_doc_outbox(
            environment=env,
            event_type="DiagnosticoConcluido.v1",
            saga_id=body.saga_id,
            os_id=body.os_id,
            correlation_id=body.saga_id,
            causation_id=body.saga_id,
            aggregate_version=1,
            payload={
                "rodada": body.rodada,
                "diagnostico_id": execucao_id,
                "laudo": body.laudo,
                "itens_servico": body.itens_servico,
                "itens_peca": body.itens_peca,
            },
            traceparent=traceparent,
        )
        tx.salvar("outbox", out["id"], out)
        return doc

    return repo.store.executar_em_transacao(_tx)


@router.post("/v1/execucoes/{execucao_id}/iniciar", tags=["Execução"])
def iniciar_execucao(
    execucao_id: str,
    request: Request,
    _principal: Principal = Depends(require_mecanico_ou_admin),
    repo: ExecucaoRepositorioFirestore = Depends(get_repo),
) -> dict[str, Any]:
    env = request.app.state.config.environment
    traceparent = traceparent_atual(request.headers.get("traceparent"))

    def _tx(tx):
        doc = tx.buscar("execucoes", execucao_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="Execução não encontrada")

        saga_id = UUID(str(doc["saga_id"]))
        os_id = UUID(str(doc["os_id"]))
        rodada = int(doc.get("rodada", 1))
        controle_doc = tx.buscar("controle_saga", str(saga_id))
        controle = (
            ControleSagaExecucao(
                saga_id=saga_id,
                os_id=os_id,
                rodada=rodada,
                estado_controle=EstadoControleSaga(
                    str(controle_doc["estado_controle"])
                ),
                version=int(controle_doc.get("version", 1)),
            )
            if controle_doc is not None
            else ControleSagaExecucao(saga_id=saga_id, os_id=os_id, rodada=rodada)
        )
        if not doc.get("pagamento_id"):
            raise HTTPException(
                status_code=409,
                detail="Pagamento ainda não autorizado para iniciar execução",
            )
        reserva_doc = tx.buscar("reservas", str(saga_id))
        if (
            reserva_doc is None
            or reserva_doc.get("status") != StatusReserva.ATIVA.value
        ):
            raise HTTPException(
                status_code=409,
                detail="Reserva de estoque ativa obrigatória para iniciar execução",
            )

        try:
            controle.iniciar_reparo()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        tx.salvar(
            "controle_saga",
            str(saga_id),
            {
                "saga_id": str(saga_id),
                "os_id": str(os_id),
                "rodada": rodada,
                "estado_controle": controle.estado_controle.value,
                "cancelamento_event_id": None,
                "motivo_cancelamento": None,
                "version": controle.version,
            },
        )
        reserva_doc["status"] = StatusReserva.CONSUMIDA.value
        tx.salvar("reservas", str(saga_id), reserva_doc)
        consumida = True

        doc["status"] = StatusExecucao.EM_EXECUCAO.value
        doc["iniciada_em"] = datetime.now(timezone.utc).isoformat()
        tx.salvar("execucoes", execucao_id, doc)

        out = montar_doc_outbox(
            environment=env,
            event_type="ExecucaoIniciada.v1",
            saga_id=saga_id,
            os_id=os_id,
            correlation_id=saga_id,
            causation_id=saga_id,
            aggregate_version=controle.version,
            payload={
                "rodada": rodada,
                "execucao_id": execucao_id,
                "reserva_consumida": consumida,
            },
            traceparent=traceparent,
        )
        tx.salvar("outbox", out["id"], out)
        return doc

    return repo.store.executar_em_transacao(_tx)


@router.post("/v1/execucoes/{execucao_id}/concluir", tags=["Execução"])
def concluir_execucao(
    execucao_id: str,
    request: Request,
    _principal: Principal = Depends(require_mecanico_ou_admin),
    repo: ExecucaoRepositorioFirestore = Depends(get_repo),
) -> dict[str, Any]:
    env = request.app.state.config.environment
    traceparent = traceparent_atual(request.headers.get("traceparent"))

    def _tx(tx):
        doc = tx.buscar("execucoes", execucao_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="Execução não encontrada")
        if doc.get("status") != StatusExecucao.EM_EXECUCAO.value:
            raise HTTPException(
                status_code=409,
                detail="Apenas execuções em andamento (EM_EXECUCAO) podem ser concluídas",
            )

        saga_id = UUID(str(doc["saga_id"]))
        os_id = UUID(str(doc["os_id"]))
        rodada = int(doc.get("rodada", 1))
        controle_doc = tx.buscar("controle_saga", str(saga_id))
        version = int(controle_doc.get("version", 1)) + 1 if controle_doc else 2
        if controle_doc is not None:
            controle_doc["estado_controle"] = EstadoControleSaga.CONCLUIDO.value
            controle_doc["version"] = version
            tx.salvar("controle_saga", str(saga_id), controle_doc)

        agora = datetime.now(timezone.utc).isoformat()
        doc["status"] = StatusExecucao.CONCLUIDA.value
        doc["concluida_em"] = agora
        tx.salvar("execucoes", execucao_id, doc)

        out = montar_doc_outbox(
            environment=env,
            event_type="ExecucaoConcluida.v1",
            saga_id=saga_id,
            os_id=os_id,
            correlation_id=saga_id,
            causation_id=saga_id,
            aggregate_version=version,
            payload={
                "rodada": rodada,
                "execucao_id": execucao_id,
                "concluida_em": agora,
            },
            traceparent=traceparent,
        )
        tx.salvar("outbox", out["id"], out)
        return doc

    return repo.store.executar_em_transacao(_tx)
