from decimal import Decimal
from uuid import UUID

from src.catalogo.dominio.entidades import Servico
from src.execucao.infraestrutura.firestore_store import DocumentoStore


class ServicoRepositorioFirestore:
    def __init__(self, store: DocumentoStore) -> None:
        self.store = store

    def salvar(self, servico: Servico) -> Servico:
        preco_centavos = int((servico.preco_base * Decimal(100)).quantize(Decimal("1")))
        self.store.salvar(
            "servicos",
            str(servico.id),
            {
                "id": str(servico.id),
                "nome": servico.nome,
                "descricao": servico.descricao,
                "preco_base": f"{servico.preco_base:.2f}",
                "preco_base_centavos": preco_centavos,
                "tempo_estimado_minutos": servico.tempo_estimado_minutos,
            },
        )
        return servico

    def buscar_por_id(self, id: UUID) -> Servico | None:
        doc = self.store.buscar("servicos", str(id))
        if doc is None:
            return None
        return Servico(
            id=UUID(str(doc["id"])),
            nome=str(doc["nome"]),
            descricao=str(doc["descricao"]),
            preco_base=Decimal(str(doc["preco_base"])),
            tempo_estimado_minutos=int(doc["tempo_estimado_minutos"]),
        )

    def listar(self, busca: str | None = None) -> list[Servico]:
        docs = self.store.listar("servicos")
        servicos = [
            Servico(
                id=UUID(str(d["id"])),
                nome=str(d["nome"]),
                descricao=str(d["descricao"]),
                preco_base=Decimal(str(d["preco_base"])),
                tempo_estimado_minutos=int(d["tempo_estimado_minutos"]),
            )
            for d in docs
        ]
        if busca:
            termo = busca.lower()
            servicos = [s for s in servicos if termo in s.nome.lower()]
        return servicos

    def atualizar(self, servico: Servico) -> Servico:
        return self.salvar(servico)

    def remover(self, id: UUID) -> None:
        self.store.remover("servicos", str(id))
