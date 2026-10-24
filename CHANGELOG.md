# Changelog

Semantic versioning: a metric or log field renamed or removed is a major
version (dashboards and alerts depend on them).

## 0.1.0

First release (brick-k8s-config#40).

- `init()`: structlog logs (JSON with `LOG_FORMAT=json`), the stdlib loggers
  through the same renderer, one-line exceptions, uncaught exceptions and
  warnings logged; OTLP tracing when `OTEL_EXPORTER_OTLP_ENDPOINT` is set;
  `service_info`.
- `brick_observe.flask.instrument()` and `brick_observe.httpserver.Observed`:
  the `http_server_*` metrics, a span per request, one access line per request.
- `instrument_requests()`: the `http_client_*` metrics by upstream, client
  spans, `traceparent` sent on.
- `job()`: a run's result, duration and last success pushed to the
  Pushgateway, one span per run.
- `span()`, working without OpenTelemetry too.
- `python -m brick_observe env|podmonitor|dashboard`: the container env, a
  PodMonitor, and the standard Grafana dashboard.
