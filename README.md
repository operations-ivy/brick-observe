# brick-observe

Logs, metrics and traces the same way in every brick homelab service, from
one `init()` call. It replaces the observability code each app wrote for
itself (brick-k8s-config#40): structlog setups, Flask request hooks,
`http.server` collectors, tracer setup, and Pushgateway pushes.

```python
import brick_observe
from flask import Flask

brick_observe.init("wigle-console")                 # logs, traces, service_info
log = brick_observe.get_logger(__name__)

app = Flask(__name__)
brick_observe.flask.instrument(app)                  # metrics, spans, access lines, /metrics
brick_observe.instrument_requests({"api.wigle.net": "wigle"})

with brick_observe.span("refresh radar", drives=12):
    log.info("radar refreshed", drives=12, contacts=4638)
```

A CronJob:

```python
brick_observe.init("wigle-sync")
with brick_observe.job("wigle-sync") as run:         # pushed to the Pushgateway on exit
    ...
    run.gauge("wigle_sync_files_uploaded", "Files uploaded in the last run", uploaded)
    if failed:
        run.fail(f"{failed} files failed")
```

## What you get

| | How | Where it goes |
| --- | --- | --- |
| **Logs** | structlog; stdlib loggers (werkzeug, requests, urllib3, libraries) through the same renderer | stdout: JSON when `LOG_FORMAT=json`, a console renderer otherwise. Promtail/Alloy ship it to Loki; a host unit's goes to journald |
| **Traces** | OpenTelemetry, OTLP over gRPC, when `OTEL_EXPORTER_OTLP_ENDPOINT` is set | the cluster's collector, then Tempo |
| **Metrics** | prometheus-client, the same names everywhere | `/metrics` (PodMonitor), or the Pushgateway for a Job |

### Every log line

`timestamp` (ISO, UTC), `level` (lowercase), `event`, `logger`, `service`,
and inside a span `trace_id` and `span_id`. Grafana's Loki datasource turns
`trace_id` into a link to the trace in Tempo. An exception is **one** line,
with the traceback in `exception`. Uncaught exceptions (main thread or any
other) and `warnings` are logged the same way. `LOG_LEVEL` sets the threshold
(default `info`). Put facts in fields (`log.info("upload done", files=3)`),
not formatted into `event`. Never log secrets, tokens or full device records.

### Metrics

| Metric | Labels | From |
| --- | --- | --- |
| `http_server_requests_total` | `route`, `method`, `code` | `brick_observe.flask`, `brick_observe.httpserver` |
| `http_server_request_duration_seconds` (histogram) | `route`, `method` | 〃 |
| `http_server_requests_in_flight` | | 〃 |
| `http_client_requests_total` | `upstream`, `method`, `code` (`error`: no answer) | `instrument_requests` |
| `http_client_request_duration_seconds` (histogram) | `upstream` | 〃 |
| `service_info` | `service`, `version` | `init()` |
| `job_last_run_timestamp_seconds`, `job_duration_seconds`, `job_succeeded`, `job_last_success_timestamp_seconds` | (Pushgateway `job`) | `job()` |

No service prefix: Prometheus' `job`, `namespace` and `pod` labels tell
services apart. `route` is the matched route (`/drive/<transid>`), never the
raw path; unknown paths are `other`, so the series stay bounded.
`/health`, `/ready` and `/metrics` are counted but logged at debug and not
traced. `job_last_success_timestamp_seconds` is only pushed on success (the
Pushgateway's `pushadd` keeps the old value), so
`time() - job_last_success_timestamp_seconds` is "time since the last good run".

### Traces

- Flask: a server span per request (OpenTelemetry's Flask instrumentation).
- `http.server`: a server span per request, continuing the caller's
  `traceparent`.
- `requests`: a client span per call, and `traceparent` sent on, so the
  upstream's spans join the trace.
- `span(name, **attributes)` anywhere; without OpenTelemetry it does nothing.
- `job()`: the whole run is one span (an error span if it failed), flushed
  before the pod exits.

## Install

```toml
# pyproject.toml (Poetry): pinned to a tag, so images stay reproducible
brick-observe = { git = "https://github.com/operations-ivy/brick-observe.git", tag = "v0.1.0", extras = ["flask", "requests"] }
```

Extras: `metrics`, `tracing`, `flask`, `requests`, `all`. The core needs only
structlog: a host app on Debian's system Python (brick-status, the LCARS panel,
brick69's daemons) installs `python3-structlog` from apt and gets the same
logs and a `span()` that does nothing.

## Kubernetes

The manifests come from the package, to commit to the app's `k8s_config/`
(Argo CD applies them):

```sh
python -m brick_observe env [--pushgateway]          # the container's env: block
python -m brick_observe podmonitor --name wigle-console --namespace wigle
python -m brick_observe dashboard --service wigle-console --namespace wigle [--job wigle-sync] \
    > k8s_config/metrics/wigle-console-observe-dashboard.yaml
```

- `env`: the downward API's `POD_NAME`, `POD_NAMESPACE` and `NODE_NAME`
  (resource attributes on every span), `LOG_FORMAT=json`, the collector's
  endpoint, and with `--pushgateway` the Pushgateway's.
- `podmonitor`: scrapes the pods' `/metrics` (port `http`). Its `job` label is
  `<namespace>/<name>`; Promtail's `job` in Loki is `<namespace>/<app label>`.
  Name the PodMonitor after the pods' `app` label (the default) and one
  selector finds a service's metrics and its logs.
- `dashboard`: the standard Grafana dashboard as a ConfigMap (label
  `grafana_dashboard`): requests, 5xx share, latency and in-flight by route;
  upstream calls and latency; warnings and errors from Loki; recent traces
  from Tempo; and with `--job`, the Job's last run and last success.

`SERVICE_VERSION` (if set) becomes `service.version` and `service_info`'s
`version`.

## Changes

`poetry install -E all`, then `poetry run pytest`: logs (formats, fields,
the stdlib bridge, exceptions, uncaught exceptions), the Flask and
`http.server` middleware against real servers (routes, 500s, keep-alive, trace
propagation), `requests` against a real upstream, `job()` against a stand-in
Pushgateway, the manifests and dashboard, and the core with only structlog.
Then a release: bump the version here, in `pyproject.toml` and
`brick_observe/__init__.py`, add to `CHANGELOG.md`, merge, tag `vX.Y.Z` on
`main`.

Tried on the cluster before the first release (2026-10-24): a Flask service
using every piece, in a throwaway namespace, checked end to end. Its metrics
were in Prometheus by route and upstream, the Job's push in the Pushgateway,
JSON lines in Loki with their `trace_id`, one trace in Tempo from a request
through an outbound call to the nested request (with the pod, namespace and
node on every span), and the generated dashboard in Grafana.
