from typing import Protocol
from uuid import UUID

from src.estoque.dominio.entidades import Peca


class PecaRepositorio(Protocol):
    def salvar(self, peca: Peca) -> Peca: ...

    def buscar_por_id(self, id: UUID) -> Peca | None: ...

    def buscar_por_codigo(self, codigo: str) -> Peca | None: ...

    def listar(
        self, busca: str | None = None, apenas_alerta: bool = False
    ) -> list[Peca]: ...

    def remover(self, id: UUID) -> None: ...
