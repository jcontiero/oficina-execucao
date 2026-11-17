from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field


class CadastrarPecaRequest(BaseModel):
    id: UUID | None = None
    nome: str
    codigo: str
    preco_unitario: Decimal
    quantidade_disponivel: int = Field(ge=0)
    quantidade_minima_alerta: int = Field(default=5, ge=0)


class AtualizarPecaRequest(BaseModel):
    nome: str | None = None
    preco_unitario: Decimal | None = None
    quantidade_minima_alerta: int | None = Field(default=None, ge=0)


class ReporEstoqueRequest(BaseModel):
    quantidade: int = Field(gt=0)


class PecaResponse(BaseModel):
    id: str
    nome: str
    codigo: str
    preco_unitario: str
    quantidade_disponivel: int
    quantidade_minima_alerta: int
    alerta_estoque_baixo: bool

    @classmethod
    def from_domain(cls, peca: object) -> "PecaResponse":
        return cls(
            id=str(getattr(peca, "id")),
            nome=str(getattr(peca, "nome")),
            codigo=str(getattr(peca, "codigo")),
            preco_unitario=str(getattr(peca, "preco_unitario")),
            quantidade_disponivel=int(getattr(peca, "quantidade_disponivel")),
            quantidade_minima_alerta=int(getattr(peca, "quantidade_minima_alerta")),
            alerta_estoque_baixo=bool(getattr(peca, "alerta_estoque_baixo")),
        )
