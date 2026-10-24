import pytest

from brick_observe._resource import detect


def test_reads_the_downward_api_env(monkeypatch):
    monkeypatch.setenv("POD_NAME", "web-abc")
    monkeypatch.setenv("POD_NAMESPACE", "wigle")
    monkeypatch.setenv("NODE_NAME", "brick666")
    monkeypatch.setenv("SERVICE_VERSION", "1.2.3")
    r = detect("wigle-console")
    assert (r.service, r.version, r.pod, r.namespace, r.node) == ("wigle-console", "1.2.3", "web-abc", "wigle", "brick666")
    assert r.otel_attributes()["k8s.node.name"] == "brick666"
    assert r.otel_attributes()["service.version"] == "1.2.3"


def test_on_a_host_only_the_host_is_known(monkeypatch):
    for name in ("POD_NAME", "POD_NAMESPACE", "NODE_NAME", "SERVICE_VERSION"):
        monkeypatch.delenv(name, raising=False)
    r = detect("brick-status", version="2.0")
    assert r.version == "2.0"
    attrs = r.otel_attributes()
    assert "k8s.pod.name" not in attrs and attrs["host.name"]


def test_version_defaults_to_unknown(monkeypatch):
    monkeypatch.delenv("SERVICE_VERSION", raising=False)
    assert detect("x").version == "unknown"


def test_a_service_name_is_required():
    with pytest.raises(ValueError):
        detect("")


def test_init_twice_with_tracing_keeps_one_provider(monkeypatch):
    import subprocess
    import sys

    script = (
        "import io, brick_observe\n"
        "a = brick_observe.init('t', stream=io.StringIO())\n"
        "b = brick_observe.init('t', stream=io.StringIO())\n"
        "assert a.tracer_provider is b.tracer_provider is not None\n"
    )
    env = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9", "PATH": "/usr/bin:/bin"}
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    assert "Overriding" not in r.stderr
