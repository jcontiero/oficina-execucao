class EstoqueInsuficienteError(Exception):
    def __init__(self, nome: str, solicitado: int, disponivel: int) -> None:
        super().__init__(
            f"Estoque insuficiente para '{nome}': solicitado {solicitado}, disponível {disponivel}"
        )


class PecaNaoEncontradaError(Exception):
    def __init__(self, identificador: object) -> None:
        super().__init__(f"Peça não encontrada: {identificador}")


class CodigoPecaDuplicadoError(Exception):
    def __init__(self, codigo: str) -> None:
        super().__init__(f"Já existe uma peça com o código: {codigo}")


class ReposicaoInvalidaError(Exception):
    def __init__(self) -> None:
        super().__init__("Quantidade de reposição deve ser maior que zero")
