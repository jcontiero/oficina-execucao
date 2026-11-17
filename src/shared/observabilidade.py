from contextlib import contextmanager
import logging
import os
from contextvars import ContextVar

from pythonjsonlogger import jsonlogger

correlacao_ctx: ContextVar[dict[str, str] | None] = ContextVar(
    "correlacao_ctx",
    default=None,
)


class FiltroCorrelacaoObservabilidade(logging.Filter):
    """
    Injeta campos de rastreamento distribuído (dd.trace_id, dd.span_id) e de negócio
    (correlation_id, saga_id, os_id, event_id) nos logs JSON sem exigir que o
    Datadog Agent esteja ativo localmente.
    """

    def __init__(self, service: str, env: str, version: str) -> None:
        super().__init__()
        self.service = service
        self.env = env
        self.version = version

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = correlacao_ctx.get() or {}
        setattr(record, "dd.service", self.service)
        setattr(record, "dd.env", self.env)
        setattr(record, "dd.version", self.version)
        if not hasattr(record, "dd.trace_id"):
            setattr(record, "dd.trace_id", ctx.get("trace_id", "0"))
        if not hasattr(record, "dd.span_id"):
            setattr(record, "dd.span_id", ctx.get("span_id", "0"))
        record.correlation_id = ctx.get("correlation_id", "")
        record.saga_id = ctx.get("saga_id", "")
        record.os_id = ctx.get("os_id", "")
        record.event_id = ctx.get("event_id", "")
        return True


def configurar_observabilidade(
    *,
    service: str = "oficina-billing",
    env: str = "homolog",
    version: str = "0.1.0",
    trace_enabled: bool | None = None,
) -> logging.Logger:
    if trace_enabled is None:
        trace_enabled = os.getenv("DD_TRACE_ENABLED", "false").lower() in {
            "1",
            "true",
            "yes",
        }

    if trace_enabled:
        try:
            from ddtrace import patch_all

            patch_all()
        except Exception:
            pass

    logger = logging.getLogger(service)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    handler = logging.StreamHandler()
    formatter = jsonlogger.JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s "
        "%(dd.service)s %(dd.env)s %(dd.version)s "
        "%(dd.trace_id)s %(dd.span_id)s "
        "%(correlation_id)s %(saga_id)s %(os_id)s %(event_id)s",
        rename_fields={"levelname": "level", "asctime": "timestamp"},
    )
    handler.setFormatter(formatter)
    handler.addFilter(
        FiltroCorrelacaoObservabilidade(service=service, env=env, version=version)
    )
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def traceparent_atual(recebido: str | None = None) -> str | None:
    from ddtrace import tracer
    from ddtrace.propagation.http import HTTPPropagator

    headers = {}
    contexto = tracer.current_trace_context()
    if contexto is not None:
        HTTPPropagator.inject(contexto, headers)
    return headers.get("traceparent") or recebido


@contextmanager
def consumir_contexto(dados, traceparent):
    from ddtrace import tracer
    from ddtrace.propagation.http import HTTPPropagator

    anterior = tracer.context_provider.active()
    contexto = {
        **(correlacao_ctx.get() or {}),
        **{
            k: str(dados.get(k, ""))
            for k in ("correlation_id", "saga_id", "os_id", "event_id")
        },
    }
    token = correlacao_ctx.set(contexto)
    try:
        if os.getenv("DD_TRACE_ENABLED", "false").lower() != "true":
            yield traceparent
            return
        if traceparent:
            tracer.context_provider.activate(
                HTTPPropagator.extract({"traceparent": traceparent})
            )
        with tracer.trace(
            "pubsub.consume", resource=dados.get("event_type", "mensagem")
        ) as span:
            contexto.update(
                {
                    "trace_id": str(span.trace_id),
                    "span_id": str(span.span_id),
                    "dd.trace_id": str(span.trace_id),
                    "dd.span_id": str(span.span_id),
                }
            )
            for chave, valor in contexto.items():
                span.set_tag(chave, valor)
            yield traceparent_atual(traceparent)
    finally:
        correlacao_ctx.reset(token)
        tracer.context_provider.activate(anterior)
