from __future__ import annotations

import json
from pathlib import Path

from observability.dashboards import DASHBOARDS, METRIC_NAMES, write_dashboards

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "k8s-config" / "observability" / "dashboards"

def test_dashboards_cover_each_layer() -> None:
    assert set(DASHBOARDS) == {
        "overview",
        "gateway",
        "router",
        "replicas",
        "vllm",
        "keda",
        "hami",
        "mooncake",
        "cluster",
        "errors",
    }
    write_dashboards(DEST)
    for name, builder in DASHBOARDS.items():
        path = DEST / f"{name}.json"
        payload = json.loads(path.read_text())
        assert payload["uid"].startswith("class10-")
        assert payload == builder()
        blob = json.dumps(payload)
        for metric in METRIC_NAMES[name]:
            assert metric in blob, f"{name} missing {metric}"
