"""Shared fixtures: an in-memory span exporter (OpenTelemetry allows one
global tracer provider per process, so it's set once here), logs captured as
parsed JSON, and a fresh Prometheus registry per test."""

from __future__ import annotations

import io
import json
import logging

import prometheus_client
import pytest
import structlog
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import brick_observe

EXPORTER = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
trace.set_tracer_provider(_provider)


class Lines:
    """What the service logged, one parsed JSON object per line."""

    def __init__(self):
        self.stream = io.StringIO()

    @property
    def raw(self) -> list[str]:
        return [line for line in self.stream.getvalue().splitlines() if line]

    def all(self) -> list[dict]:
        return [json.loads(line) for line in self.raw]

    def events(self, event: str) -> list[dict]:
        return [line for line in self.all() if line.get("event") == event]


@pytest.fixture
def spans():
    EXPORTER.clear()
    yield EXPORTER
    EXPORTER.clear()


@pytest.fixture
def lines(monkeypatch):
    """init() for a service "test", JSON at debug, into a buffer."""
    for name in ("POD_NAME", "POD_NAMESPACE", "NODE_NAME", "SERVICE_VERSION", "LOG_FORMAT", "LOG_LEVEL",
                 "OTEL_EXPORTER_OTLP_ENDPOINT", "PUSHGATEWAY_URL"):
        monkeypatch.delenv(name, raising=False)
    out = Lines()
    brick_observe.init("test", json=True, level="debug", stream=out.stream)
    yield out
    structlog.reset_defaults()
    logging.getLogger().handlers.clear()


@pytest.fixture
def registry():
    return prometheus_client.CollectorRegistry()


def sample(registry, name: str, labels: dict | None = None) -> float | None:
    return registry.get_sample_value(name, labels or {})
