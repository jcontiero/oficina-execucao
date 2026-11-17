from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from src.catalogo.apresentacao.rotas import require_admin
from src.estoque.aplicacao.casos_de_uso import (
    AtualizarPeca,
    BuscarPeca,
    CadastrarPeca,
    ListarPecas,
    RemoverPeca,
    ReporEstoque,
)
from src.estoque.apresentacao.schemas import (
    AtualizarPecaRequest,
    CadastrarPecaRequest,
    PecaResponse,
    ReporEstoqueRequest,
)
from src.estoque.dominio.excecoes import (
    CodigoPecaDuplicadoError,
    EstoqueInsuficienteError,
    PecaNaoEncontradaError,
)
from src.estoque.infraestrutura.repositorios import PecaRepositorioFirestore
from src.shared.seguranca import Principal

router = APIRouter(tags=["Estoque"])


def get_peca_repo(request: Request) -> PecaRepositorioFirestore:
    return PecaRepositorioFirestore(request.app.state.store)


@router.post("/pecas", response_model=PecaResponse, status_code=201)
@router.post("/v1/pecas", response_model=PecaResponse, status_code=201)
def cadastrar(
    body: CadastrarPecaRequest,
    _admin: Principal = Depends(require_admin),
    repo: PecaRepositorioFirestore = Depends(get_peca_repo),
) -> PecaResponse:
    try:
        peca = CadastrarPeca(repo).executar(**body.model_dump())
        return PecaResponse.from_domain(peca)
    except CodigoPecaDuplicadoError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/pecas", response_model=list[PecaResponse])
@router.get("/v1/pecas", response_model=list[PecaResponse])
def listar(
    _admin: Principal = Depends(require_admin),
    repo: PecaRepositorioFirestore = Depends(get_peca_repo),
    busca: str | None = None,
    alerta_estoque_baixo: bool = False,
) -> list[PecaResponse]:
    pecas = ListarPecas(repo).executar(busca, alerta_estoque_baixo)
    return [PecaResponse.from_domain(p) for p in pecas]


@router.get("/pecas/codigo/{codigo}", response_model=PecaResponse)
@router.get("/v1/pecas/codigo/{codigo}", response_model=PecaResponse)
def buscar_por_codigo(
    codigo: str,
    _admin: Principal = Depends(require_admin),
    repo: PecaRepositorioFirestore = Depends(get_peca_repo),
) -> PecaResponse:
    peca = repo.buscar_por_codigo(codigo)
    if not peca:
        raise HTTPException(
            status_code=404, detail=f"Peça com código '{codigo}' não encontrada"
        )
    return PecaResponse.from_domain(peca)


@router.get("/pecas/{id}", response_model=PecaResponse)
@router.get("/v1/pecas/{id}", response_model=PecaResponse)
def buscar(
    id: UUID,
    _admin: Principal = Depends(require_admin),
    repo: PecaRepositorioFirestore = Depends(get_peca_repo),
) -> PecaResponse:
    try:
        return PecaResponse.from_domain(BuscarPeca(repo).executar(id))
    except PecaNaoEncontradaError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/pecas/{id}", response_model=PecaResponse)
@router.put("/v1/pecas/{id}", response_model=PecaResponse)
def atualizar(
    id: UUID,
    body: AtualizarPecaRequest,
    _admin: Principal = Depends(require_admin),
    repo: PecaRepositorioFirestore = Depends(get_peca_repo),
) -> PecaResponse:
    try:
        peca = AtualizarPeca(repo).executar(id=id, **body.model_dump(exclude_none=True))
        return PecaResponse.from_domain(peca)
    except PecaNaoEncontradaError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/pecas/{id}", status_code=204)
@router.delete("/v1/pecas/{id}", status_code=204)
def remover(
    id: UUID,
    _admin: Principal = Depends(require_admin),
    repo: PecaRepositorioFirestore = Depends(get_peca_repo),
) -> None:
    try:
        RemoverPeca(repo).executar(id)
    except PecaNaoEncontradaError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/pecas/{id}/repor-estoque", response_model=PecaResponse)
@router.post("/v1/pecas/{id}/repor-estoque", response_model=PecaResponse)
def repor_estoque(
    id: UUID,
    body: ReporEstoqueRequest,
    _admin: Principal = Depends(require_admin),
    repo: PecaRepositorioFirestore = Depends(get_peca_repo),
) -> PecaResponse:
    try:
        peca = ReporEstoque(repo).executar(id, body.quantidade)
        return PecaResponse.from_domain(peca)
    except PecaNaoEncontradaError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (EstoqueInsuficienteError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
