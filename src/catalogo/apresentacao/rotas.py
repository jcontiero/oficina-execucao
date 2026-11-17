from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from src.catalogo.aplicacao.casos_de_uso import (
    AtualizarServico,
    BuscarServico,
    CadastrarServico,
    ListarServicos,
    RemoverServico,
)
from src.catalogo.apresentacao.schemas import (
    ServicoAtualizarRequest,
    ServicoRequest,
    ServicoResponse,
)
from src.catalogo.dominio.excecoes import ServicoNaoEncontradoError
from src.catalogo.infraestrutura.repositorios import ServicoRepositorioFirestore
from src.shared.seguranca import ActorType, Principal, get_principal_atual

router = APIRouter(tags=["Catálogo"])


def require_admin(principal: Principal = Depends(get_principal_atual)) -> Principal:
    if principal.actor_type != ActorType.FUNCIONARIO or principal.perfil != "ADMIN":
        raise HTTPException(status_code=403, detail="Acesso restrito ao perfil ADMIN")
    return principal


def get_servico_repo(request: Request) -> ServicoRepositorioFirestore:
    return ServicoRepositorioFirestore(request.app.state.store)


@router.post("/servicos", response_model=ServicoResponse, status_code=201)
@router.post("/v1/servicos", response_model=ServicoResponse, status_code=201)
def cadastrar_servico(
    body: ServicoRequest,
    _admin: Principal = Depends(require_admin),
    repo: ServicoRepositorioFirestore = Depends(get_servico_repo),
) -> ServicoResponse:
    servico = CadastrarServico(repo).executar(
        id=body.id,
        nome=body.nome,
        descricao=body.descricao,
        preco_base=body.preco_base,
        tempo_estimado_minutos=body.tempo_estimado_minutos,
    )
    return ServicoResponse(
        id=servico.id,
        nome=servico.nome,
        descricao=servico.descricao,
        preco_base=servico.preco_base,
        tempo_estimado_minutos=servico.tempo_estimado_minutos,
    )


@router.get("/servicos", response_model=list[ServicoResponse])
@router.get("/v1/servicos", response_model=list[ServicoResponse])
def listar_servicos(
    _admin: Principal = Depends(require_admin),
    repo: ServicoRepositorioFirestore = Depends(get_servico_repo),
    busca: str | None = Query(default=None),
) -> list[ServicoResponse]:
    servicos = ListarServicos(repo).executar(busca)
    return [
        ServicoResponse(
            id=s.id,
            nome=s.nome,
            descricao=s.descricao,
            preco_base=s.preco_base,
            tempo_estimado_minutos=s.tempo_estimado_minutos,
        )
        for s in servicos
    ]


@router.get("/servicos/{id}", response_model=ServicoResponse)
@router.get("/v1/servicos/{id}", response_model=ServicoResponse)
def buscar_servico(
    id: UUID,
    _admin: Principal = Depends(require_admin),
    repo: ServicoRepositorioFirestore = Depends(get_servico_repo),
) -> ServicoResponse:
    try:
        s = BuscarServico(repo).executar(id)
        return ServicoResponse(
            id=s.id,
            nome=s.nome,
            descricao=s.descricao,
            preco_base=s.preco_base,
            tempo_estimado_minutos=s.tempo_estimado_minutos,
        )
    except ServicoNaoEncontradoError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/servicos/{id}", response_model=ServicoResponse)
@router.put("/v1/servicos/{id}", response_model=ServicoResponse)
def atualizar_servico(
    id: UUID,
    body: ServicoAtualizarRequest,
    _admin: Principal = Depends(require_admin),
    repo: ServicoRepositorioFirestore = Depends(get_servico_repo),
) -> ServicoResponse:
    try:
        s = AtualizarServico(repo).executar(
            id=id,
            nome=body.nome,
            descricao=body.descricao,
            preco_base=body.preco_base,
            tempo_estimado_minutos=body.tempo_estimado_minutos,
        )
        return ServicoResponse(
            id=s.id,
            nome=s.nome,
            descricao=s.descricao,
            preco_base=s.preco_base,
            tempo_estimado_minutos=s.tempo_estimado_minutos,
        )
    except ServicoNaoEncontradoError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/servicos/{id}", status_code=204)
@router.delete("/v1/servicos/{id}", status_code=204)
def remover_servico(
    id: UUID,
    _admin: Principal = Depends(require_admin),
    repo: ServicoRepositorioFirestore = Depends(get_servico_repo),
) -> None:
    try:
        RemoverServico(repo).executar(id)
    except ServicoNaoEncontradoError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
