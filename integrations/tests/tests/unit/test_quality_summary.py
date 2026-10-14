import importlib.util
import json
from datetime import datetime, timezone

import pytest

NOW = 1800000000


@pytest.fixture
def quality(suite):
    path = suite.scripts_dir / "_quality_summary.py"
    if not path.exists():
        pytest.skip("quality cache not packaged for this host")
    spec = importlib.util.spec_from_file_location("quality_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ready():
    return {
        "schema": 1,
        "scope": "benchmark",
        "source": "live",
        "status": "ready",
        "pairs": 20,
        "better": 12,
        "equivalent": 6,
        "worse": 2,
        "unjudgeable": 0,
        "improvement_rate": 0.6,
        "regression_rate": 0.1,
        "evaluated_at": datetime.fromtimestamp(NOW, timezone.utc).isoformat(),
    }


def test_cache_scope_and_freshness(quality, tmp_path):
    path = quality.cache_path(tmp_path, "https://tenant-a")
    path.parent.mkdir()
    path.write_text(json.dumps({"summary": ready()}))
    label = quality.segment(tmp_path, "https://tenant-a", now=NOW)
    assert "▰▰▰▱▱ 60%" in label and "benchmark" in label and "10% worse" in label
    assert "pending" in quality.segment(tmp_path, "https://tenant-b", now=NOW)
    assert "stale" in quality.segment(tmp_path, "https://tenant-a", now=NOW + 172801)


@pytest.mark.parametrize(
    "change",
    [
        {"source": "smoke"},
        {"better": 13},
        {"improvement_rate": float("nan")},
        {"pairs": True},
        {"scope": "personal"},
        {"evaluated_at": "bad"},
    ],
)
def test_invalid_results_never_show_percent(quality, change):
    assert quality.validate({**ready(), **change}, NOW) == "pending"


def test_refresh_throttles_and_never_renders_network(quality, suite, tmp_path, monkeypatch):

    monkeypatch.syspath_prepend(str(suite.scripts_dir))
    calls = []

    def fetch():
        calls.append(1)
        return ready()

    quality.refresh(tmp_path, "https://tenant", fetch, now=NOW)
    quality.refresh(tmp_path, "https://tenant", fetch, now=NOW + 1)
    for _ in range(4):
        quality.segment(tmp_path, "https://tenant", now=NOW)
    assert len(calls) == 1
