from typing import Protocol


class NotificadorEstoque(Protocol):
    def notificar_admin_estoque_reposto(
        self,
        *,
        nome: str,
        codigo: str,
        quantidade_reposta: int,
        quantidade_atual: int,
    ) -> None: ...
