"""The standard Prometheus metrics, the same names in every service (the
`metrics` extra). Prometheus' own labels (`job`, `namespace`, `pod`) tell the
services apart, so the names carry no service prefix.

Served (HTTP middleware, `brick_observe.flask` and `brick_observe.httpserver`):

    http_server_requests_total{route, method, code}
    http_server_request_duration_seconds{route, method}      histogram
    http_server_requests_in_flight

Outbound (`instrument_requests`):

    http_client_requests_total{upstream, method, code}       code "error": no answer
    http_client_request_duration_seconds{upstream}           histogram

And from init(): `service_info{service, version} 1`.

Routes are the matched route (`/drive/<transid>`), never the raw path, and
anything unknown is `other`, so a scan of random paths makes no new series.
"""

from __future__ import annotations

import threading
import weakref
from dataclasses import dataclass
from typing import Any

from ._resource import Resource

try:
    import prometheus_client as _prom
except ImportError:
    _prom = None

_lock = threading.Lock()
# Made once per registry; weak keys, so a registry dropped (a test's) takes its
# collectors with it rather than leaving them to a new one at the same id.
_server: "weakref.WeakKeyDictionary[Any, HttpServer]" = weakref.WeakKeyDictionary()
_client: "weakref.WeakKeyDictionary[Any, HttpClient]" = weakref.WeakKeyDictionary()
_info: "weakref.WeakKeyDictionary[Any, Any]" = weakref.WeakKeyDictionary()


def available() -> bool:
    return _prom is not None


def _registry(registry):
    if _prom is None:
        raise RuntimeError("brick-observe's metrics need prometheus-client (the 'metrics' extra)")
    return registry if registry is not None else _prom.REGISTRY


@dataclass(frozen=True)
class HttpServer:
    requests: Any
    duration: Any
    in_flight: Any

    def observe(self, route: str, method: str, code: int | str, seconds: float) -> None:
        self.requests.labels(route, method, str(code)).inc()
        self.duration.labels(route, method).observe(seconds)


@dataclass(frozen=True)
class HttpClient:
    requests: Any
    duration: Any

    def observe(self, upstream: str, method: str, code: int | str, seconds: float) -> None:
        self.requests.labels(upstream, method, str(code)).inc()
        self.duration.labels(upstream).observe(seconds)


def http_server(registry=None) -> HttpServer:
    """The served-request metrics, made once per registry."""
    registry = _registry(registry)
    with _lock:
        if registry not in _server:
            _server[registry] = HttpServer(
                _prom.Counter("http_server_requests", "HTTP requests served.",
                              ["route", "method", "code"], registry=registry),
                _prom.Histogram("http_server_request_duration_seconds", "Time to answer an HTTP request.",
                                ["route", "method"], registry=registry),
                _prom.Gauge("http_server_requests_in_flight", "HTTP requests being answered now.",
                            registry=registry),
            )
        return _server[registry]


def http_client(registry=None) -> HttpClient:
    """The outbound-request metrics, made once per registry."""
    registry = _registry(registry)
    with _lock:
        if registry not in _client:
            _client[registry] = HttpClient(
                _prom.Counter("http_client_requests", "HTTP requests made to an upstream.",
                              ["upstream", "method", "code"], registry=registry),
                _prom.Histogram("http_client_request_duration_seconds", "Time an upstream took to answer.",
                                ["upstream"], registry=registry),
            )
        return _client[registry]


def service_info(resource: Resource, registry=None) -> None:
    """`service_info{service, version} 1`, for joining a version onto any series."""
    if _prom is None:
        return
    registry = _registry(registry)
    with _lock:
        if registry not in _info:
            _info[registry] = _prom.Gauge("service_info", "The service and its version (always 1).",
                                              ["service", "version"], registry=registry)
        _info[registry].labels(resource.service, resource.version).set(1)


def exposition(registry=None) -> tuple[bytes, str]:
    """The registry in Prometheus' text format, and its content type."""
    registry = _registry(registry)
    return _prom.generate_latest(registry), _prom.CONTENT_TYPE_LATEST


def serve(port: int, addr: str = "0.0.0.0", registry=None) -> None:
    """A /metrics server on its own thread, for a service with no HTTP of its own."""
    _prom.start_http_server(port, addr=addr, registry=_registry(registry))
