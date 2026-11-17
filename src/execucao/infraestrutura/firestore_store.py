import os
from typing import Any, Protocol
from uuid import UUID

from src.config import Configuracoes
from src.execucao.dominio.entidades import ControleSagaExecucao, EstadoControleSaga


class DocumentoStore(Protocol):
    def ping(self) -> bool: ...

    def salvar(
        self, colecao: str, doc_id: str, dados: dict[str, Any]
    ) -> dict[str, Any]: ...

    def buscar(self, colecao: str, doc_id: str) -> dict[str, Any] | None: ...

    def listar(self, colecao: str) -> list[dict[str, Any]]: ...


class InMemoryFirestoreStore:
    """
    Armazenamento em memória compatível com a estrutura de coleções do Firestore
    para testes unitários isolados sem necessidade de processo externo.
    """

    def __init__(self) -> None:
        self._cols: dict[str, dict[str, dict[str, Any]]] = {}

    def ping(self) -> bool:
        return True

    def salvar(
        self, colecao: str, doc_id: str, dados: dict[str, Any]
    ) -> dict[str, Any]:
        col = self._cols.setdefault(colecao, {})
        copia = dict(dados)
        col[doc_id] = copia
        return copia

    def buscar(self, colecao: str, doc_id: str) -> dict[str, Any] | None:
        col = self._cols.get(colecao, {})
        doc = col.get(doc_id)
        return dict(doc) if doc is not None else None

    def listar(self, colecao: str) -> list[dict[str, Any]]:
        col = self._cols.get(colecao, {})
        return [dict(v) for v in col.values()]


class GoogleFirestoreStore:
    """
    Adaptador oficial Google Cloud Firestore (suporta FIRESTORE_EMULATOR_HOST
    automaticamente em dev/CI e banco nomeado oficina-execucao-{env}).
    """

    def __init__(self, config: Configuracoes) -> None:
        if config.firestore_emulator_host:
            os.environ["FIRESTORE_EMULATOR_HOST"] = config.firestore_emulator_host

        from google.auth.credentials import AnonymousCredentials
        from google.cloud import firestore

        emulator = os.getenv("FIRESTORE_EMULATOR_HOST", "")
        if emulator:
            self.client = firestore.Client(
                project=config.gcp_project_id,
                database=config.firestore_database,
                credentials=AnonymousCredentials(),
            )
        else:
            self.client = firestore.Client(
                project=config.gcp_project_id,
                database=config.firestore_database,
            )

    def ping(self) -> bool:
        doc_ref = self.client.collection("_health").document("ping")
        doc_ref.set({"status": "ok"})
        snap = doc_ref.get()
        return bool(snap.exists)

    def salvar(
        self, colecao: str, doc_id: str, dados: dict[str, Any]
    ) -> dict[str, Any]:
        self.client.collection(colecao).document(doc_id).set(dados)
        return dados

    def buscar(self, colecao: str, doc_id: str) -> dict[str, Any] | None:
        snap = self.client.collection(colecao).document(doc_id).get()
        if not snap.exists:
            return None
        return snap.to_dict()

    def listar(self, colecao: str) -> list[dict[str, Any]]:
        return [doc.to_dict() for doc in self.client.collection(colecao).stream()]


class ExecucaoRepositorioFirestore:
    def __init__(self, store: DocumentoStore) -> None:
        self.store = store

    def salvar_controle(self, controle: ControleSagaExecucao) -> dict[str, Any]:
        dados = {
            "saga_id": str(controle.saga_id),
            "os_id": str(controle.os_id),
            "rodada": controle.rodada,
            "estado_controle": controle.estado_controle.value,
            "cancelamento_event_id": (
                str(controle.cancelamento_event_id)
                if controle.cancelamento_event_id
                else None
            ),
            "motivo_cancelamento": controle.motivo_cancelamento,
            "version": controle.version,
        }
        return self.store.salvar("controle_saga", str(controle.saga_id), dados)

    def buscar_controle(self, saga_id: UUID) -> ControleSagaExecucao | None:
        dados = self.store.buscar("controle_saga", str(saga_id))
        if dados is None:
            return None
        return ControleSagaExecucao(
            saga_id=UUID(str(dados["saga_id"])),
            os_id=UUID(str(dados["os_id"])),
            rodada=int(dados["rodada"]),
            estado_controle=EstadoControleSaga(str(dados["estado_controle"])),
            cancelamento_event_id=(
                UUID(str(dados["cancelamento_event_id"]))
                if dados.get("cancelamento_event_id")
                else None
            ),
            motivo_cancelamento=dados.get("motivo_cancelamento"),
            version=int(dados.get("version", 1)),
        )

    def salvar_execucao(
        self, execucao_id: str, dados: dict[str, Any]
    ) -> dict[str, Any]:
        return self.store.salvar("execucoes", execucao_id, dados)

    def buscar_execucao(self, execucao_id: str) -> dict[str, Any] | None:
        return self.store.buscar("execucoes", execucao_id)

    def listar_execucoes(self) -> list[dict[str, Any]]:
        return self.store.listar("execucoes")
