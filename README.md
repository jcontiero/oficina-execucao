# Oficina Execução Service (`oficina-execucao`)

Microsserviço de Execução, Diagnóstico, Catálogo de Serviços e Estoque de Peças com persistência NoSQL (**Google Cloud Firestore Native Mode**) da Fase 4 do Tech Challenge.

## Responsabilidades
- Gestão de diagnósticos, fila de execução, início e conclusão de reparos.
- Controle transacional por `saga_id` (`controle_saga/{saga_id}`) disputado entre `iniciar_reparo` e `CancelarExecucao.v1`.
- Aplicação da **barreira de cancelamento**:
  - Se o reparo ainda não iniciou, grava *tombstone* (`CANCELADO_TOMBSTONE`) e emite `ExecucaoCancelada.v1`.
  - Se o reparo já iniciou (`REPARO_INICIADO`), recusa o cancelamento automático (`ExecucaoCancelamentoRecusado.v1`) impedindo estorno ou liberação de peças já consumidas.
  - Só permite `LiberarReserva.v1` após comprovação do *tombstone* (`execucao_cancelada_event_id`).

## Execução Local e Testes
```bash
uv sync --all-extras
uv run ruff check src tests
uv run black --check src tests
uv run pytest tests/unit tests/integration tests/contract --cov=src --cov-fail-under=80
```
