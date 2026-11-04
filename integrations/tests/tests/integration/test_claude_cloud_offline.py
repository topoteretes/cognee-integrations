"""Cloud hook subprocesses stay usable with no network or background worker."""

import contextlib
import json
import socket
import time

import pytest

#: Every network timeout the hooks could wait on, pinned through the env so the
#: cost of a synchronous call is known: a hook that waits on the server pays at
#: least this long. The hooks must instead hand the work to the background
#: worker and return in roughly the time it takes to start an interpreter.
NETWORK_TIMEOUT = 8.0
#: A start that finishes inside half the network timeout cannot have waited on
#: the server. Generous enough for a slow CI runner (Windows spawns an
#: interpreter and the detached worker in 1-1.5 s), and an order of magnitude
#: below what one blocking call costs.
START_BUDGET = NETWORK_TIMEOUT / 2


@contextlib.contextmanager
def black_hole_server():
    """A local URL whose server accepts connections and never answers.

    A refused port (``127.0.0.1:1``) fails a request instantly, so a hook that
    had regressed into waiting on the network would still look fast. A listener
    that never reads makes any synchronous request hang for its full timeout,
    which is what the budget below is measured against.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        sock.listen(8)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        sock.close()


def _blocks_for_timeout(url: str) -> bool:
    """Sanity check of the black hole itself: a plain request waits the timeout."""
    import urllib.error
    import urllib.request

    started = time.monotonic()
    try:
        urllib.request.urlopen(url, timeout=0.5)
    except (urllib.error.URLError, OSError, TimeoutError):
        pass
    return time.monotonic() - started >= 0.4


def test_offline_start_capture_and_end_are_local(suite, run_hook, temp_home):
    if suite.name != "claude-code":
        pytest.skip("Claude cloud hooks")
    env = {
        "COGNEE_BACKEND": "cloud",
        "COGNEE_IDLE_DISABLED": "1",
        "COGNEE_REACHABLE_TIMEOUT": str(NETWORK_TIMEOUT),
        "COGNEE_REGISTER_TIMEOUT": str(NETWORK_TIMEOUT),
    }
    with black_hole_server() as url:
        assert _blocks_for_timeout(url), "the black-hole listener answered or refused"
        _offline_start_capture_and_end_are_local(suite, run_hook, temp_home, env, url)


def _offline_start_capture_and_end_are_local(suite, run_hook, temp_home, env, url):
    payload = {"session_id": "offline-host", "cwd": str(temp_home), "source": "startup"}
    samples = []
    for _ in range(5):
        started = time.monotonic()
        result = run_hook(suite, "session-start.py", stdin=payload, service_url=url, env=env)
        samples.append(time.monotonic() - started)
        assert result.returncode == 0, result.stderr
        assert "background" in result.stdout
    assert max(samples) < START_BUDGET, (
        f"session start waited on the server: {samples} (budget {START_BUDGET}s, "
        f"a blocking call costs >= {NETWORK_TIMEOUT}s)"
    )
    prompt = {**payload, "prompt": "Remember our reliable offline session."}
    assert (
        run_hook(suite, "store-user-prompt.py", stdin=prompt, service_url=url, env=env).returncode
        == 0
    )
    answer = {**payload, "last_assistant_message": "The project uses Postgres."}
    assert (
        run_hook(
            suite, "store-to-session.py", "--stop", stdin=answer, service_url=url, env=env
        ).returncode
        == 0
    )
    root = temp_home / ".cognee-plugin" / "claude-code"
    paths = list((root / "spool").glob("*.json"))
    assert len(paths) == 1
    item = json.loads(paths[0].read_text())
    assert item["entry"]["question"] == prompt["prompt"]
    assert item["entry"]["answer"] == answer["last_assistant_message"]
    end = run_hook(
        suite, "sync-session-to-graph.py", "--session-end", stdin=payload, service_url=url, env=env
    )
    assert end.returncode == 0, end.stderr
    wanted = list((root / "improve-wanted").glob("*.json"))
    assert wanted and json.loads(wanted[0].read_text())["reason"] == "final"
    assert not (root / "final-sync-once").exists()
    cache = root / "context-cache" / (item["dataset"] + ".json")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(
            {
                "entry_format": "bare-records-v1",
                "rendered": "Cognee project memory: use Postgres for transaction safety.",
                "entry_hashes": ["f" * 16],
                "guidance_hashes": [],
                "built_at": time.time(),
                "server_version": "1.6.3",
                "target": item["target"],
            }
        )
    )
    for index in range(5):
        started = time.monotonic()
        warm = run_hook(
            suite,
            "session-start.py",
            service_url=url,
            env=env,
            stdin={**payload, "session_id": f"warm-{index}"},
        )
        assert time.monotonic() - started < START_BUDGET, "warm start waited on the server"
        assert "transaction safety" in warm.stdout
        ledger = root / "sessions" / f"warm-{index}" / "injected.json"
        assert "f" * 16 in json.loads(ledger.read_text())["entry_hashes"]
    resumed = run_hook(
        suite,
        "session-start.py",
        service_url=url,
        env=env,
        stdin={**payload, "session_id": "warm-0", "source": "resume"},
    )
    assert "transaction safety" not in resumed.stdout

    # The digest leads the startup context without replacing it, and honours the opt-out.
    steer = "Use cognee-search for memory and cognee-remember to save it."
    context = json.loads(warm.stdout)["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("Cognee project memory") and steer in context
    opted_out = run_hook(
        suite,
        "session-start.py",
        service_url=url,
        env={**env, "COGNEE_PREFER_MEMORY": "false"},
        stdin={**payload, "session_id": "warm-opt-out"},
    )
    context = json.loads(opted_out.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "transaction safety" in context and steer not in context

    # A malformed cache costs the digest, never the rest of the startup output.
    cache.write_text(json.dumps({**json.loads(cache.read_text()), "built_at": "yesterday"}))
    broken = run_hook(
        suite,
        "session-start.py",
        service_url=url,
        env=env,
        stdin={**payload, "session_id": "warm-broken"},
    )
    assert broken.returncode == 0, broken.stderr
    context = json.loads(broken.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "transaction safety" not in context and steer in context


def test_busy_ledger_on_compact_never_blocks_startup(suite, run_hook, temp_home, isolated_modules):
    if suite.name != "claude-code":
        pytest.skip("Claude cloud hooks")
    env = {"COGNEE_BACKEND": "cloud", "COGNEE_IDLE_DISABLED": "1"}
    payload = {"session_id": "busy-ledger", "cwd": str(temp_home), "source": "compact"}
    ledger = temp_home / ".cognee-plugin" / "claude-code" / "sessions" / "busy-ledger"
    ledger.mkdir(parents=True)
    (ledger / "injected.json").write_text(json.dumps({"entry_hashes": ["f" * 16]}))
    file_lock = isolated_modules(suite, "_file_lock").file_lock
    with file_lock(ledger / "injected.lock") as acquired:
        assert acquired
        result = run_hook(
            suite, "session-start.py", stdin=payload, service_url="http://127.0.0.1:1", env=env
        )
    assert result.returncode == 0, result.stderr
    assert "background" in result.stdout
    # The context was compacted: no stale "already in context" claims survive.
    assert not (ledger / "injected.json").exists()
