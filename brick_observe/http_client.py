"""Outbound HTTP through `requests`: metrics per upstream and a client span
per call (OpenTelemetry's requests instrumentation, which also sends the
`traceparent` header, so the upstream's spans join the trace).

    brick_observe.instrument_requests({"api.wigle.net": "wigle", "brick69": "pi"})

`upstreams` maps a host (or a domain: `wigle.net` matches `api.wigle.net`) to
the `upstream` label; any other host is `other`, so the label stays bounded.
Calling it again adds to the map.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from urllib.parse import urlsplit

from . import metrics

_upstreams: dict[str, str] = {}
_MARK = "_brick_observe"


def upstream_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    while host:
        if host in _upstreams:
            return _upstreams[host]
        _, _, host = host.partition(".")
    return "other"


def instrument_requests(upstreams: Mapping[str, str] | None = None, *, registry=None) -> None:
    import requests

    _upstreams.update({k.lower(): v for k, v in (upstreams or {}).items()})
    if getattr(requests.Session.send, _MARK, False):
        return
    try:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor
    except ImportError:
        pass
    else:
        instrumentor = RequestsInstrumentor()
        if not instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.instrument()

    m = metrics.http_client(registry)
    original = requests.Session.send

    def send(self, request, **kwargs):
        upstream = upstream_of(request.url)
        started = time.perf_counter()
        try:
            response = original(self, request, **kwargs)
        except Exception:
            m.observe(upstream, request.method or "GET", "error", time.perf_counter() - started)
            raise
        m.observe(upstream, request.method or "GET", response.status_code, time.perf_counter() - started)
        return response

    setattr(send, _MARK, True)
    send.__wrapped__ = original
    requests.Session.send = send
