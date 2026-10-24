"""Traces: an OTLP exporter to the cluster's OpenTelemetry collector (then
Tempo), when `OTEL_EXPORTER_OTLP_ENDPOINT` is set and OpenTelemetry is
installed (the `tracing` extra). Otherwise spans cost nothing and record
nothing, so code can use `span()` everywhere, host apps included."""

from __future__ import annotations

import atexit
import contextlib
import os
from collections.abc import Iterator
from typing import Any

from ._resource import Resource

try:
    from opentelemetry import trace as _trace
except ImportError:
    _trace = None

TRACER = "brick_observe"
_provider = None


def available() -> bool:
    return _trace is not None


def configure(resource: Resource, *, endpoint: str | None = None):
    """The TracerProvider set as global, or None when tracing is off."""
    global _provider
    if _provider is not None:
        return _provider  # OpenTelemetry allows one global provider per process
    endpoint = endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint or _trace is None:
        return None
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource as OtelResource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        return None
    provider = TracerProvider(resource=OtelResource.create(resource.otel_attributes()))
    insecure = not endpoint.startswith("https://")
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=insecure)))
    _trace.set_tracer_provider(provider)
    # Spans still in the batch at exit would be lost; a Job exits right after its work.
    atexit.register(provider.shutdown)
    _provider = provider
    return provider


def flush(timeout_millis: int = 5000) -> None:
    """Send the spans waiting in the batch now (before a Job's pod exits)."""
    provider = _provider or (_trace.get_tracer_provider() if _trace else None)
    force_flush = getattr(provider, "force_flush", None)
    if force_flush:
        force_flush(timeout_millis)


@contextlib.contextmanager
def span(name: str, /, **attributes: Any) -> Iterator[Any]:
    """A span around a block (an exception in it is recorded and re-raised);
    without OpenTelemetry, just the block. Yields the span, or None."""
    if _trace is None:
        yield None
        return
    clean = {k: v for k, v in attributes.items() if v is not None}
    with _trace.get_tracer(TRACER).start_as_current_span(name, attributes=clean) as s:
        yield s
