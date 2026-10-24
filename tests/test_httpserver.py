import http.client
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import brick_observe.httpserver
from brick_observe import metrics
from tests.conftest import sample

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"


@pytest.fixture
def server(lines, registry):
    class Handler(brick_observe.httpserver.Observed, BaseHTTPRequestHandler):
        routes = {"/api/state"}
        protocol_version = "HTTP/1.1"  # keep-alive, as browsers and Prometheus use it
        metrics_registry = registry

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/boom":
                raise RuntimeError("broken")
            if path == "/metrics":
                body, ctype = metrics.exposition(registry)
            elif path in ("/api/state", "/health"):
                body, ctype = b"{}", "application/json"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.handle_error = lambda request, address: None  # the test checks what was recorded
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def get(server, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    try:
        conn.request("GET", path, headers=headers or {})
        r = conn.getresponse()
        r.read()
        return r.status
    except (http.client.RemoteDisconnected, ConnectionResetError):
        return None
    finally:
        conn.close()


def until(predicate):
    import time

    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    return predicate()


def test_known_routes_unknown_paths_and_status_codes(server, registry):
    assert get(server, "/api/state?x=1") == 200
    assert get(server, "/random/path") == 404
    assert until(lambda: sample(registry, "http_server_requests_total", {"route": "other", "method": "GET", "code": "404"}) == 1)
    assert sample(registry, "http_server_requests_total", {"route": "/api/state", "method": "GET", "code": "200"}) == 1
    assert until(lambda: sample(registry, "http_server_requests_in_flight") == 0)


def test_one_json_access_line_and_no_stderr_lines(server, lines, capfd):
    get(server, "/api/state")
    assert until(lambda: lines.events("request"))
    (access,) = lines.events("request")
    assert (access["method"], access["route"], access["status"], access["level"]) == ("GET", "/api/state", 200, "info")
    assert capfd.readouterr().err == ""


def test_a_handler_that_raises_is_a_counted_500(server, registry, lines, spans):
    get(server, "/boom")
    assert until(lambda: sample(registry, "http_server_requests_total", {"route": "other", "method": "GET", "code": "500"}) == 1)
    assert until(lambda: spans.get_finished_spans())
    (span,) = spans.get_finished_spans()
    assert not span.status.is_ok and span.events[0].name == "exception"


def test_the_callers_trace_is_continued(server, lines, spans):
    get(server, "/api/state", {"traceparent": f"00-{TRACE_ID}-00f067aa0ba902b7-01"})
    assert until(lambda: spans.get_finished_spans())
    (span,) = spans.get_finished_spans()
    assert format(span.context.trace_id, "032x") == TRACE_ID
    assert span.name == "GET /api/state"
    assert lines.events("request")[0]["trace_id"] == TRACE_ID


def test_health_is_not_traced_and_logged_at_debug(server, lines, spans):
    get(server, "/health")
    assert until(lambda: lines.events("request"))
    assert lines.events("request")[0]["level"] == "debug"
    assert spans.get_finished_spans() == ()


def test_keep_alive_counts_each_request_once(server, registry):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    for _ in range(2):
        conn.request("GET", "/api/state")
        conn.getresponse().read()
    conn.close()
    assert until(lambda: sample(registry, "http_server_requests_total", {"route": "/api/state", "method": "GET", "code": "200"}) == 2)
    assert until(lambda: sample(registry, "http_server_requests_in_flight") == 0)
