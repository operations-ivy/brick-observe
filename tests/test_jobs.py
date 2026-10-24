import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from prometheus_client.parser import text_string_to_metric_families

import brick_observe


@pytest.fixture
def gateway():
    """A stand-in Pushgateway: records each push's method, path and samples."""
    pushes = []

    class Handler(BaseHTTPRequestHandler):
        def _push(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            samples = {s.name + (str(sorted(s.labels.items())) if s.labels else ""): s.value
                       for f in text_string_to_metric_families(body) for s in f.samples}
            pushes.append((self.command, self.path, samples))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_POST = do_PUT = _push

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}"
    srv.pushes = pushes
    yield srv
    srv.shutdown()
    srv.server_close()


def test_a_successful_run_pushes_its_result_and_last_success(gateway, lines):
    with brick_observe.job("wigle-sync", gateway=gateway.url) as run:
        run.gauge("wigle_sync_files_uploaded", "Files uploaded in the last run", 3)
    ((method, path, samples),) = gateway.pushes
    assert method == "POST" and path == "/metrics/job/wigle-sync"  # pushadd: other metrics kept
    assert samples["job_succeeded"] == 1
    assert samples["job_last_success_timestamp_seconds"] == samples["job_last_run_timestamp_seconds"]
    assert samples["job_duration_seconds"] >= 0
    assert samples["wigle_sync_files_uploaded"] == 3
    (finished,) = lines.events("job finished")
    assert finished["succeeded"] is True and finished["job"] == "wigle-sync"


def test_a_run_that_raises_fails_without_a_last_success_and_reraises(gateway, lines):
    with pytest.raises(RuntimeError):
        with brick_observe.job("importer", gateway=gateway.url):
            raise RuntimeError("db down")
    ((_, _, samples),) = gateway.pushes
    assert samples["job_succeeded"] == 0
    assert "job_last_success_timestamp_seconds" not in samples
    (failed,) = lines.events("job failed")
    assert "RuntimeError: db down" in failed["exception"]
    assert lines.events("job finished")[0]["level"] == "warning"


def test_a_failure_logged_by_the_job_is_not_logged_again_on_the_way_out(gateway, lines):
    try:
        with brick_observe.job("importer", gateway=gateway.url):
            raise RuntimeError("once")
    except RuntimeError as exc:
        sys.excepthook(RuntimeError, exc, exc.__traceback__)
    assert len(lines.events("job failed")) == 1
    assert lines.events("uncaught exception") == []


def test_fail_marks_a_run_failed_without_raising(gateway, lines):
    with brick_observe.job("wigle-sync", gateway=gateway.url) as run:
        run.fail("2 files failed")
    ((_, _, samples),) = gateway.pushes
    assert samples["job_succeeded"] == 0
    assert lines.events("job finished")[0]["reason"] == "2 files failed"


@pytest.mark.parametrize("code, ok", [(0, 1), (None, 1), (2, 0)])
def test_system_exit_zero_is_success(gateway, lines, code, ok):
    with pytest.raises(SystemExit):
        with brick_observe.job("j", gateway=gateway.url):
            sys.exit(code)
    assert gateway.pushes[0][2]["job_succeeded"] == ok


def test_labelled_gauges_and_the_grouping_key(gateway, lines):
    with brick_observe.job("wigle-sync", gateway=gateway.url, grouping_key={"pi": "brick69"}) as run:
        run.gauge("detections", "Devices seen, by kind", 2, {"kind": "flock"})
        run.gauge("detections", "Devices seen, by kind", 0, {"kind": "axon"})
        with pytest.raises(ValueError):
            run.gauge("detections", "Devices seen, by kind", 1, {"other": "x"})
    ((_, path, samples),) = gateway.pushes
    assert path == "/metrics/job/wigle-sync/pi/brick69"
    assert samples["detections[('kind', 'flock')]"] == 2 and samples["detections[('kind', 'axon')]"] == 0


def test_no_gateway_pushes_nothing(monkeypatch, lines):
    monkeypatch.delenv("PUSHGATEWAY_URL", raising=False)
    with brick_observe.job("local"):
        pass
    assert lines.events("job finished")[0]["succeeded"] is True


def test_a_failed_push_is_logged_not_raised(lines):
    with brick_observe.job("j", gateway="http://127.0.0.1:9"):
        pass
    (warning,) = lines.events("failed to push the job's metrics")
    assert warning["level"] == "warning"


def test_the_run_is_one_span_with_its_lines_in_it(gateway, lines, spans):
    with brick_observe.job("j", gateway=gateway.url):
        brick_observe.get_logger().info("working")
    (span,) = [s for s in spans.get_finished_spans() if s.name == "job j"]
    assert lines.events("working")[0]["trace_id"] == format(span.context.trace_id, "032x")


def test_a_failed_run_is_an_error_span(gateway, lines, spans):
    with brick_observe.job("j", gateway=gateway.url) as run:
        run.fail("partial")
    (span,) = [s for s in spans.get_finished_spans() if s.name == "job j"]
    assert not span.status.is_ok and span.status.description == "partial"
