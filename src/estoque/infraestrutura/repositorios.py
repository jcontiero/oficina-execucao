from decimal import Decimal
from uuid import UUID

from src.estoque.dominio.entidades import Peca
from src.execucao.infraestrutura.firestore_store import DocumentoStore


class PecaRepositorioFirestore:
    def __init__(self, store: DocumentoStore) -> None:
        self.store = store

    def salvar(self, peca: Peca) -> Peca:
        preco_centavos = int(
            (peca.preco_unitario * Decimal(100)).quantize(Decimal("1"))
        )
        self.store.salvar(
            "pecas",
            str(peca.id),
            {
                "id": str(peca.id),
                "nome": peca.nome,
                "codigo": peca.codigo,
                "preco_unitario": f"{peca.preco_unitario:.2f}",
                "preco_unitario_centavos": preco_centavos,
                "quantidade_disponivel": peca.quantidade_disponivel,
                "quantidade_minima_alerta": peca.quantidade_minima_alerta,
            },
        )
        return peca

    def _doc_para_entidade(self, doc: dict) -> Peca:
        return Peca(
            id=UUID(str(doc["id"])),
            nome=str(doc["nome"]),
            codigo=str(doc["codigo"]),
            preco_unitario=Decimal(str(doc["preco_unitario"])),
            quantidade_disponivel=int(doc["quantidade_disponivel"]),
            quantidade_minima_alerta=int(doc["quantidade_minima_alerta"]),
        )

    def buscar_por_id(self, id: UUID) -> Peca | None:
        doc = self.store.buscar("pecas", str(id))
        if doc is None:
            return None
        return self._doc_para_entidade(doc)

    def buscar_por_codigo(self, codigo: str) -> Peca | None:
        for doc in self.store.listar("pecas"):
            if str(doc.get("codigo")) == codigo:
                return self._doc_para_entidade(doc)
        return None

    def listar(
        self, busca: str | None = None, apenas_alerta: bool = False
    ) -> list[Peca]:
        pecas = [self._doc_para_entidade(d) for d in self.store.listar("pecas")]
        if busca:
            termo = busca.lower()
            pecas = [
                p for p in pecas if termo in p.nome.lower() or termo in p.codigo.lower()
            ]
        if apenas_alerta:
            pecas = [p for p in pecas if p.alerta_estoque_baixo]
        return pecas

    def remover(self, id: UUID) -> None:
        self.store.remover("pecas", str(id))
