"""Who is emitting: the service, its version, and where it runs.

In Kubernetes the pod, namespace and node come from the downward API
(`brick_observe.k8s.env()` writes the env entries); on a host they're absent
and `host` is the machine's name."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass


@dataclass(frozen=True)
class Resource:
    service: str
    version: str
    namespace: str | None = None
    pod: str | None = None
    node: str | None = None
    host: str = ""

    def otel_attributes(self) -> dict[str, str]:
        """OpenTelemetry's semantic-convention names, the empty ones left out."""
        attributes = {
            "service.name": self.service,
            "service.version": self.version,
            "k8s.namespace.name": self.namespace,
            "k8s.pod.name": self.pod,
            "k8s.node.name": self.node,
            "host.name": self.host,
        }
        return {k: v for k, v in attributes.items() if v}


def detect(service: str, version: str | None = None) -> Resource:
    if not service:
        raise ValueError("a service name is required")
    env = os.environ
    return Resource(
        service=service,
        version=version or env.get("SERVICE_VERSION") or "unknown",
        namespace=env.get("POD_NAMESPACE") or None,
        pod=env.get("POD_NAME") or None,
        node=env.get("NODE_NAME") or None,
        host=socket.gethostname(),
    )
