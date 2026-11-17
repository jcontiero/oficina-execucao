from copy import deepcopy
import os
import threading
from typing import Any, Callable, Protocol
from uuid import UUID

from src.config import Configuracoes
from src.execucao.dominio.entidades import ControleSagaExecucao, EstadoControleSaga


class TransacaoContexto(Protocol):
    def buscar(self, colecao: str, doc_id: str) -> dict[str, Any] | None: ...

    def salvar(
        self, colecao: str, doc_id: str, dados: dict[str, Any]
    ) -> dict[str, Any]: ...


class DocumentoStore(Protocol):
    def ping(self) -> bool: ...

    def salvar(
        self, colecao: str, doc_id: str, dados: dict[str, Any]
    ) -> dict[str, Any]: ...

    def buscar(self, colecao: str, doc_id: str) -> dict[str, Any] | None: ...

    def listar(self, colecao: str) -> list[dict[str, Any]]: ...

    def remover(self, colecao: str, doc_id: str) -> None: ...

    def executar_em_transacao[T](self, fn: Callable[[TransacaoContexto], T]) -> T: ...


class _InMemoryTx:
    def __init__(self, cols: dict[str, dict[str, dict[str, Any]]]) -> None:
        self._cols = cols

    def buscar(self, colecao: str, doc_id: str) -> dict[str, Any] | None:
        doc = self._cols.get(colecao, {}).get(doc_id)
        return deepcopy(doc) if doc is not None else None

    def salvar(
        self, colecao: str, doc_id: str, dados: dict[str, Any]
    ) -> dict[str, Any]:
        col = self._cols.setdefault(colecao, {})
        copia = deepcopy(dados)
        col[doc_id] = copia
        return copia


class InMemoryFirestoreStore:
    """
    Armazenamento em memória compatível com a estrutura de coleções do Firestore,
    suportando transações atômicas com rollback em memória para testes unitários.
    """

    def __init__(self) -> None:
        self._cols: dict[str, dict[str, dict[str, Any]]] = {}
        self._lock = threading.RLock()

    def ping(self) -> bool:
        return True

    def salvar(
        self, colecao: str, doc_id: str, dados: dict[str, Any]
    ) -> dict[str, Any]:
        with self._lock:
            col = self._cols.setdefault(colecao, {})
            copia = deepcopy(dados)
            col[doc_id] = copia
            return copia

    def buscar(self, colecao: str, doc_id: str) -> dict[str, Any] | None:
        with self._lock:
            col = self._cols.get(colecao, {})
            doc = col.get(doc_id)
            return deepcopy(doc) if doc is not None else None

    def listar(self, colecao: str) -> list[dict[str, Any]]:
        with self._lock:
            col = self._cols.get(colecao, {})
            return [deepcopy(v) for v in col.values()]

    def remover(self, colecao: str, doc_id: str) -> None:
        with self._lock:
            col = self._cols.get(colecao, {})
            col.pop(doc_id, None)

    def executar_em_transacao[T](self, fn: Callable[[TransacaoContexto], T]) -> T:
        with self._lock:
            snapshot = deepcopy(self._cols)
            try:
                tx = _InMemoryTx(self._cols)
                return fn(tx)
            except Exception:
                self._cols = snapshot
                raise


class _GoogleFirestoreTx:
    def __init__(self, client: Any, transaction: Any) -> None:
        self._client = client
        self._tx = transaction
        self._reads: dict[tuple[str, str], dict[str, Any] | None] = {}
        self._pending_writes: list[tuple[str, str, dict[str, Any]]] = []

    def buscar(self, colecao: str, doc_id: str) -> dict[str, Any] | None:
        chave = (colecao, doc_id)
        if chave in self._reads:
            return deepcopy(self._reads[chave])
        ref = self._client.collection(colecao).document(doc_id)
        snap = ref.get(transaction=self._tx)
        val = snap.to_dict() if snap.exists else None
        self._reads[chave] = deepcopy(val)
        return val

    def salvar(
        self, colecao: str, doc_id: str, dados: dict[str, Any]
    ) -> dict[str, Any]:
        copia = deepcopy(dados)
        self._reads[(colecao, doc_id)] = copia
        self._pending_writes.append((colecao, doc_id, copia))
        return copia

    def flush(self) -> None:
        for colecao, doc_id, dados in self._pending_writes:
            ref = self._client.collection(colecao).document(doc_id)
            self._tx.set(ref, dados)


class GoogleFirestoreStore:
    """
    Adaptador oficial Google Cloud Firestore com suporte a transações atômicas
    (@firestore.transactional).
    - Em dev/CI com FIRESTORE_EMULATOR_HOST: conecta em `(default)`.
    - Em GCP (homolog/production): conecta em `oficina-execucao-{env}`.
    """

    def __init__(self, config: Configuracoes) -> None:
        if config.firestore_emulator_host:
            os.environ["FIRESTORE_EMULATOR_HOST"] = config.firestore_emulator_host

        from google.auth.credentials import AnonymousCredentials
        from google.cloud import firestore

        self._firestore = firestore
        emulator = os.getenv("FIRESTORE_EMULATOR_HOST", "")
        if emulator:
            self.client = firestore.Client(
                project=config.gcp_project_id,
                database="(default)",
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

    def remover(self, colecao: str, doc_id: str) -> None:
        self.client.collection(colecao).document(doc_id).delete()

    def executar_em_transacao[T](self, fn: Callable[[TransacaoContexto], T]) -> T:
        transaction = self.client.transaction()

        @self._firestore.transactional
        def _run_in_tx(tx_obj: Any) -> T:
            ctx = _GoogleFirestoreTx(self.client, tx_obj)
            res = fn(ctx)
            ctx.flush()
            return res

        return _run_in_tx(transaction)


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

    def salvar_reserva(self, saga_id: UUID, dados: dict[str, Any]) -> dict[str, Any]:
        return self.store.salvar("reservas", str(saga_id), dados)

    def buscar_reserva(self, saga_id: UUID) -> dict[str, Any] | None:
        return self.store.buscar("reservas", str(saga_id))
