from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID


@dataclass
class Peca:
    id: UUID
    nome: str
    codigo: str
    preco_unitario: Decimal
    quantidade_disponivel: int
    quantidade_minima_alerta: int

    def reservar(self, quantidade: int) -> None:
        from src.estoque.dominio.excecoes import (
            EstoqueInsuficienteError,
            ReposicaoInvalidaError,
        )

        if quantidade <= 0:
            raise ReposicaoInvalidaError()

        if quantidade > self.quantidade_disponivel:
            raise EstoqueInsuficienteError(
                self.nome, quantidade, self.quantidade_disponivel
            )
        self.quantidade_disponivel -= quantidade

    def repor(self, quantidade: int) -> None:
        from src.estoque.dominio.excecoes import ReposicaoInvalidaError

        if quantidade <= 0:
            raise ReposicaoInvalidaError()
        self.quantidade_disponivel += quantidade

    def liberar_reserva(self, quantidade: int) -> None:
        from src.estoque.dominio.excecoes import ReposicaoInvalidaError

        if quantidade <= 0:
            raise ReposicaoInvalidaError()
        self.quantidade_disponivel += quantidade

    @property
    def alerta_estoque_baixo(self) -> bool:
        return self.quantidade_disponivel <= self.quantidade_minima_alerta
