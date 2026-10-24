"""Structured logs: structlog in front, the stdlib's logging underneath.

Every line, from structlog or from a stdlib logger (werkzeug, requests,
urllib3, a library's own), goes through one handler and one renderer, so a
process never mixes formats:

- `LOG_FORMAT=json`: one JSON object per line on stdout, for Loki (Promtail
  or Alloy ship a pod's stdout; journald holds a host unit's).
- anything else: structlog's console renderer, for a terminal.

Each line has `timestamp` (ISO, UTC), `level` (lowercase), `event`, `logger`,
`service`, and `trace_id`/`span_id` inside a span (Grafana's Loki datasource
links `trace_id` to Tempo). An exception is one line with its traceback in
the `exception` field. `LOG_LEVEL` (default info) sets the threshold.

Uncaught exceptions, in the main thread or any other, are logged the same way
instead of Python's multi-line traceback on stderr.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Any, TextIO

import structlog

from ._resource import Resource

try:
    from opentelemetry import trace as _trace
except ImportError:  # the core works without OpenTelemetry
    _trace = None

LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING,
          "error": logging.ERROR, "critical": logging.CRITICAL}
# Marks an exception already logged (by job(), say), so the excepthook
# doesn't log it a second time on its way out.
LOGGED = "_brick_observe_logged"

_handler: logging.Handler | None = None


def json_wanted() -> bool:
    return os.environ.get("LOG_FORMAT", "").strip().lower() == "json"


def add_trace_context(_: Any, __: str, event_dict: dict) -> dict:
    """trace_id and span_id of the current span, if there is a recording one."""
    if _trace is not None:
        ctx = _trace.get_current_span().get_span_context()
        if ctx.is_valid:
            event_dict.setdefault("trace_id", format(ctx.trace_id, "032x"))
            event_dict.setdefault("span_id", format(ctx.span_id, "016x"))
    return event_dict


def _add_service(service: str):
    def add_service(_: Any, __: str, event_dict: dict) -> dict:
        event_dict.setdefault("service", service)
        return event_dict

    return add_service


def _level(level: str | None) -> int:
    name = (level or os.environ.get("LOG_LEVEL") or "info").strip().lower()
    if name not in LEVELS:
        raise ValueError(f"unknown log level {name!r}; one of {', '.join(LEVELS)}")
    return LEVELS[name]


def configure(resource: Resource, *, json: bool | None = None, level: str | None = None,
              stream: TextIO | None = None) -> None:
    """Set up structlog and the root logger. Safe to call again (tests do):
    the previous handler is replaced, not added to."""
    global _handler
    as_json = json_wanted() if json is None else json
    out = stream or sys.stdout
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        _add_service(resource.service),
        add_trace_context,
        structlog.processors.StackInfoRenderer(),
    ]
    if as_json:
        render = [structlog.processors.format_exc_info, structlog.processors.UnicodeDecoder(),
                  structlog.processors.JSONRenderer()]
    else:
        render = [structlog.dev.ConsoleRenderer(colors=getattr(out, "isatty", lambda: False)())]
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, *render],
    )
    handler = logging.StreamHandler(out)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for h in list(root.handlers):
        # Ours from an earlier call, or basicConfig's plain stream handler (a
        # file handler, say, is left alone).
        if h is _handler or type(h) is logging.StreamHandler:
            root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(_level(level))
    _handler = handler
    logging.captureWarnings(True)

    structlog.reset_defaults()
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    _install_excepthooks()


def mark_logged(exc: BaseException) -> None:
    try:
        setattr(exc, LOGGED, True)
    except AttributeError:
        pass


def _install_excepthooks() -> None:
    log = structlog.get_logger("brick_observe")

    def excepthook(kind, exc, tb):
        if issubclass(kind, KeyboardInterrupt):
            sys.__excepthook__(kind, exc, tb)
            return
        if not getattr(exc, LOGGED, False):
            log.critical("uncaught exception", exc_info=(kind, exc, tb))

    def thread_excepthook(args):
        if args.exc_type is SystemExit or getattr(args.exc_value, LOGGED, False):
            return
        log.critical("uncaught exception in a thread",
                     thread=getattr(args.thread, "name", None),
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
