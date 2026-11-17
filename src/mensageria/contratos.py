from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

PRODUTORES_PERMITIDOS = {"oficina-os", "oficina-billing", "oficina-execucao"}

COMANDOS_EXECUCAO_V1: dict[str, set[str]] = {
    "SolicitarDiagnostico.v1": {
        "rodada",
        "cliente_id",
        "veiculo_id",
        "descricao_problema",
    },
    "ReservarPecas.v1": {
        "rodada",
        "reserva_id",
        "orcamento_id",
        "orcamento_versao",
        "itens_peca",
    },
    "EnfileirarExecucao.v1": {
        "rodada",
        "reserva_id",
        "orcamento_id",
        "pagamento_id",
        "itens_servico",
        "itens_peca",
    },
    "CancelarExecucao.v1": {"rodada", "motivo", "compensacao_id"},
    "LiberarReserva.v1": {
        "rodada",
        "motivo",
        "compensacao_id",
        "reserva_id",
        "execucao_cancelada_event_id",
    },
}

CAMPOS_SENSIVEIS_PROIBIDOS = {
    "cpf",
    "senha",
    "password",
    "secret",
    "access_token",
    "token",
}


class EnvelopeMensagemV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    event_type: str
    schema_version: int = Field(default=1)
    occurred_at: datetime
    producer: str
    saga_id: UUID
    os_id: UUID
    correlation_id: UUID
    causation_id: UUID
    aggregate_version: int = Field(ge=1)
    payload: dict[str, Any]

    @field_validator("schema_version")
    @classmethod
    def _validar_versao(cls, valor: int) -> int:
        if valor != 1:
            raise ValueError("schema_version deve ser 1")
        return valor

    @field_validator("producer")
    @classmethod
    def _validar_produtor(cls, valor: str) -> str:
        if valor not in PRODUTORES_PERMITIDOS:
            raise ValueError(f"Produtor não permitido: {valor}")
        return valor


def validar_comando_execucao_v1(dados: dict[str, Any]) -> EnvelopeMensagemV1:
    """
    Valida comandos recebidos pelo serviço de Execução, exigindo obrigatoriamente
    a comprovação da barreira de cancelamento (execucao_cancelada_event_id) antes
    de liberar uma reserva de peças (LiberarReserva.v1).
    """
    envelope = EnvelopeMensagemV1.model_validate(dados)
    if envelope.producer != "oficina-os":
        raise ValueError("Comandos de execução só podem ser produzidos por oficina-os")
    if envelope.event_type not in COMANDOS_EXECUCAO_V1:
        raise ValueError(f"Comando desconhecido para Execução: {envelope.event_type}")

    obrigatorios = COMANDOS_EXECUCAO_V1[envelope.event_type]
    faltantes = obrigatorios - set(envelope.payload.keys())
    if faltantes:
        raise ValueError(
            f"Payload de {envelope.event_type} sem campos obrigatórios: {sorted(faltantes)}"
        )

    if envelope.event_type == "LiberarReserva.v1":
        barreira_id = envelope.payload.get("execucao_cancelada_event_id")
        if not barreira_id:
            raise ValueError(
                "LiberarReserva.v1 rejeitado: exige execucao_cancelada_event_id "
                "comprovando o cancelamento prévio da execução"
            )
        UUID(str(barreira_id))

    proibidos = set(envelope.payload.keys()) & CAMPOS_SENSIVEIS_PROIBIDOS
    if proibidos:
        raise ValueError(
            f"Payload contém campos sensíveis proibidos: {sorted(proibidos)}"
        )

    return envelope
