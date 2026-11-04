"""Warm digest is offline, tenant-scoped, distilled-only and seeds recall dedup."""

import json

import pytest


@pytest.fixture
def cache(suite, isolated_modules, monkeypatch):
    if suite.name != "claude-code":
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
    # A background rebuild alone never counts as use.
    assert calls[-1]["record_context_usage"] is False
    assert data["server_version"] == "1.6.4"
    cache.build("ds", "old", "new-host", request=request, record_usage=True)
    assert calls[-1]["record_context_usage"] is capable


def test_usage_is_stamped_only_after_a_session_receives_guidance(cache, monkeypatch):
    def request(path, body, **kwargs):
        if path == "/health":
            return {"version": "1.6.4", "capabilities": ["context_usage_v1"]}
        if body["scope"] == ["session_context"]:
            return [{"source": "session_context", "content": "Use Postgres for transactions."}]
        return []

    data = cache.build("ds", "old", "new-host", request=request)
    assert data["guidance_hashes"]
    wanted = cache.cache_path("ds").with_suffix(".wanted.json")
    assert not wanted.exists()

    assert "Postgres" in cache.inject("ds", "new-host")
    assert json.loads(wanted.read_text())["record_usage"] is True
    # A plain refresh queued afterwards keeps the pending stamp.
    cache.request_refresh("ds", "old", "new-host")
    assert json.loads(wanted.read_text())["record_usage"] is True

    seen = []
    monkeypatch.setattr(cache, "build", lambda *a, **k: seen.append(k.get("record_usage")))
    cache.refresh_pending()
    assert seen == [True]
    assert not wanted.exists()


def test_digest_without_guidance_requests_no_usage_stamp(cache):
    cache.build(
        "ds",
        "old",
        "new-host",
        request=lambda path, body, **k: (
            [{"source": "graph", "text": "The project uses an outbox."}]
            if body and body.get("scope") == ["graph"]
            else []
        ),
    )
    assert "outbox" in cache.inject("ds", "new-host")
    assert not cache.cache_path("ds").with_suffix(".wanted.json").exists()


def test_old_rendering_hashes_request_a_fresh_digest(cache):
    data = cache.build("ds", "old", "new-host", request=lambda *a, **k: [])
    data.pop("entry_format")
    cache.cache_path("ds").write_text(json.dumps(data))
    assert cache.load("ds") == {}
    assert cache.inject("ds", "new-host") == ""
    assert cache.cache_path("ds").with_suffix(".wanted.json").exists()


def test_failing_refresh_backs_off_logs_and_is_eventually_dropped(cache, monkeypatch):
    import _cloud_worker

    events = []
    monkeypatch.setattr(_cloud_worker, "log", lambda event, **kw: events.append(event))

    def broken(*a, **kw):
        raise OSError("cloud unreachable")

    monkeypatch.setattr(cache, "build", broken)
    cache.request_refresh("ds", "old", "new-host")
    wanted = cache.cache_path("ds").with_suffix(".wanted.json")
    clock = cache.time.time()
    delays = []
    for _ in range(cache.MAX_REFRESH_ATTEMPTS - 1):
        monkeypatch.setattr(cache.time, "time", lambda: clock)
        cache.refresh_pending()
        delays.append(cache.read(wanted)["next_attempt"] - clock)
        clock += delays[-1]
    assert delays == [60, 120, 240, 480]
    monkeypatch.setattr(cache.time, "time", lambda: clock)
    cache.refresh_pending()
    assert not wanted.exists()
    assert events.count("context_cache_refresh_failed") == cache.MAX_REFRESH_ATTEMPTS
    assert events[-1] == "context_cache_refresh_dropped"
    assert not _cloud_worker.has_pending_work(now=clock)


@pytest.mark.parametrize("change", ["other_target", "expired"])
def test_orphaned_refresh_requests_do_not_keep_the_worker_alive(cache, monkeypatch, change):
    import _cloud_worker

    monkeypatch.setattr(_cloud_worker, "log", lambda *a, **kw: None)
    monkeypatch.setattr(cache, "build", lambda *a, **kw: pytest.fail("must not refresh"))
    cache.request_refresh("ds", "old", "new-host")
    wanted = cache.cache_path("ds").with_suffix(".wanted.json")
    if change == "other_target":
        other = {"base_url": "https://cloud", "principal": "b"}
        monkeypatch.setattr(cache, "target", lambda: other)
    else:
        item = cache.read(wanted)
        item["requested_at"] -= cache.TTL + 1
        cache.atomic_json(wanted, item)
    cache.refresh_pending()
    assert not wanted.exists()
    assert not _cloud_worker.has_pending_work()
