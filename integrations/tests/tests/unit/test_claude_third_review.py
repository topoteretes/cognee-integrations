"""The third review round of the directory-policy release (SDK-1411, Claude Code).

One section per fix:

* the code-graph requests and the forget script's curls reach a local server
  directly, never through a proxy, and the local server itself gets loopback
  in ``NO_PROXY`` when a proxy is copied to it;
* a shell storage directory dropped before the re-exec into the plugin venv is
  still named in the storage notice after it;
* the live suite hands its settings over as plugin options;
* a request to the local server is not sent without its owner key;
* the eval scaffold turns the plugin off instead of naming a server.
"""

from __future__ import annotations

import inspect
import os
import socket
import subprocess
import sys
import threading
import urllib.request

import pytest
from utils import isolation
from utils.isolation import build_env, usable_bash

BASH = usable_bash()


@pytest.fixture(autouse=True)
def _options_suite_only(suite):
    if not suite.keys_from_plugin_options:
        pytest.skip(f"{suite.name}: reads its settings from the environment")


def _as_hook(monkeypatch, suite):
    """This process is a hook Claude Code started, with no plugin options set."""
    isolation.scrub_cognee_env(monkeypatch)
    monkeypatch.setattr(isolation, "scrub_cognee_env", lambda _monkeypatch: None)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(suite.scripts_dir.parent))


class _Listener:
    """A socket that records the request text it is sent and answers ``{}``."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.requests: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            with conn:
                try:
                    self.requests.append(conn.recv(4096).decode("utf-8", "replace"))
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
                except OSError:
                    pass

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self.sock.close()


@pytest.fixture
def proxy(monkeypatch):
    """Every proxy variable pointing at a listener, with nothing excluded."""
    listener = _Listener()
    monkeypatch.setattr(urllib.request, "_opener", None)
    url = f"http://127.0.0.1:{listener.port}"
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, url)
    for name in ("NO_PROXY", "no_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    yield listener
    listener.close()


# ── no proxy for this machine: the code graph ─────────────────────────────

_CODE_GRAPH_CALLS = (
    "do_index_repo",
    "poll_code_graph_status",
    "index_repo",
    "reingest_if_changed",
    "autoindex_on_session_start",
)


def test_every_code_graph_request_uses_the_shared_opener(suite, isolated_modules):
    cg = isolated_modules(suite, "_code_graph")
    for name in _CODE_GRAPH_CALLS:
        opener = inspect.signature(getattr(cg, name)).parameters["opener"].default
        assert opener is cg.open_url, name


def test_a_code_graph_request_to_this_machine_skips_the_proxy(
    suite, isolated_modules, monkeypatch, proxy
):
    _as_hook(monkeypatch, suite)
    server = _Listener()
    try:
        cg = isolated_modules(suite, "_code_graph")
        url = f"http://127.0.0.1:{server.port}"
        cg.do_index_repo(url, "k-owner", "/repo", "code_repo", timeout=5)
        cg.poll_code_graph_status(url, "k-owner", "ds-1", 1, interval_seconds=0.1)
    finally:
        server.close()
    assert proxy.requests == []
    assert any("/api/v1/remember" in r for r in server.requests)
    assert any("/api/v1/datasets/status" in r for r in server.requests)


# ── no proxy for this machine: the forget script ──────────────────────────


@pytest.mark.skipif(BASH is None, reason="requires a working POSIX bash")
def test_the_forget_script_reaches_a_local_server_without_the_proxy(
    suite, temp_home, mock_server, proxy
):
    owner = "k-local-owner"
    mock_server.identity.seed_api_key(owner)
    mock_server.identity.user_api_keys["default_user@example.com"] = [{"key": owner}]
    env = build_env(suite, temp_home, extra={"COGNEE_LOCAL_API_URL": mock_server.url})
    proxy_url = f"http://127.0.0.1:{proxy.port}"
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        env[name] = proxy_url
    for name in ("NO_PROXY", "no_proxy"):
        env.pop(name, None)

    def run(*args):
        return subprocess.run(
            [BASH, str(suite.scripts_dir / "cognee-forget.sh"), *args],
            env=env,
            cwd=str(temp_home),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )

    listed = run("datasets")
    assert listed.returncode == 0, listed.stderr
    assert listed.stdout.rstrip().endswith("HTTP 200")
    assert proxy.requests == []
    assert any(c["path"] == "/api/v1/datasets" for c in mock_server.calls)

    # The shipped curl blocks run in a shell that evals these lines.
    exported = run("env")
    assert exported.returncode == 0, exported.stderr
    no_proxy = [line for line in exported.stdout.splitlines() if "no_proxy=" in line]
    assert no_proxy and "127.0.0.1" in no_proxy[0] and "localhost" in no_proxy[0]


# ── the local server's own calls to this machine ──────────────────────────


@pytest.mark.parametrize("own", ["", "corp.internal,.example.com"])
def test_the_server_env_exempts_loopback_from_a_copied_proxy(suite, hook_module, monkeypatch, own):
    _as_hook(monkeypatch, suite)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:3128")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    if own:
        monkeypatch.setenv("NO_PROXY", own)
    session_start = hook_module(suite, "session-start.py")
    env = session_start._server_env()

    spellings = ["NO_PROXY"] if sys.platform == "win32" else ["NO_PROXY", "no_proxy"]
    for name in spellings:
        entries = env[name].split(",")
        assert {"127.0.0.1", "localhost", "::1"} <= set(entries), name
        if own:
            assert {"corp.internal", ".example.com"} <= set(entries), name


def test_no_proxy_is_left_alone_without_a_proxy(suite, hook_module, monkeypatch):
    _as_hook(monkeypatch, suite)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    session_start = hook_module(suite, "session-start.py")
    env = session_start._server_env()
    assert "NO_PROXY" not in env and "no_proxy" not in env


# ── the storage notice survives the re-exec into the venv ─────────────────


def test_the_storage_notice_crosses_the_venv_reexec(suite, temp_home):
    if sys.platform == "win32":
        pytest.skip("the venv re-exec is a POSIX execv")
    script = suite.scripts_dir / "_env_file.py"
    env = build_env(suite, temp_home, extra={"DATA_ROOT_DIRECTORY": "/data/mine"})
    env.pop("COGNEE_PLUGIN_IN_VENV", None)
    env["CLAUDE_PLUGIN_ROOT"] = str(suite.scripts_dir.parent)
    # Drop the shell value, re-exec as _plugin_common does (os.execv: a fresh
    # process with the defaults only), then ask for the ignored names there.
    probe = (
        "import os, sys\n"
        f"sys.path.insert(0, {str(script.parent)!r})\n"
        "if os.environ.get('PROBE_STAGE') != '2':\n"
        "    import _env_file\n"
        "    _env_file.load_env_file()\n"
        "    assert 'DATA_ROOT_DIRECTORY' not in os.environ\n"
        "    os.environ['PROBE_STAGE'] = '2'\n"
        "    os.execv(sys.executable, [sys.executable, '-c', sys.argv[1], sys.argv[1]])\n"
        "import _env_file\n"
        "_env_file.load_env_file()\n"
        "print(','.join(_env_file.storage_settings_ignored()))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe, probe],
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "DATA_ROOT_DIRECTORY" in result.stdout.strip().split(",")
    assert "/data/mine" not in result.stdout


def test_the_carried_names_never_hold_a_value(suite, isolated_modules, monkeypatch):
    _as_hook(monkeypatch, suite)
    monkeypatch.setenv("COGNEE_PLUGIN_SHELL_STORAGE_IGNORED", "DATA_ROOT_DIRECTORY,/etc/passwd")
    env_file = isolated_modules(suite, "_env_file")
    assert env_file.storage_settings_ignored() == ["DATA_ROOT_DIRECTORY"]


# ── the live suite hands its settings over as plugin options ──────────────


def test_the_live_env_carries_the_settings_as_plugin_options(suite, tmp_path):
    from utils.live import build_live_env

    env = build_live_env(
        home=tmp_path / "home",
        project=tmp_path / "project",
        base_url="http://127.0.0.1:18011",
        dataset="live_x",
        llm_api_key="sk-live",
        suite=suite,
    )
    assert env["CLAUDE_PLUGIN_OPTION_COGNEE_BASE_URL"] == "http://127.0.0.1:18011"
    assert env["CLAUDE_PLUGIN_OPTION_LLM_API_KEY"] == "sk-live"
    assert env["CLAUDE_PLUGIN_ROOT"] == str(suite.scripts_dir.parent)


# ── no keyless request to the local server ────────────────────────────────


def test_a_local_request_without_its_owner_key_is_not_sent(
    suite, isolated_modules, mock_server, monkeypatch
):
    _as_hook(monkeypatch, suite)
    monkeypatch.setenv("COGNEE_LOCAL_API_URL", mock_server.url)
    pc = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(pc, "_api_key", lambda *a, **k: "")

    with pytest.raises(pc.LocalKeyUnavailable) as raised:
        pc._json_http_request("/api/v1/recall", {"query": "q"})
    assert not isinstance(raised.value, pc.urllib.error.HTTPError)
    assert all(c["path"] != "/api/v1/recall" for c in mock_server.calls)


def test_the_prompt_hook_records_no_auth_failure_without_a_local_key(
    suite, isolated_modules, mock_server, monkeypatch
):
    _as_hook(monkeypatch, suite)
    monkeypatch.setenv("COGNEE_LOCAL_API_URL", mock_server.url)
    pc = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(pc, "_api_key", lambda *a, **k: "")
    recall_http = isolated_modules(suite, "_recall_http")
    try:
        pc._json_http_request("/api/v1/recall", {"query": "q"})
    except Exception as exc:  # what the prompt hook classifies
        verdict = recall_http.classify_transport_exception(exc)
        assert verdict not in (recall_http.DOWN, recall_http.SLOW)
        assert not isinstance(exc, pc.urllib.error.HTTPError)
    else:
        pytest.fail("a keyless local request was sent")


# ── the eval scaffold turns the plugin off ────────────────────────────────


@pytest.mark.skipif(BASH is None, reason="requires a working POSIX bash")
def test_the_eval_scaffold_turns_the_plugin_off(suite, temp_home):
    scaffolds = sorted((suite.scripts_dir.parent / "evals").glob("*/*/isolate.sh"))
    assert scaffolds
    env = build_env(suite, temp_home)
    done = subprocess.run(
        [BASH, str(scaffolds[0])], env=env, capture_output=True, encoding="utf-8", timeout=30
    )
    assert done.returncode == 0, done.stderr

    probe = (
        "import sys\n"
        f"sys.path.insert(0, {str(suite.scripts_dir)!r})\n"
        "import _env_file\n"
        "legacy = _env_file.legacy_settings_in_file()\n"
        "print('legacy=' + ','.join(legacy['cloud'] + legacy['llm']))\n"
        "_env_file.load_env_file()\n"
        "print('still running')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], env=env, capture_output=True, encoding="utf-8", timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert "legacy=\n" in result.stdout
    assert "still running" not in result.stdout


def test_a_disabled_plugin_hook_does_nothing(suite, temp_home, mock_server):
    env_file = temp_home / ".cognee" / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("COGNEE_PLUGIN_DISABLED=true\n", encoding="utf-8")
    result = isolation.run_hook(
        suite,
        "session-context-lookup.py",
        stdin={"prompt": "hello", "session_id": "s1", "cwd": str(temp_home)},
        home=temp_home,
        extra_env={"COGNEE_LOCAL_API_URL": mock_server.url},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
    assert mock_server.calls == []
    assert not os.path.exists(temp_home / ".cognee-plugin" / "claude-code" / "hook.log") or (
        "recall" not in (temp_home / ".cognee-plugin" / "claude-code" / "hook.log").read_text()
    )
