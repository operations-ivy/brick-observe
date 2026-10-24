import io
import json
import logging
import subprocess
import sys
import threading
import warnings

import pytest
import structlog

import brick_observe
from brick_observe import logs

REQUIRED = {"timestamp", "level", "event", "logger", "service"}


def test_a_structlog_line_is_one_json_object_with_the_standard_fields(lines):
    brick_observe.get_logger("radar").info("refreshed", drives=12)
    (line,) = lines.events("refreshed")
    assert REQUIRED <= set(line)
    assert line["level"] == "info" and line["logger"] == "radar" and line["service"] == "test"
    assert line["drives"] == 12
    assert line["timestamp"].endswith("Z")


def test_stdlib_loggers_come_out_the_same(lines):
    logging.getLogger("werkzeug").warning("port %d busy", 5000)
    (line,) = lines.events("port 5000 busy")
    assert REQUIRED <= set(line)
    assert line["level"] == "warning" and line["logger"] == "werkzeug"


def test_levels_are_lowercase_for_every_source(lines):
    log = brick_observe.get_logger("a")
    log.warning("w")
    log.error("e")
    log.critical("c")
    logging.getLogger("b").critical("sc")
    assert [line["level"] for line in lines.all() if line["event"] in ("w", "e", "c", "sc")] == [
        "warning", "error", "critical", "critical"]


def test_an_exception_is_one_line_with_its_traceback_in_a_field(lines):
    try:
        raise ValueError("boom")
    except ValueError:
        brick_observe.get_logger("x").exception("failed")
        logging.getLogger("y").exception("stdlib failed")
    assert all(line.startswith("{") for line in lines.raw)
    for event in ("failed", "stdlib failed"):
        (line,) = lines.events(event)
        assert "Traceback" in line["exception"] and "ValueError: boom" in line["exception"]
        assert line["level"] == "error"


def test_a_line_inside_a_span_carries_its_trace(lines, spans):
    with brick_observe.span("work") as s:
        brick_observe.get_logger().info("inside")
    brick_observe.get_logger().info("outside")
    (inside,) = lines.events("inside")
    ctx = s.get_span_context()
    assert inside["trace_id"] == format(ctx.trace_id, "032x")
    assert inside["span_id"] == format(ctx.span_id, "016x")
    assert "trace_id" not in lines.events("outside")[0]


def test_bound_and_context_fields_are_kept(lines):
    structlog.contextvars.bind_contextvars(request_id="r1")
    try:
        brick_observe.get_logger("x").bind(drive="d1").info("bound")
    finally:
        structlog.contextvars.clear_contextvars()
    (line,) = lines.events("bound")
    assert line["request_id"] == "r1" and line["drive"] == "d1"


def test_log_level_is_a_threshold(monkeypatch):
    out = io.StringIO()
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    brick_observe.init("t", json=True, stream=out)
    brick_observe.get_logger().info("quiet")
    logging.getLogger("lib").info("quiet too")
    brick_observe.get_logger().warning("loud")
    assert [json.loads(line)["event"] for line in out.getvalue().splitlines()] == ["loud"]


def test_an_unknown_level_is_refused():
    with pytest.raises(ValueError, match="unknown log level"):
        brick_observe.init("t", level="chatty", stream=io.StringIO())


def test_log_format_env_picks_json_or_console(monkeypatch):
    out = io.StringIO()
    monkeypatch.setenv("LOG_FORMAT", "json")
    brick_observe.init("t", stream=out)
    brick_observe.get_logger().info("as json")
    assert json.loads(out.getvalue())["event"] == "as json"

    out = io.StringIO()
    monkeypatch.delenv("LOG_FORMAT")
    brick_observe.init("t", stream=out)
    brick_observe.get_logger().info("for a terminal", n=1)
    text = out.getvalue()
    assert "for a terminal" in text and not text.startswith("{")
    assert "\x1b[" not in text  # not a terminal, so no colour codes


def test_init_again_replaces_its_handler_and_basic_configs(lines):
    logging.basicConfig()  # a library's, say: a plain stream handler on the root
    file_handler = logging.FileHandler("/dev/null")
    logging.getLogger().addHandler(file_handler)
    out = io.StringIO()
    brick_observe.init("t", json=True, stream=out)
    brick_observe.init("t", json=True, stream=out)
    logging.getLogger("x").warning("once")
    assert len([line for line in out.getvalue().splitlines() if '"once"' in line]) == 1
    assert file_handler in logging.getLogger().handlers  # someone else's file handler is left alone
    logging.getLogger().removeHandler(file_handler)
    file_handler.close()


def test_warnings_are_logged():
    # In a fresh interpreter: pytest catches warnings itself.
    script = (
        "import warnings, brick_observe\n"
        "brick_observe.init('t', json=True)\n"
        "warnings.warn('old api', UserWarning)\n"
    )
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    (line,) = [json.loads(x) for x in r.stdout.splitlines()]
    assert line["logger"] == "py.warnings" and "old api" in line["event"] and line["level"] == "warning"


def test_an_uncaught_exception_is_one_json_line():
    script = (
        "import brick_observe\n"
        "brick_observe.init('crashy', json=True)\n"
        "raise RuntimeError('oh no')\n"
    )
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 1
    assert r.stderr == ""
    (line,) = [json.loads(x) for x in r.stdout.splitlines()]
    assert line["event"] == "uncaught exception" and line["level"] == "critical"
    assert "RuntimeError: oh no" in line["exception"] and line["service"] == "crashy"


def test_an_exception_in_a_thread_is_one_json_line(lines):
    t = threading.Thread(target=lambda: 1 / 0, name="worker")
    t.start()
    t.join()
    (line,) = lines.events("uncaught exception in a thread")
    assert line["thread"] == "worker" and "ZeroDivisionError" in line["exception"]


def test_an_exception_marked_logged_is_not_logged_again(lines):
    exc = RuntimeError("already said")
    logs.mark_logged(exc)
    sys.excepthook(RuntimeError, exc, None)
    assert lines.events("uncaught exception") == []


def test_a_span_attribute_may_be_called_name(lines, spans):
    with brick_observe.span("greet", name="nested") as s:
        pass
    assert s.attributes["name"] == "nested"
