from decimal import Decimal
import logging
from uuid import UUID, uuid4

from src.estoque.aplicacao.ports import NotificadorEstoque
from src.estoque.dominio.entidades import Peca
from src.estoque.dominio.excecoes import (
    CodigoPecaDuplicadoError,
    PecaNaoEncontradaError,
)
from src.estoque.dominio.repositorios import PecaRepositorio

logger = logging.getLogger("oficina-execucao")


class LogNotificadorEstoque:
    def notificar_admin_estoque_reposto(
        self,
        *,
        nome: str,
        codigo: str,
        quantidade_reposta: int,
        quantidade_atual: int,
    ) -> None:
        logger.info(
            "Estoque reposto: nome=%s codigo=%s qtd_reposta=%s qtd_atual=%s",
            nome,
            codigo,
            quantidade_reposta,
            quantidade_atual,
        )


class CadastrarPeca:
    def __init__(self, repo: PecaRepositorio) -> None:
        self.repo = repo

    def executar(
        self,
        nome: str,
        codigo: str,
        preco_unitario: Decimal,
        quantidade_disponivel: int,
        quantidade_minima_alerta: int = 5,
        id: UUID | None = None,
    ) -> Peca:
        if self.repo.buscar_por_codigo(codigo):
            raise CodigoPecaDuplicadoError(codigo)
        peca = Peca(
            id=id or uuid4(),
            nome=nome,
            codigo=codigo,
            preco_unitario=preco_unitario,
            quantidade_disponivel=quantidade_disponivel,
            quantidade_minima_alerta=quantidade_minima_alerta,
        )
        return self.repo.salvar(peca)


class ListarPecas:
    def __init__(self, repo: PecaRepositorio) -> None:
        self.repo = repo

    def executar(
        self, busca: str | None = None, apenas_alerta: bool = False
    ) -> list[Peca]:
        return self.repo.listar(busca, apenas_alerta)


class BuscarPeca:
    def __init__(self, repo: PecaRepositorio) -> None:
        self.repo = repo

    def executar(self, id: UUID) -> Peca:
        peca = self.repo.buscar_por_id(id)
        if not peca:
            raise PecaNaoEncontradaError(id)
        return peca


class AtualizarPeca:
    def __init__(self, repo: PecaRepositorio) -> None:
        self.repo = repo

    def executar(
        self,
        id: UUID,
        nome: str | None = None,
        preco_unitario: Decimal | None = None,
        quantidade_disponivel: int | None = None,
        quantidade_minima_alerta: int | None = None,
    ) -> Peca:
        peca = self.repo.buscar_por_id(id)
        if not peca:
            raise PecaNaoEncontradaError(id)
        if nome is not None:
            peca.nome = nome
        if preco_unitario is not None:
            peca.preco_unitario = preco_unitario
        if quantidade_disponivel is not None:
            peca.quantidade_disponivel = quantidade_disponivel
        if quantidade_minima_alerta is not None:
            peca.quantidade_minima_alerta = quantidade_minima_alerta
        return self.repo.salvar(peca)


class ReporEstoque:
    def __init__(
        self,
        repo: PecaRepositorio,
        notificador: NotificadorEstoque | None = None,
    ) -> None:
        self.repo = repo
        self.notificador = notificador or LogNotificadorEstoque()

    def executar(self, id: UUID, quantidade: int) -> Peca:
        peca = self.repo.buscar_por_id(id)
        if not peca:
            raise PecaNaoEncontradaError(id)
        peca.repor(quantidade)
        resultado = self.repo.salvar(peca)
        self.notificador.notificar_admin_estoque_reposto(
            nome=peca.nome,
            codigo=peca.codigo,
            quantidade_reposta=quantidade,
            quantidade_atual=resultado.quantidade_disponivel,
        )
        return resultado


class ReservarPeca:
    def __init__(self, repo: PecaRepositorio) -> None:
        self.repo = repo

    def executar(self, id: UUID, quantidade: int) -> Peca:
        peca = self.repo.buscar_por_id(id)
        if not peca:
            raise PecaNaoEncontradaError(id)
        peca.reservar(quantidade)
        return self.repo.salvar(peca)


class LiberarReserva:
    def __init__(self, repo: PecaRepositorio) -> None:
        self.repo = repo

    def executar(self, id: UUID, quantidade: int) -> Peca:
        peca = self.repo.buscar_por_id(id)
        if not peca:
            raise PecaNaoEncontradaError(id)
        peca.liberar_reserva(quantidade)
        return self.repo.salvar(peca)


class RemoverPeca:
    def __init__(self, repo: PecaRepositorio) -> None:
        self.repo = repo

    def executar(self, id: UUID) -> None:
        if not self.repo.buscar_por_id(id):
            raise PecaNaoEncontradaError(id)
        self.repo.remover(id)
