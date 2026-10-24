import json

import yaml

from brick_observe import k8s
from brick_observe.__main__ import main


def test_env_is_valid_yaml_matching_env(capsys):
    main(["env", "--pushgateway", "--indent", "0"])
    parsed = yaml.safe_load(capsys.readouterr().out)["env"]
    assert parsed == k8s.env(pushgateway=True)
    names = [e["name"] for e in parsed]
    assert names[:3] == ["POD_NAME", "POD_NAMESPACE", "NODE_NAME"] and "PUSHGATEWAY_URL" in names
    assert {"name": "LOG_FORMAT", "value": "json"} in parsed


def test_podmonitor(capsys):
    main(["podmonitor", "--name", "wigle-console", "--namespace", "wigle"])
    pm = yaml.safe_load(capsys.readouterr().out)
    assert pm["kind"] == "PodMonitor" and pm["metadata"] == {"name": "wigle-console", "namespace": "wigle"}
    assert pm["spec"]["selector"]["matchLabels"] == {"app": "wigle-console"}
    assert pm["spec"]["podMetricsEndpoints"][0] == {"port": "http", "path": "/metrics", "interval": "30s"}


def _dashboard(capsys, *args):
    main(["dashboard", "--service", "wigle-console", "--namespace", "wigle", *args])
    cm = yaml.safe_load(capsys.readouterr().out)
    assert cm["kind"] == "ConfigMap" and cm["metadata"]["labels"] == {"grafana_dashboard": "1"}
    (body,) = cm["data"].values()
    return json.loads(body)


def test_dashboard_configmap_holds_valid_grafana_json(capsys):
    d = _dashboard(capsys)
    assert d["uid"] == "observe-wigle-console" and len(d["uid"]) <= 40
    ids = [p["id"] for p in d["panels"]]
    assert len(ids) == len(set(ids))
    for p in d["panels"]:
        if p["type"] != "row":
            assert p["targets"], p["title"]


def test_panels_never_overlap(capsys):
    cells = set()
    for p in _dashboard(capsys, "--job", "wigle-sync")["panels"]:
        g = p["gridPos"]
        assert g["x"] + g["w"] <= 24
        for x in range(g["x"], g["x"] + g["w"]):
            for y in range(g["y"], g["y"] + g["h"]):
                assert (x, y) not in cells, p["title"]
                cells.add((x, y))


def test_every_query_selects_the_service(capsys):
    for p in _dashboard(capsys)["panels"]:
        for t in p.get("targets", []):
            if t["datasource"]["type"] in ("prometheus", "loki"):
                assert 'job="wigle/wigle-console"' in t["expr"], p["title"]
            else:
                assert 'resource.service.name="wigle-console"' in t["query"]


def test_a_job_row_only_with_job(capsys):
    assert not any("job_" in t.get("expr", "") for p in _dashboard(capsys)["panels"] for t in p.get("targets", []))
    exprs = [t.get("expr") for p in _dashboard(capsys, "--job", "wigle-sync")["panels"] for t in p.get("targets", [])]
    assert 'time() - job_last_success_timestamp_seconds{job="wigle-sync"}' in exprs


def test_monitor_overrides_the_selector(capsys):
    d = _dashboard(capsys, "--monitor", "console")
    assert 'job="wigle/console"' in d["panels"][1]["targets"][0]["expr"]
