import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests
from opentelemetry.instrumentation.requests import RequestsInstrumentor

import brick_observe
from brick_observe import http_client
from tests.conftest import sample


@pytest.fixture
def upstream():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(dict(self.headers))
            code = 503 if self.path == "/down" else 200
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.seen = seen
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def instrumented(registry, monkeypatch):
    # A fresh Session.send each test, so the patch (and its registry) is this test's.
    monkeypatch.setattr(requests.Session, "send", requests.Session.send)
    monkeypatch.setattr(http_client, "_upstreams", {})
    # OpenTelemetry's instrumentor is a process-wide singleton: it still thinks
    # it's installed after the last test's patch was undone.
    instrumentor = RequestsInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    brick_observe.instrument_requests({"127.0.0.1": "local"}, registry=registry)
    return registry


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_calls_are_counted_by_upstream_and_code(upstream, instrumented):
    base = f"http://127.0.0.1:{upstream.server_address[1]}"
    requests.get(f"{base}/ok", timeout=5)
    requests.get(f"{base}/down", timeout=5)
    assert sample(instrumented, "http_client_requests_total", {"upstream": "local", "method": "GET", "code": "200"}) == 1
    assert sample(instrumented, "http_client_requests_total", {"upstream": "local", "method": "GET", "code": "503"}) == 1
    assert sample(instrumented, "http_client_request_duration_seconds_count", {"upstream": "local"}) == 2


def test_no_answer_is_an_error_and_still_raises(instrumented):
    with pytest.raises(requests.ConnectionError):
        requests.get(f"http://127.0.0.1:{free_port()}/", timeout=2)
    assert sample(instrumented, "http_client_requests_total", {"upstream": "local", "method": "GET", "code": "error"}) == 1


def test_the_trace_goes_to_the_upstream(upstream, instrumented, spans):
    with brick_observe.span("caller") as s:
        requests.get(f"http://127.0.0.1:{upstream.server_address[1]}/ok", timeout=5)
    traceparent = {k.lower(): v for k, v in upstream.seen[0].items()}["traceparent"]
    assert format(s.get_span_context().trace_id, "032x") in traceparent


def test_hosts_map_to_upstreams_by_domain_and_others_are_other(monkeypatch):
    monkeypatch.setattr(http_client, "_upstreams", {"wigle.net": "wigle", "brick69": "pi"})
    assert http_client.upstream_of("https://api.wigle.net/api/v2/file/upload") == "wigle"
    assert http_client.upstream_of("http://BRICK69:2501/x") == "pi"
    assert http_client.upstream_of("https://example.com/") == "other"
    assert http_client.upstream_of("https://notwigle.net/") == "other"


def test_instrumenting_twice_wraps_once_and_adds_upstreams(instrumented):
    wrapped = requests.Session.send
    brick_observe.instrument_requests({"api.wigle.net": "wigle"})
    assert requests.Session.send is wrapped
    assert http_client.upstream_of("https://api.wigle.net/") == "wigle"
    assert http_client.upstream_of("http://127.0.0.1/") == "local"
