"""Jobs (CronJobs, one-shot units): the pod is gone before Prometheus would
scrape it, so a run pushes its result to the Pushgateway on exit, whether it
succeeded or not.

    with brick_observe.job("wigle-sync") as run:
        ...
        run.gauge("wigle_sync_files_uploaded", "Files uploaded in the last run", uploaded)
        if failed:
            run.fail("3 files failed")

Every run pushes (job label = the job's name):

    job_last_run_timestamp_seconds
    job_duration_seconds
    job_succeeded                       1 or 0
    job_last_success_timestamp_seconds  only on success: pushadd keeps the old
                                        value otherwise, so "time since the last
                                        good run" is `time() - job_last_success_timestamp_seconds`

plus the run's own gauges. A run fails if its block raises (logged once, as
one line, then re-raised; `SystemExit(0)` is a success) or calls `fail()`.
The whole run is one span, and its spans are flushed before the push.
`PUSHGATEWAY_URL` unset (a run on the laptop): nothing is pushed. A push that
fails is logged, never raised: metrics never fail a run, or hide its result.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Iterator, Mapping

import structlog

from . import logs, tracing

log = structlog.get_logger("brick_observe.job")


class Run:
    def __init__(self, name: str):
        self.name = name
        self.ok = True
        self.reason: str | None = None
        self.started = time.time()
        self.finished: float | None = None
        self._gauges: dict[str, tuple[str, tuple[str, ...], dict[tuple[str, ...], float]]] = {}

    def fail(self, reason: str | None = None) -> None:
        """Mark the run failed without raising (some items failed, say)."""
        self.ok = False
        self.reason = reason

    def gauge(self, name: str, doc: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        """A gauge pushed with the run's results; call again with other labels
        for more series of the same gauge."""
        names = tuple(sorted(labels or {}))
        _, known, series = self._gauges.setdefault(name, (doc, names, {}))
        if names != known:
            raise ValueError(f"{name}: labels {names} don't match the earlier {known}")
        series[tuple(str((labels or {})[k]) for k in names)] = float(value)

    @property
    def duration(self) -> float:
        return (self.finished or time.time()) - self.started

    def registry(self):
        from prometheus_client import CollectorRegistry, Gauge

        registry = CollectorRegistry()

        def one(name, doc, value):
            Gauge(name, doc, registry=registry).set(value)

        one("job_last_run_timestamp_seconds", "When the job last finished a run.", self.finished)
        one("job_duration_seconds", "How long the job's last run took.", self.duration)
        one("job_succeeded", "Whether the job's last run succeeded (1/0).", int(self.ok))
        if self.ok:
            one("job_last_success_timestamp_seconds", "When the job last succeeded.", self.finished)
        for name, (doc, names, series) in self._gauges.items():
            g = Gauge(name, doc, list(names), registry=registry)
            for values, value in series.items():
                (g.labels(*values) if names else g).set(value)
        return registry


@contextlib.contextmanager
def job(name: str, *, gateway: str | None = None,
        grouping_key: Mapping[str, str] | None = None) -> Iterator[Run]:
    gateway = gateway or os.environ.get("PUSHGATEWAY_URL")
    run = Run(name)
    log.info("job started", job=name)
    try:
        with tracing.span(f"job {name}", **{"job.name": name}) as s:
            try:
                yield run
            except SystemExit as exc:
                if exc.code not in (0, None):
                    run.fail(f"exit {exc.code}")
                raise
            except BaseException as exc:
                run.fail(type(exc).__name__)
                log.error("job failed", job=name, exc_info=True)
                logs.mark_logged(exc)
                raise
            finally:
                if s is not None and not run.ok:
                    from opentelemetry.trace import Status, StatusCode

                    s.set_status(Status(StatusCode.ERROR, run.reason))
    finally:
        run.finished = time.time()
        (log.info if run.ok else log.warning)(
            "job finished", job=name, succeeded=run.ok, reason=run.reason,
            duration_seconds=round(run.duration, 3))
        tracing.flush()
        _push(run, gateway, grouping_key)


def _push(run: Run, gateway: str | None, grouping_key: Mapping[str, str] | None) -> None:
    if not gateway:
        log.debug("no PUSHGATEWAY_URL, metrics not pushed", job=run.name)
        return
    try:
        from prometheus_client import pushadd_to_gateway

        pushadd_to_gateway(gateway, job=run.name, registry=run.registry(),
                           grouping_key=dict(grouping_key or {}), timeout=10)
    except Exception:
        log.warning("failed to push the job's metrics", job=run.name, gateway=gateway, exc_info=True)

