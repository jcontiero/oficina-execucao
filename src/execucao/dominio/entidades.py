from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID


class EstadoControleSaga(str, Enum):
    ATIVO = "ATIVO"
    EM_FILA = "EM_FILA"
    REPARO_INICIADO = "REPARO_INICIADO"
    CONCLUIDO = "CONCLUIDO"
    CANCELADO_TOMBSTONE = "CANCELADO_TOMBSTONE"


class StatusReserva(str, Enum):
    ATIVA = "ATIVA"
    CONSUMIDA = "CONSUMIDA"
    LIBERADA = "LIBERADA"


class StatusExecucao(str, Enum):
    AGUARDANDO_EXECUCAO = "AGUARDANDO_EXECUCAO"
    EM_EXECUCAO = "EM_EXECUCAO"
    CONCLUIDA = "CONCLUIDA"
    CANCELADA = "CANCELADA"


@dataclass
class ControleSagaExecucao:
    """
    Documento âncora de controle transacional por saga_id no Firestore
    (coleção controle_saga/{saga_id}).
    Disputado atomicamente por diagnosticar, enfileirar, iniciar_reparo e CancelarExecucao.v1.
    """

    saga_id: UUID
    os_id: UUID
    rodada: int
    estado_controle: EstadoControleSaga = EstadoControleSaga.ATIVO
    cancelamento_event_id: UUID | None = None
    motivo_cancelamento: str | None = None
    iniciado_em: datetime | None = None
    cancelado_em: datetime | None = None
    version: int = 1

    def cancelar_com_tombstone(
        self, *, cancelamento_event_id: UUID, motivo: str
    ) -> dict[str, Any]:
        """
        Aplica a barreira de cancelamento:
        - Se o reparo já foi iniciado (REPARO_INICIADO ou CONCLUIDO), recusa o cancelamento
          automático (REPARO_INICIADO) para impedir estorno ou reposição indevida de peças.
        - Caso contrário, grava o tombstone CANCELADO_TOMBSTONE (idempotente).
        """
        if self.estado_controle in {
            EstadoControleSaga.REPARO_INICIADO,
            EstadoControleSaga.CONCLUIDO,
        }:
            return {
                "event_type": "ExecucaoCancelamentoRecusado.v1",
                "resultado": "RECUSADO",
                "motivo": "REPARO_INICIADO",
                "tombstone_persistido": False,
            }

        if self.estado_controle != EstadoControleSaga.CANCELADO_TOMBSTONE:
            self.estado_controle = EstadoControleSaga.CANCELADO_TOMBSTONE
            self.cancelamento_event_id = cancelamento_event_id
            self.motivo_cancelamento = motivo
            self.cancelado_em = datetime.now(timezone.utc)
            self.version += 1

        return {
            "event_type": "ExecucaoCancelada.v1",
            "resultado": "CANCELADA",
            "motivo": motivo,
            "tombstone_persistido": True,
            "cancelamento_event_id": str(self.cancelamento_event_id),
        }

    def iniciar_reparo(self) -> None:
        if self.estado_controle == EstadoControleSaga.CANCELADO_TOMBSTONE:
            raise ValueError(
                "Não é permitido iniciar reparo: saga cancelada por tombstone"
            )
        if self.estado_controle == EstadoControleSaga.CONCLUIDO:
            raise ValueError("Reparo já concluído")
        if self.estado_controle == EstadoControleSaga.REPARO_INICIADO:
            raise ValueError("Reparo já iniciado")
        if self.estado_controle != EstadoControleSaga.EM_FILA:
            raise ValueError("Execução ainda não enfileirada após pagamento autorizado")
        self.estado_controle = EstadoControleSaga.REPARO_INICIADO
        self.iniciado_em = datetime.now(timezone.utc)
        self.version += 1


@dataclass
class ReservaEstoque:
    saga_id: UUID
    os_id: UUID
    rodada: int
    itens: list[dict[str, Any]] = field(default_factory=list)
    status: StatusReserva = StatusReserva.ATIVA

    def liberar_apos_barreira(
        self, *, controle: ControleSagaExecucao, execucao_cancelada_event_id: UUID
    ) -> dict[str, str]:
        """
        Regra crítica da Fase 4: somente libera a reserva se o documento de controle
        da mesma Saga estiver comprovadamente em CANCELADO_TOMBSTONE e a reserva não
        tiver sido consumida no início do reparo.
        """
        if controle.estado_controle != EstadoControleSaga.CANCELADO_TOMBSTONE:
            raise ValueError(
                "Barreira violada: controle_saga não está em CANCELADO_TOMBSTONE"
            )
        if (
            controle.cancelamento_event_id is not None
            and controle.cancelamento_event_id != execucao_cancelada_event_id
        ):
            raise ValueError(
                "execucao_cancelada_event_id não corresponde ao tombstone registrado"
            )
        if self.status == StatusReserva.CONSUMIDA:
            return {
                "event_type": "ReservaLiberacaoRecusada.v1",
                "resultado": "RECUSADA",
                "motivo": "RESERVA_CONSUMIDA",
                "repor_estoque": False,
            }
        if self.status == StatusReserva.LIBERADA:
            return {
                "event_type": "ReservaLiberada.v1",
                "resultado": "JA_LIBERADA",
                "motivo": controle.motivo_cancelamento or "CANCELADA",
                "repor_estoque": False,
            }
        self.status = StatusReserva.LIBERADA
        return {
            "event_type": "ReservaLiberada.v1",
            "resultado": "LIBERADA",
            "motivo": controle.motivo_cancelamento or "CANCELADA",
            "repor_estoque": True,
        }
