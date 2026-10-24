"""Flask: one call gives an app the standard request metrics, a span per
request (OpenTelemetry's Flask instrumentation, when installed), one JSON
access line per request through structlog in place of werkzeug's, and
/metrics.

    app = Flask(__name__)
    brick_observe.flask.instrument(app)

The health and metrics paths (`skip`) are counted but logged at debug and not
traced, so probes and scrapes don't drown the logs or Tempo.
"""

from __future__ import annotations

import logging
import time

import structlog
from flask import Flask, Response, g, request

from . import metrics

SKIP = ("/health", "/ready", "/metrics")

log = structlog.get_logger("brick_observe.http")


def instrument(app: Flask, *, skip: tuple[str, ...] = SKIP, metrics_path: str | None = "/metrics",
               registry=None) -> Flask:
    m = metrics.http_server(registry)
    # Our access line replaces werkzeug's; its warnings and errors still show.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    _trace(app, skip)

    @app.before_request
    def _brick_observe_start():
        g._brick_observe_started = time.perf_counter()
        m.in_flight.inc()

    @app.after_request
    def _brick_observe_record(response):
        started = g.pop("_brick_observe_started", None)
        if started is None:
            return response
        m.in_flight.dec()
        seconds = time.perf_counter() - started
        route = request.url_rule.rule if request.url_rule else "other"
        m.observe(route, request.method, response.status_code, seconds)
        emit = log.debug if request.path in skip else (log.warning if response.status_code >= 500 else log.info)
        emit("request", method=request.method, route=route, path=request.path,
             status=response.status_code, duration_ms=round(seconds * 1000, 1))
        return response

    @app.teardown_request
    def _brick_observe_teardown(_exc):
        # after_request didn't run (an error in another after_request hook).
        if g.pop("_brick_observe_started", None) is not None:
            m.in_flight.dec()

    if metrics_path and not any(r.rule == metrics_path for r in app.url_map.iter_rules()):
        def _metrics():
            body, content_type = metrics.exposition(registry)
            return Response(body, content_type=content_type)

        app.add_url_rule(metrics_path, "brick_observe_metrics", _metrics)
    return app


def _trace(app: Flask, skip: tuple[str, ...]) -> None:
    try:
        from opentelemetry.instrumentation.flask import FlaskInstrumentor
    except ImportError:
        return
    if getattr(app, "_is_instrumented_by_opentelemetry", False):
        return
    excluded = ",".join(f"{p}$" for p in skip)
    FlaskInstrumentor().instrument_app(app, excluded_urls=excluded)
