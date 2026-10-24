"""A host app on Debian's system Python has structlog and nothing else:
logs, span() and job() must still work."""

import json
import subprocess
import sys

SCRIPT = r"""
import sys
for missing in ("opentelemetry", "prometheus_client", "flask", "requests"):
    sys.modules[missing] = None  # import fails, as if not installed
import brick_observe
o = brick_observe.init("brick-status", json=True)
assert not o.tracing
log = brick_observe.get_logger("board")
with brick_observe.span("probe", target="x") as s:
    assert s is None
    log.info("probed", status=200)
with brick_observe.job("local-job") as run:
    run.gauge("g", "a gauge", 1)
"""


def test_logs_spans_and_jobs_without_the_optional_packages(monkeypatch):
    monkeypatch.delenv("PUSHGATEWAY_URL", raising=False)
    r = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr + r.stdout
    lines = [json.loads(x) for x in r.stdout.splitlines()]
    events = [line["event"] for line in lines]
    assert "probed" in events and "job finished" in events
    assert "trace_id" not in lines[events.index("probed")]
