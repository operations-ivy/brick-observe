"""The Kubernetes side, as manifests to commit to an app's `k8s_config/`
(Argo CD applies them):

    python -m brick_observe env
    python -m brick_observe podmonitor --name wigle-console --namespace wigle
    python -m brick_observe dashboard --service wigle-console --namespace wigle > \\
        k8s_config/metrics/wigle-console-observe-dashboard.yaml

- `env`: the container env init() reads: the downward API's pod, namespace
  and node, `LOG_FORMAT=json`, the collector's OTLP endpoint, the Pushgateway.
- `podmonitor`: Prometheus scrapes the pods' /metrics (the `job` label becomes
  `<namespace>/<name>`, the same as Loki's).
- `dashboard`: a ConfigMap Grafana's sidecar loads (label `grafana_dashboard`):
  requests, errors, latency and in-flight by route; upstream calls; warnings
  and errors from Loki; recent traces from Tempo; and with `--job`, the
  Pushgateway's last run and last success.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

OTLP_ENDPOINT = "http://otel-collector-opentelemetry-collector.monitoring.svc.cluster.local:4317"
PUSHGATEWAY = "http://pushgateway-prometheus-pushgateway.monitoring.svc.cluster.local:9091"
PROMETHEUS = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}
TEMPO = {"type": "tempo", "uid": "tempo"}


def env(*, pushgateway: bool = False) -> list[dict]:
    """A container's `env:` entries for init()."""
    entries = [
        {"name": "POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
        {"name": "POD_NAMESPACE", "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}},
        {"name": "NODE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}},
        {"name": "LOG_FORMAT", "value": "json"},
        {"name": "OTEL_EXPORTER_OTLP_ENDPOINT", "value": OTLP_ENDPOINT},
    ]
    if pushgateway:
        entries.append({"name": "PUSHGATEWAY_URL", "value": PUSHGATEWAY})
    return entries


def env_yaml(*, pushgateway: bool = False, indent: int = 8) -> str:
    pad = " " * indent
    lines = [f"{pad}env:"]
    for e in env(pushgateway=pushgateway):
        lines.append(f"{pad}- name: {e['name']}")
        if "value" in e:
            lines.append(f"{pad}  value: {json.dumps(e['value'])}")
        else:
            lines.append(f"{pad}  valueFrom:")
            lines.append(f"{pad}    fieldRef:")
            lines.append(f"{pad}      fieldPath: {e['valueFrom']['fieldRef']['fieldPath']}")
    return "\n".join(lines) + "\n"


def podmonitor_yaml(name: str, namespace: str, *, app: str | None = None, port: str = "http",
                    path: str = "/metrics", interval: str = "30s") -> str:
    return f"""# Prometheus scrapes {name}'s {path} (brick-observe's standard metrics plus its own).
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata:
  name: {name}
  namespace: {namespace}
spec:
  selector:
    matchLabels:
      app: {app or name}
  podMetricsEndpoints:
  - port: {port}
    path: {path}
    interval: {interval}
"""


@dataclass
class _Layout:
    """Panels two to a row, 12 wide and 8 high; a row header spans the width."""
    y: int = 0
    x: int = 0
    next_id: int = 1

    def id(self) -> int:
        self.next_id += 1
        return self.next_id - 1

    def row(self, title: str) -> dict:
        if self.x:
            self.y, self.x = self.y + 8, 0
        panel = {"type": "row", "title": title, "id": self.id(), "collapsed": False,
                 "gridPos": {"h": 1, "w": 24, "x": 0, "y": self.y}, "panels": []}
        self.y += 1
        return panel

    def place(self, panel: dict, width: int = 12) -> dict:
        if self.x + width > 24:
            self.y, self.x = self.y + 8, 0
        panel["gridPos"] = {"h": 8, "w": width, "x": self.x, "y": self.y}
        panel["id"] = self.id()
        self.x += width
        if self.x >= 24:
            self.y, self.x = self.y + 8, 0
        return panel


def _series(title: str, targets: list[tuple[str, str]], *, unit: str = "short", datasource=PROMETHEUS,
            description: str = "") -> dict:
    return {
        "type": "timeseries", "title": title, "description": description, "datasource": datasource,
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
        "targets": [{"datasource": datasource, "expr": expr, "legendFormat": legend, "refId": chr(65 + i)}
                    for i, (expr, legend) in enumerate(targets)],
    }


def _stat(title: str, expr: str, *, unit: str = "short", description: str = "") -> dict:
    return {
        "type": "stat", "title": title, "description": description, "datasource": PROMETHEUS,
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value"},
        "targets": [{"datasource": PROMETHEUS, "expr": expr, "refId": "A", "instant": True}],
    }


def dashboard(service: str, namespace: str, *, monitor: str | None = None,
              job: str | None = None) -> dict:
    """Grafana's JSON for a service's standard dashboard. `monitor` is the
    PodMonitor's name (default: the service), `job` a Pushgateway job's name."""
    sel = f'job="{namespace}/{monitor or service}"'
    rate = "[$__rate_interval]"
    L = _Layout()
    panels: list[dict] = []
    panels.append(L.row("Requests"))
    panels.append(L.place(_series("Requests per second, by route", [
        (f"sum by (route) (rate(http_server_requests_total{{{sel}}}{rate}))", "{{route}}")], unit="reqps")))
    panels.append(L.place(_series("Server errors (5xx), share of requests", [
        (f'sum(rate(http_server_requests_total{{{sel}, code=~"5.."}}{rate})) / '
         f"sum(rate(http_server_requests_total{{{sel}}}{rate}))", "5xx")], unit="percentunit")))
    panels.append(L.place(_series("Latency", [
        (f"histogram_quantile({q}, sum by (le) (rate(http_server_request_duration_seconds_bucket{{{sel}}}{rate})))",
         f"p{int(q * 100)}") for q in (0.5, 0.95, 0.99)], unit="s")))
    panels.append(L.place(_series("Latency p95, by route", [
        (f"histogram_quantile(0.95, sum by (le, route) "
         f"(rate(http_server_request_duration_seconds_bucket{{{sel}}}{rate})))", "{{route}}")], unit="s")))
    panels.append(L.place(_series("Requests in flight", [
        (f"sum(http_server_requests_in_flight{{{sel}}})", "in flight")])))
    panels.append(L.place(_series("Responses by code", [
        (f"sum by (code) (rate(http_server_requests_total{{{sel}}}{rate}))", "{{code}}")], unit="reqps")))

    panels.append(L.row("Upstreams"))
    panels.append(L.place(_series("Calls per second, by upstream and code", [
        (f"sum by (upstream, code) (rate(http_client_requests_total{{{sel}}}{rate}))", "{{upstream}} {{code}}")],
        unit="reqps", description='code "error": no answer (refused, timed out)')))
    panels.append(L.place(_series("Upstream latency p95", [
        (f"histogram_quantile(0.95, sum by (le, upstream) "
         f"(rate(http_client_request_duration_seconds_bucket{{{sel}}}{rate})))", "{{upstream}}")], unit="s")))

    if job:
        jsel = f'job="{job}"'
        panels.append(L.row(f"Job: {job}"))
        panels.append(L.place(_stat("Since the last success", f"time() - job_last_success_timestamp_seconds{{{jsel}}}",
                                    unit="s"), width=8))
        panels.append(L.place(_stat("Last run succeeded", f"job_succeeded{{{jsel}}}"), width=8))
        panels.append(L.place(_stat("Last run took", f"job_duration_seconds{{{jsel}}}", unit="s"), width=8))

    panels.append(L.row("Logs and traces"))
    logs = f'{{{sel}}} | json | level=~"warning|error|critical"'
    panels.append(L.place(_series("Warnings and errors per minute", [
        (f"sum by (level) (count_over_time({logs} [1m]))", "{{level}}")], datasource=LOKI)))
    panels.append(L.place({
        "type": "table", "title": "Recent traces", "datasource": TEMPO,
        "targets": [{"datasource": TEMPO, "queryType": "traceql", "limit": 20, "refId": "A",
                     "query": f'{{resource.service.name="{service}"}}'}],
    }))
    panels.append(L.place({
        "type": "logs", "title": "Warnings and errors", "datasource": LOKI,
        "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending"},
        "targets": [{"datasource": LOKI, "expr": logs, "refId": "A"}],
    }, width=24))

    return {
        "uid": f"observe-{service}"[:40],
        "title": f"{service}: service health",
        "tags": ["brick-observe", namespace],
        "timezone": "browser",
        "schemaVersion": 39,
        "time": {"from": "now-6h", "to": "now"},
        "refresh": "1m",
        "editable": False,
        "annotations": {"list": []},
        "templating": {"list": []},
        "panels": panels,
    }


def dashboard_configmap_yaml(service: str, namespace: str, **kwargs) -> str:
    body = json.dumps(dashboard(service, namespace, **kwargs), indent=2)
    key = f"{service}-observe-dashboard.json"
    indented = "\n".join("    " + line for line in body.splitlines())
    return f"""# Generated by brick-observe (python -m brick_observe dashboard); regenerate, don't edit.
# Grafana's sidecar loads any ConfigMap labelled grafana_dashboard, in any namespace.
apiVersion: v1
kind: ConfigMap
metadata:
  name: {service}-observe-dashboard
  namespace: {namespace}
  labels:
    grafana_dashboard: "1"
data:
  {key}: |
{indented}
"""
