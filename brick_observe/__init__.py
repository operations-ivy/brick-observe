"""brick-observe: logs, metrics and traces the same way in every brick
homelab service, from one call.

    import brick_observe

    brick_observe.init("wigle-console")
    log = brick_observe.get_logger(__name__)

    app = Flask(__name__)
    brick_observe.flask.instrument(app)          # metrics, spans, access lines, /metrics
    brick_observe.instrument_requests({"api.wigle.net": "wigle"})

    with brick_observe.span("refresh radar", drives=12):
        ...

    with brick_observe.job("wigle-sync") as run:  # a CronJob: pushed on exit
        ...

init() sets up structured logs (JSON with LOG_FORMAT=json), OTLP tracing
(when OTEL_EXPORTER_OTLP_ENDPOINT is set and OpenTelemetry is installed), and
`service_info` (when prometheus-client is installed). Without the optional
packages, logs and span() still work: a host app on Debian's system Python
needs only python3-structlog.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TextIO

import structlog

from . import logs, metrics, tracing
from ._resource import Resource, detect
from .jobs import Run, job
from .tracing import span

__version__ = "0.1.0"
__all__ = ["Observer", "Resource", "Run", "get_logger", "init", "instrument_requests", "job", "span"]


@dataclass(frozen=True)
class Observer:
    resource: Resource
    tracer_provider: Any | None

    @property
    def tracing(self) -> bool:
        return self.tracer_provider is not None


def init(service: str, *, version: str | None = None, json: bool | None = None, level: str | None = None,
         tracing_enabled: bool = True, stream: TextIO | None = None) -> Observer:
    """Set up logs, traces and the service's info metric. Call once, first
    thing (before Flask's app is made, so its instrumentation sees the tracer).

    `version` defaults to SERVICE_VERSION, `json` to LOG_FORMAT=json, `level`
    to LOG_LEVEL (info)."""
    resource = detect(service, version)
    logs.configure(resource, json=json, level=level, stream=stream)
    provider = tracing.configure(resource) if tracing_enabled else None
    metrics.service_info(resource)
    structlog.get_logger("brick_observe").debug(
        "observability ready", version=resource.version, tracing=provider is not None,
        metrics=metrics.available(), namespace=resource.namespace, pod=resource.pod, node=resource.node)
    return Observer(resource, provider)


def get_logger(name: str | None = None, **initial: Any):
    """A structlog logger; `name` becomes the line's `logger` field."""
    return structlog.get_logger(name, **initial)


def instrument_requests(upstreams=None, **kwargs) -> None:
    from .http_client import instrument_requests as _instrument

    _instrument(upstreams, **kwargs)


def __getattr__(name: str):
    # brick_observe.flask and .httpserver import Flask / OpenTelemetry pieces,
    # so they load only when used.
    if name in ("flask", "httpserver", "k8s", "http_client"):
        import importlib

        return importlib.import_module(f".{name}", __name__)
    raise AttributeError(name)
