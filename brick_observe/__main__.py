"""`python -m brick_observe env|podmonitor|dashboard ...`: manifests for an
app's k8s_config/ (brick_observe.k8s)."""

from __future__ import annotations

import argparse
import sys

from . import k8s


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m brick_observe", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("env", help="the container env init() reads")
    p.add_argument("--pushgateway", action="store_true", help="add PUSHGATEWAY_URL (Jobs)")
    p.add_argument("--indent", type=int, default=8)

    p = sub.add_parser("podmonitor", help="a PodMonitor for the pods' /metrics")
    p.add_argument("--name", required=True)
    p.add_argument("--namespace", required=True)
    p.add_argument("--app", help="the pods' app label (default: the name)")
    p.add_argument("--port", default="http")
    p.add_argument("--path", default="/metrics")

    p = sub.add_parser("dashboard", help="the standard Grafana dashboard, as a ConfigMap")
    p.add_argument("--service", required=True)
    p.add_argument("--namespace", required=True)
    p.add_argument("--monitor", help="the PodMonitor's name (default: the service)")
    p.add_argument("--job", help="a Pushgateway job's name, for a Job row")

    args = parser.parse_args(argv)
    if args.command == "env":
        out = k8s.env_yaml(pushgateway=args.pushgateway, indent=args.indent)
    elif args.command == "podmonitor":
        out = k8s.podmonitor_yaml(args.name, args.namespace, app=args.app, port=args.port, path=args.path)
    else:
        out = k8s.dashboard_configmap_yaml(args.service, args.namespace, monitor=args.monitor, job=args.job)
    sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
