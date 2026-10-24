"""The stdlib's http.server: a mixin giving a handler the standard request
metrics, a span per request (continuing the caller's trace from its
`traceparent` header), and one structlog access line per request in place of
the stdlib's stderr lines.

    class Handler(brick_observe.httpserver.Observed, BaseHTTPRequestHandler):
        routes = {"/", "/api/state", "/api/stream"}

`routes` are the paths worth their own series; any other path is counted as
`other`. The health and metrics paths (`skip`) are always known, logged at
debug and not traced. Serve `/metrics` with `metrics.exposition()`.
"""

from __future__ import annotations

import time
from urllib.parse import urlsplit

import structlog

from . import metrics

try:
    from opentelemetry import context as _context
    from opentelemetry import propagate as _propagate
    from opentelemetry import trace as _trace
except ImportError:
    _trace = None

SKIP = frozenset({"/health", "/ready", "/metrics"})

log = structlog.get_logger("brick_observe.http")


class Observed:
    routes: frozenset[str] | set[str] = frozenset()
    skip: frozenset[str] | set[str] = SKIP
    metrics_registry = None

    def _brick_observe_route(self) -> str:
        path = urlsplit(self.path).path
        return path if path in self.routes or path in self.skip else "other"

    def handle_one_request(self):
        # A keep-alive connection runs this once per request; a closed one
        # returns without parsing, leaving the last request's command behind.
        self.command = None
        self._brick_observe_status = None
        self._brick_observe_span = None
        self._brick_observe_token = None
        m = metrics.http_server(self.metrics_registry)
        started = time.perf_counter()
        m.in_flight.inc()
        failed = None
        try:
            super().handle_one_request()
        except BaseException as exc:
            failed = exc
            raise
        finally:
            m.in_flight.dec()
            if self.command:
                self._brick_observe_finish(m, time.perf_counter() - started, failed)

    def parse_request(self):
        ok = super().parse_request()
        if ok and _trace is not None and urlsplit(self.path).path not in self.skip:
            route = self._brick_observe_route()
            carrier = {k.lower(): v for k, v in self.headers.items()}
            self._brick_observe_span = _trace.get_tracer("brick_observe").start_span(
                f"{self.command} {route}",
                context=_propagate.extract(carrier),
                kind=_trace.SpanKind.SERVER,
                attributes={"http.request.method": self.command, "http.route": route,
                            "url.path": urlsplit(self.path).path},
            )
            self._brick_observe_token = _context.attach(_trace.set_span_in_context(self._brick_observe_span))
        return ok

    def send_response(self, code, message=None):
        self._brick_observe_status = code
        super().send_response(code, message)

    def _brick_observe_finish(self, m, seconds: float, failed: BaseException | None) -> None:
        status = self._brick_observe_status or (500 if failed else 0)
        route = self._brick_observe_route()
        path = urlsplit(self.path).path
        m.observe(route, self.command, status, seconds)
        # Still inside the request's span, so the access line carries its trace_id.
        emit = log.debug if path in self.skip else (log.warning if status >= 500 else log.info)
        emit("request", method=self.command, route=route, path=path, status=status,
             duration_ms=round(seconds * 1000, 1))
        span = self._brick_observe_span
        if span is not None:
            span.set_attribute("http.response.status_code", status)
            if failed is not None:
                span.record_exception(failed)
            if status >= 500 or failed is not None:
                span.set_status(_trace.Status(_trace.StatusCode.ERROR))
            span.end()
            _context.detach(self._brick_observe_token)

    def log_request(self, code="-", size="-"):
        pass  # the access line comes from _brick_observe_finish

    def log_error(self, format, *args):
        # send_error's "code 404, message ..."; the access line has the status already.
        log.debug(format % args if args else format)

    def log_message(self, format, *args):
        log.info(format % args if args else format)
