from uuid import uuid4

import pytest
from pydantic import ValidationError
from src.execucao.apresentacao.rotas import DiagnosticoRequest


@pytest.mark.parametrize(
    "item",
    [
        {"servico_id": None, "preco_unitario_centavos": 100},
        {"servico_id": str(uuid4()), "quantidade": 0, "preco_unitario_centavos": 100},
    ],
)
def test_diagnostico_rejeita_item_invalido_antes_da_outbox(item):
    with pytest.raises(ValidationError):
        DiagnosticoRequest(
            os_id=uuid4(), saga_id=uuid4(), laudo="Revisão", itens_servico=[item]
        )
