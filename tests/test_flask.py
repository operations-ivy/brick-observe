import pytest
from flask import Flask

import brick_observe.flask
from tests.conftest import sample


@pytest.fixture
def app(lines, registry):
    app = Flask("t")

    @app.route("/drive/<transid>")
    def drive(transid):
        return {"transid": transid}

    @app.route("/health")
    def health():
        return "ok"

    @app.route("/boom")
    def boom():
        raise RuntimeError("broken")

    brick_observe.flask.instrument(app, registry=registry)
    return app


def test_requests_are_counted_by_matched_route_not_path(app, registry):
    c = app.test_client()
    c.get("/drive/1")
    c.get("/drive/2")
    c.get("/nope")
    assert sample(registry, "http_server_requests_total", {"route": "/drive/<transid>", "method": "GET", "code": "200"}) == 2
    assert sample(registry, "http_server_requests_total", {"route": "other", "method": "GET", "code": "404"}) == 1
    assert sample(registry, "http_server_request_duration_seconds_count", {"route": "/drive/<transid>", "method": "GET"}) == 2
    assert sample(registry, "http_server_requests_in_flight") == 0


def test_an_unhandled_error_is_a_counted_500_and_a_warning(app, registry, lines):
    app.test_client().get("/boom")
    assert sample(registry, "http_server_requests_total", {"route": "/boom", "method": "GET", "code": "500"}) == 1
    (access,) = [line for line in lines.events("request") if line["path"] == "/boom"]
    assert access["level"] == "warning" and access["status"] == 500
    assert sample(registry, "http_server_requests_in_flight") == 0


def test_one_json_access_line_per_request_instead_of_werkzeugs(app, lines):
    app.test_client().get("/drive/7")
    (access,) = lines.events("request")
    assert access["level"] == "info" and access["logger"] == "brick_observe.http"
    assert (access["method"], access["route"], access["path"], access["status"]) == ("GET", "/drive/<transid>", "/drive/7", 200)
    assert access["duration_ms"] >= 0
    assert all(line["logger"] != "werkzeug" for line in lines.all())


def test_health_checks_are_counted_but_logged_at_debug_and_not_traced(app, registry, lines, spans):
    app.test_client().get("/health")
    assert sample(registry, "http_server_requests_total", {"route": "/health", "method": "GET", "code": "200"}) == 1
    (access,) = lines.events("request")
    assert access["level"] == "debug"
    assert spans.get_finished_spans() == ()


def test_a_request_is_a_span_and_its_access_line_is_in_the_trace(app, lines, spans):
    app.test_client().get("/drive/9")
    (span,) = spans.get_finished_spans()
    assert span.name == "GET /drive/<transid>"
    (access,) = lines.events("request")
    assert access["trace_id"] == format(span.context.trace_id, "032x")


def test_metrics_are_served(app):
    r = app.test_client().get("/metrics")
    assert r.status_code == 200
    assert b"http_server_requests_total" in r.data
    assert r.content_type.startswith("text/plain")


def test_an_apps_own_metrics_route_is_kept(lines, registry):
    app = Flask("own")

    @app.route("/metrics")
    def mine():
        return "mine"

    brick_observe.flask.instrument(app, registry=registry)
    assert app.test_client().get("/metrics").data == b"mine"
