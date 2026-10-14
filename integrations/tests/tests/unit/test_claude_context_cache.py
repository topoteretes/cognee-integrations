"""Warm digest is offline, tenant-scoped, distilled-only and seeds recall dedup."""

import json

import pytest


@pytest.fixture
def cache(suite, isolated_modules, monkeypatch):
    if suite.name not in {"claude-code", "codex"}:
        pytest.skip("Claude digest")
    module = isolated_modules(suite, "_context_cache")
    monkeypatch.setattr(module, "target", lambda: {"base_url": "https://cloud", "principal": "a"})
    monkeypatch.setenv("COGNEE_SESSION_KEY", "new-host")
    return module


def test_build_uses_distilled_node_sets_and_seed_prevents_repeat(cache):
    requests = []

    def request(path, body, **kwargs):
        if path == "/health":
            return {"version": "1.6.3"}
        requests.append(body)
        if body["scope"] == ["session_context"]:
            return [
                {
                    "source": "session_context",
                    "content": "Use Postgres because transactions matter.",
                }
            ]
        return [{"source": "graph", "text": "The project uses a transactional outbox."}]

    data = cache.build("ds", "old-session", "new-host", request=request)
    assert requests[0]["node_name"] == ["session_learnings", "project_docs"]
    assert all(r["only_context"] for r in requests)
    assert len(json.dumps(data["rendered"])) < 6002
    assert "outbox" in cache.inject("ds", "new-host")
    import _injection_ledger as ledger

    sections = cache._sections(
        [
            {"source": "graph", "text": "The project uses a transactional outbox."},
            {"source": "session_context", "content": "Use Postgres because transactions matter."},
        ],
        "old-session",
    )
    text, stats, _ = ledger.render("new-host", "header", sections, 9000)
    assert stats["entries_deduped"] == 2
    assert "outbox" not in text


def test_stale_wrong_tenant_and_resume_never_inject(cache, monkeypatch):
    data = cache.build("ds", "", "new-host", request=lambda *a, **k: [])
    assert cache.load("ds", now=data["built_at"] + 86401) == {}
    assert cache.inject("ds", "new-host", source="resume") == ""
    monkeypatch.setattr(cache, "target", lambda: {"base_url": "https://other", "principal": "b"})
    assert cache.inject("ds", "new-host") == ""
    assert cache.cache_path("ds").with_suffix(".wanted.json").exists()


def test_raw_turns_are_rejected_even_if_server_ignores_scope(cache):
    rows = [
        {"source": "graph", "text": "Previous conversation\nQuestion: secret\nAnswer: raw"},
        {"source": "session", "question": "secret", "answer": "raw"},
    ]
    data = cache.build("ds", "", "new-host", request=lambda *a, **k: rows)
    assert not data["entry_hashes"]
    assert "secret" not in data["rendered"]


def test_failed_refresh_preserves_last_good_cache(cache, monkeypatch):
    data = cache.build("ds", "old", "new-host", request=lambda *a, **k: [])
    cache.request_refresh("ds", "old", "new-host")

    def offline(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(cache, "build", offline)
    cache.refresh_pending()
    assert cache.load("ds") == data
    assert cache.cache_path("ds").with_suffix(".wanted.json").exists()


@pytest.mark.parametrize("capable", [False, True])
def test_summary_and_usage_require_advertised_server_capability(cache, capable):
    calls = []

    def request(path, body, **kwargs):
        if path == "/health":
            return {
                "version": "1.6.4",
                "capabilities": ["session_summary_v1", "context_usage_v1"] if capable else [],
            }
        calls.append(body)
        if body["scope"] == ["session_context"]:
            return [
                {
                    "source": "session_context",
                    "content": "",
                    "session_summary": {
                        "goal": "Safe outbox",
                        "outcome": "Atomic cursor implemented",
                        "open_items": ["Crash test"],
                    },
                }
            ]
        return []

    data = cache.build("ds", "old", "new-host", request=request)
    assert ("Atomic cursor" in data["rendered"]) is capable
    assert calls[-1]["record_context_usage"] is capable
    assert data["server_version"] == "1.6.4"


def test_old_rendering_hashes_request_a_fresh_digest(cache):
    data = cache.build("ds", "old", "new-host", request=lambda *a, **k: [])
    data.pop("entry_format")
    cache.cache_path("ds").write_text(json.dumps(data))
    assert cache.load("ds") == {}
    assert cache.inject("ds", "new-host") == ""
    assert cache.cache_path("ds").with_suffix(".wanted.json").exists()
