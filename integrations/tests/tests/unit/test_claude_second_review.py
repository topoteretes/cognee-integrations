"""The second review round of the directory-policy release (SDK-1411, Claude Code).

One section per fix:

* one loopback rule, used by every check, so a host that merely looks local
  never gets the local treatment and never redirects the Cloud key;
* the Cloud key is exported only for a server it is the key for, and the
  ``COGNEE_BACKEND=cloud`` pin applies it to a loopback address only when that
  address is the configured Cloud URL option;
* every process picks the same local owner key, skipping a stored hash, and a
  server that hides its keys is refused instead of getting a new key each time;
* a request to this machine never goes through a proxy;
* the env-file parser keeps a value only for an allowlisted name;
* the two dataset settings the allowlist had dropped are loaded again, and
  every setting the scripts read is accounted for;
* the local server gets the telemetry opt-out, never a storage directory or a
  database provider from the shell or the file, and no driver extra for one.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import urllib.request

import pytest

TENANT = "https://tenant-x.cognee.ai"
OPTION_KEY = "ck-option-key"


@pytest.fixture(autouse=True)
def _options_suite_only(suite):
    if not suite.keys_from_plugin_options:
        pytest.skip(f"{suite.name}: reads its settings from the environment")


def _own_the_environment(monkeypatch):
    """Scrub the inherited plugin variables now, once, and keep what the test
    sets afterwards: the module loaders scrub again on every load, which would
    remove the options and switches a test sets before importing a module."""
    from utils import isolation

    isolation.scrub_cognee_env(monkeypatch)
    monkeypatch.setattr(isolation, "scrub_cognee_env", lambda _monkeypatch: None)


def _as_shell(monkeypatch):
    """This process is a command run from the Bash tool: no options, no marker."""
    _own_the_environment(monkeypatch)


def _as_hook(monkeypatch, suite, *, url="", key="", **options):
    """This process is a hook Claude Code started, with these plugin options."""
    _own_the_environment(monkeypatch)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(suite.scripts_dir.parent))
    for name, value in {"cognee_base_url": url, "cognee_api_key": key, **options}.items():
        var = "CLAUDE_PLUGIN_OPTION_" + name.upper()
        if value:
            monkeypatch.setenv(var, value)
        else:
            monkeypatch.delenv(var, raising=False)


def _env_file(home, text: str):
    path = home / ".cognee" / ".env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ── one loopback rule ─────────────────────────────────────────────────────

LOOPBACK_HOSTS = [
    ("localhost", True),
    ("LOCALHOST", True),
    ("127.0.0.1", True),
    ("127.5.5.5", True),
    ("::1", True),
    ("[::1]", True),
    ("127.0.0.1.evil.example", False),
    ("127.evil.com", False),
    ("localhost.evil.com", False),
    ("0.0.0.0", False),
    ("tenant-x.cognee.ai", False),
    ("", False),
]


@pytest.mark.parametrize("host, expected", LOOPBACK_HOSTS)
def test_loopback_is_localhost_or_a_literal_loopback_address(
    suite, isolated_modules, host, expected
):
    net = isolated_modules(suite, "_net")
    assert net.is_loopback_host(host) is expected


def test_userinfo_does_not_make_a_remote_url_loopback(suite, isolated_modules):
    net = isolated_modules(suite, "_net")
    assert not net.is_loopback_url("http://localhost@evil.example:8011")
    assert not net.is_loopback_url("http://127.0.0.1.evil.example:8011")
    assert net.is_loopback_url("http://[::1]:8011")
    assert net.is_loopback_url("http://127.0.0.1:8011/api")


@pytest.mark.parametrize(
    "url, expected",
    [
        ("http://localhost:8011", True),
        ("http://127.0.0.1:8011", True),
        ("http://127.0.0.2:8011", True),
        ("http://127.0.0.1.evil.example:8011", False),
        ("http://0.0.0.0:8011", False),
        (TENANT, False),
    ],
)
def test_every_loopback_check_gives_the_same_answer(
    suite, isolated_modules, hook_module, monkeypatch, url, expected
):
    _as_hook(monkeypatch, suite)
    pc = isolated_modules(suite, "_plugin_common")
    env_file = isolated_modules(suite, "_env_file")
    session_start = hook_module(suite, "session-start.py")
    assert env_file.is_loopback_url(url) is expected
    assert pc.service_url_is_local(url) is expected
    assert session_start._is_local_url(url) is expected


@pytest.mark.parametrize("source", ["file", "shell"])
def test_a_lookalike_local_url_cannot_redirect_the_cloud_key(
    suite, isolated_modules, temp_home, monkeypatch, source
):
    """``COGNEE_LOCAL_API_URL=http://127.0.0.1.evil.example`` is a remote host:
    it is not honoured, so the key option still goes to the configured tenant."""
    lookalike = "http://127.0.0.1.evil.example:8011"
    _as_hook(monkeypatch, suite, url=TENANT, key=OPTION_KEY)
    if source == "file":
        _env_file(temp_home, f"COGNEE_LOCAL_API_URL={lookalike}\n")
    else:
        monkeypatch.setenv("COGNEE_LOCAL_API_URL", lookalike)
    pc = isolated_modules(suite, "_plugin_common")
    assert pc.resolved_http_endpoint_auth() == (TENANT, OPTION_KEY)


# ── the Cloud key and the servers it is for ───────────────────────────────


def test_the_cloud_key_is_not_exported_for_a_local_server(suite, isolated_modules, monkeypatch):
    """Key option set, URL option empty: Cognee runs locally, and the key is
    not in the environment at all, so nothing can send it by reading it."""
    import os

    _as_hook(monkeypatch, suite, key=OPTION_KEY)
    isolated_modules(suite, "_plugin_common")
    assert "COGNEE_API_KEY" not in os.environ


def test_the_cloud_key_is_read_from_its_option_for_a_remote_server(
    suite, isolated_modules, monkeypatch
):
    """Sent to a remote server straight from the option; never copied into
    ``COGNEE_API_KEY``, where a shell value could stand in for it."""
    import os

    _as_hook(monkeypatch, suite, url=TENANT, key=OPTION_KEY)
    pc = isolated_modules(suite, "_plugin_common")
    assert "COGNEE_API_KEY" not in os.environ
    assert pc._api_key_with_source(TENANT) == (OPTION_KEY, "env_api_key")
    pc.resolve_runtime_mode()
    pc.resolved_http_endpoint_auth()
    assert "COGNEE_API_KEY" not in os.environ
    assert "COGNEE_PRINCIPAL_API_KEY" not in os.environ


def test_no_script_takes_the_key_for_a_request_from_the_environment(suite):
    """The exit watcher's probe, the Stop re-index and the session-start index
    ask the resolver, which gives a local server its own owner key."""
    for script in ("exit-watcher.py", "store-to-session.py"):
        source = (suite.scripts_dir / script).read_text(encoding="utf-8")
        assert 'os.environ.get("COGNEE_API_KEY"' not in source, script
    source = (suite.scripts_dir / "session-start.py").read_text(encoding="utf-8")
    index_call = source[source.index("autoindex_on_session_start(\n") :][:300]
    assert "COGNEE_API_KEY" not in index_call


def test_the_exit_watcher_probes_a_local_server_with_its_owner_key(
    suite, isolated_modules, hook_module, mock_server, monkeypatch
):
    """Cold local start with the Cloud key option set: the probe carries the
    owner key looked up at probe time, never the option value."""
    _as_hook(monkeypatch, suite, key=OPTION_KEY)
    monkeypatch.setenv("COGNEE_LOCAL_API_URL", mock_server.url)
    owner = "k-local-owner"
    mock_server.identity.seed_api_key(owner)
    mock_server.identity.user_api_keys["default_user@example.com"] = [{"key": owner}]
    pc = isolated_modules(suite, "_plugin_common")
    watcher = hook_module(suite, "exit-watcher.py")
    pc.write_connection_state("unreachable", mock_server.url)
    monkeypatch.setattr(watcher, "_last_conn_reprobe_at", 0.0)
    monkeypatch.setattr(watcher, "_conn_reprobe_interval", lambda: 0.0)

    watcher._reprobe_connection(mock_server.url)

    sent = [c["headers"].get("X-Api-Key") for c in mock_server.calls if "X-Api-Key" in c["headers"]]
    assert sent and set(sent) == {owner}, sent
    assert OPTION_KEY not in json.dumps(mock_server.calls)


def test_the_cloud_pin_without_a_url_option_sends_the_key_nowhere(
    suite, isolated_modules, mock_server, monkeypatch
):
    """``COGNEE_BACKEND=cloud`` with the key option set and no URL option: no
    key resolves, and nothing logs in to the local address for an owner key."""
    _as_hook(monkeypatch, suite, key=OPTION_KEY)
    monkeypatch.setenv("COGNEE_BACKEND", "cloud")
    monkeypatch.setenv("COGNEE_LOCAL_API_URL", mock_server.url)
    pc = isolated_modules(suite, "_plugin_common")
    assert pc._api_key_with_source() == ("", "missing")
    assert pc._api_key_with_source(mock_server.url) == ("", "missing")
    assert pc.load_cached_api_key(mock_server.url) == ""
    assert mock_server.calls == []


def test_hooks_send_nothing_under_the_cloud_pin_without_a_url_option(
    suite, run_hook, payloads, mock_server
):
    """Pinned to cloud, key option set, no URL option, and a local address that
    answers: no hook sends it a request, with or without a key, and session
    start says what is missing instead of claiming to run locally."""
    env = {
        "CLAUDE_PLUGIN_ROOT": str(suite.scripts_dir.parent),
        "CLAUDE_PLUGIN_OPTION_COGNEE_API_KEY": OPTION_KEY,
        "COGNEE_BACKEND": "cloud",
        "COGNEE_LOCAL_API_URL": mock_server.url,
    }
    prompt = payloads.user_prompt("what did we decide about retries?")
    for script, stdin in (
        ("session-start.py", payloads.session_start()),
        ("session-context-lookup.py", prompt),
        ("store-user-prompt.py", prompt),
    ):
        result = run_hook(suite, script, stdin=stdin, env=env)
        assert result.returncode == 0, (script, result.stderr)
        assert mock_server.calls == [], (script, mock_server.calls)
        if script == "session-start.py":
            message = json.loads(result.stdout)["systemMessage"]
            assert "the Cognee Cloud URL option is empty, so Cognee sends nothing" in message
            assert "running locally" not in message


def test_the_cloud_pin_from_the_env_file_is_tied_to_the_url_option_too(
    suite, isolated_modules, temp_home, monkeypatch
):
    import os

    _as_hook(monkeypatch, suite, key=OPTION_KEY)
    _env_file(temp_home, "COGNEE_BACKEND=cloud\n")
    pc = isolated_modules(suite, "_plugin_common")
    assert "COGNEE_API_KEY" not in os.environ
    assert pc._api_key_with_source() == ("", "missing")


def test_the_cloud_pin_applies_the_key_to_a_loopback_cloud_url_option(
    suite, isolated_modules, mock_server, monkeypatch
):
    """A self-hosted server on a loopback address used as Cloud: in a hook the
    option key is the key for it, and no default-user login happens."""
    _as_hook(monkeypatch, suite, url=mock_server.url, key=OPTION_KEY)
    monkeypatch.setenv("COGNEE_BACKEND", "cloud")
    pc = isolated_modules(suite, "_plugin_common")
    assert pc.cloud_key_applies(mock_server.url)
    assert pc.resolved_http_endpoint_auth() == (mock_server.url, OPTION_KEY)
    assert mock_server.calls == []


def test_a_loopback_url_option_without_the_pin_gets_the_owner_key(
    suite, isolated_modules, mock_server, monkeypatch
):
    _as_hook(monkeypatch, suite, url=mock_server.url, key=OPTION_KEY)
    owner = "k-local-owner"
    mock_server.identity.seed_api_key(owner)
    mock_server.identity.user_api_keys["default_user@example.com"] = [{"key": owner}]
    pc = isolated_modules(suite, "_plugin_common")
    assert not pc.cloud_key_applies(mock_server.url)
    assert pc._api_key_with_source(mock_server.url) == (owner, "local_owner_key")


def test_the_shell_guard_fires_for_a_pinned_loopback_cloud_url(
    suite, isolated_modules, temp_home, mock_server, monkeypatch, capsys
):
    """From a shell (no key) against the pinned loopback Cloud URL: stop with
    exit 2, as for any Cloud server, instead of logging in as the default user."""
    _as_shell(monkeypatch)
    settings = temp_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps(
            {
                "pluginConfigs": {
                    "cognee-memory@cognee": {"options": {"cognee_base_url": mock_server.url}}
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("COGNEE_BACKEND", "cloud")
    pc = isolated_modules(suite, "_plugin_common")
    with pytest.raises(SystemExit) as stopped:
        pc.require_shell_auth(mock_server.url, pc.load_cached_api_key(mock_server.url), "tool")
    assert stopped.value.code == 2
    assert "hooks only" in capsys.readouterr().err
    assert mock_server.calls == []


# ── one owner key for every process ───────────────────────────────────────

HASHED = "pbkdf2_sha256$600000$" + "ab" * 32


def test_a_stored_hash_is_never_taken_for_a_key(suite, isolated_modules):
    pc = isolated_modules(suite, "_plugin_common")
    assert pc.is_hashed_key(HASHED)
    assert pc.first_usable_key([{"key": HASHED}, {"key": "k-good"}]) == "k-good"
    assert pc.first_usable_key([{"key": "************"}, {"key": HASHED}]) == ""
    assert pc.first_usable_key([{"key": "k-first"}, {"key": "k-second"}]) == "k-first"


def test_a_server_that_masks_every_key_is_recognised(suite, isolated_modules):
    pc = isolated_modules(suite, "_plugin_common")
    assert pc.server_hides_keys([{"key": "************"}, {"key": "****"}])
    assert not pc.server_hides_keys([{"key": "************"}, {"key": "k-good"}])
    assert not pc.server_hides_keys([{"key": HASHED}])
    assert not pc.server_hides_keys([])


def _local_session(suite, monkeypatch, mock_server, listed):
    _as_hook(monkeypatch, suite)
    monkeypatch.setenv("COGNEE_LOCAL_API_URL", mock_server.url)
    mock_server.identity.user_api_keys["default_user@example.com"] = list(listed)


def _minted(mock_server) -> list:
    return [
        c
        for c in mock_server.calls
        if c["path"] == "/api/v1/auth/api-keys" and c["method"] == "POST"
    ]


def test_session_start_and_hooks_agree_on_the_key_past_a_stored_hash(
    suite, isolated_modules, hook_module, mock_server, monkeypatch
):
    """Listed ``[hash, good]``: two session starts and a hook all use ``good``
    and nothing is minted."""
    import asyncio

    good = "k-good"
    mock_server.identity.seed_api_key(good)
    _local_session(suite, monkeypatch, mock_server, [{"key": HASHED}, {"key": good}])
    session_start = hook_module(suite, "session-start.py")
    pc = isolated_modules(suite, "_plugin_common")
    config = isolated_modules(suite, "config").load_config()

    for _ in range(2):
        pc._LOCAL_OWNER_KEYS.clear()
        key = asyncio.run(session_start._resolve_single_principal_key(mock_server.url, config))
        assert key == good
    pc._LOCAL_OWNER_KEYS.clear()
    assert pc._api_key_with_source(mock_server.url) == (good, "local_owner_key")
    assert _minted(mock_server) == []


def test_a_key_minted_past_a_stored_hash_is_minted_once(
    suite, isolated_modules, hook_module, mock_server, monkeypatch
):
    """Listed ``[hash]`` only: the first session start mints, and the next one,
    and every hook, find that key instead of minting again."""
    import asyncio

    _local_session(suite, monkeypatch, mock_server, [{"key": HASHED}])
    session_start = hook_module(suite, "session-start.py")
    pc = isolated_modules(suite, "_plugin_common")
    config = isolated_modules(suite, "config").load_config()

    first = asyncio.run(session_start._resolve_single_principal_key(mock_server.url, config))
    assert first and first != HASHED
    pc._LOCAL_OWNER_KEYS.clear()
    second = asyncio.run(session_start._resolve_single_principal_key(mock_server.url, config))
    pc._LOCAL_OWNER_KEYS.clear()
    assert second == first
    assert pc._api_key_with_source(mock_server.url) == (first, "local_owner_key")
    assert len(_minted(mock_server)) == 1


def test_a_server_that_hides_its_keys_is_refused_not_minted_on(
    suite, isolated_modules, hook_module, mock_server, monkeypatch
):
    import asyncio

    _local_session(suite, monkeypatch, mock_server, [{"key": "************"}])
    session_start = hook_module(suite, "session-start.py")
    pc = isolated_modules(suite, "_plugin_common")
    config = isolated_modules(suite, "config").load_config()

    with pytest.raises(RuntimeError) as refused:
        asyncio.run(session_start._resolve_single_principal_key(mock_server.url, config))
    assert "hides its API keys" in str(refused.value)
    assert _minted(mock_server) == []
    pc._LOCAL_OWNER_KEYS.clear()
    assert pc._api_key_with_source(mock_server.url) == ("", "missing")


# ── no proxy for this machine ─────────────────────────────────────────────


class _Listener:
    """A socket that records the first request line and headers it is sent."""

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
    """``HTTP_PROXY`` pointing at a listener, with nothing excluded from it."""
    listener = _Listener()
    # urlopen caches its opener, and the proxy settings with it, on first use:
    # a plugin process sets its environment before that, a test process did not.
    monkeypatch.setattr(urllib.request, "_opener", None)
    for name in ("HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{listener.port}")
    for name in ("NO_PROXY", "no_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    yield listener
    listener.close()


def test_a_request_to_this_machine_never_goes_through_the_proxy(
    suite, isolated_modules, mock_server, monkeypatch, proxy
):
    _as_hook(monkeypatch, suite)
    monkeypatch.setenv("COGNEE_LOCAL_API_URL", mock_server.url)
    owner = "k-local-owner"
    mock_server.identity.seed_api_key(owner)
    mock_server.identity.user_api_keys["default_user@example.com"] = [{"key": owner}]
    pc = isolated_modules(suite, "_plugin_common")

    # The owner-key login and a keyed request, both to the local server.
    assert pc._api_key_with_source(mock_server.url) == (owner, "local_owner_key")
    pc._json_http_request("/api/v1/datasets", method="GET")

    assert proxy.requests == []
    assert any(c["path"] == "/api/v1/datasets" for c in mock_server.calls)


def test_a_remote_request_still_uses_the_proxy(suite, isolated_modules, proxy):
    net = isolated_modules(suite, "_net")
    request = urllib.request.Request("http://tenant-x.cognee.invalid/health")
    with net.open_url(request, timeout=5) as response:
        response.read()
    assert proxy.requests and proxy.requests[0].startswith(
        "GET http://tenant-x.cognee.invalid/health"
    )


def test_no_shipped_script_opens_a_url_except_through_the_shared_opener(suite):
    # _coding_memory.py is kept byte-identical with the Codex plugin's copy;
    # its one direct call is an ImportError fallback this plugin never reaches
    # (the request goes through urlopen_following_307, which uses the opener).
    exempt = {"_net.py", "_coding_memory.py"}
    offenders = []
    for path in sorted(suite.scripts_dir.parent.rglob("*.py")):
        if "__pycache__" in path.parts or path.name in exempt:
            continue
        # The bare name: a default argument or a reference sends a request
        # through the proxy just as a call does.
        if "urllib.request.urlopen" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(suite.scripts_dir.parent)))
    assert offenders == []


# ── the env-file parser keeps allowlisted values only ─────────────────────

SHARED_FILE = """\
COGNEE_RECALL_TIMEOUT=8
OPENAI_API_KEY=sk-upper-SECRET
openai_api_key=sk-lower-SECRET
OpenAI_Api_Key=sk-mixed-SECRET
DATABASE_URL=postgres://user:db-SECRET@db/prod
GH_PAT=ghp-SECRET
DB_PASS=pass-SECRET
COGNEE_LOCAL_API_URL=http://user:url-SECRET@tenant.example.com
"""


def test_the_parser_keeps_no_value_outside_the_allowlist(suite, isolated_modules, temp_home):
    env_file = isolated_modules(suite, "_env_file")
    path = _env_file(temp_home, SHARED_FILE)

    values, refused, ignored = env_file._parse(path)

    assert values == {"COGNEE_RECALL_TIMEOUT": "8"}
    assert "SECRET" not in json.dumps([values, refused, ignored])
    assert env_file.parse_env_file(path) == {"COGNEE_RECALL_TIMEOUT": "8"}
    assert {"OPENAI_API_KEY", "openai_api_key", "OpenAI_Api_Key"} <= set(refused)
    assert {"DATABASE_URL", "GH_PAT", "DB_PASS", "COGNEE_LOCAL_API_URL"} <= set(ignored)


def test_doctor_names_ignored_settings_without_their_values(
    suite, isolated_modules, temp_home, monkeypatch
):
    _as_hook(monkeypatch, suite)
    _env_file(temp_home, SHARED_FILE)
    env_file = isolated_modules(suite, "_env_file")
    status = env_file.env_file_status()
    assert status["keys"] == ["COGNEE_RECALL_TIMEOUT"]
    assert "DATABASE_URL" in status["ignored"] and "openai_api_key" in status["refused"]
    assert "SECRET" not in json.dumps(status)


# ── the allowlist covers every setting the scripts read ───────────────────


@pytest.mark.parametrize(
    "name, value",
    [
        ("COGNEE_PLUGIN_READ_DATASET_IDS", '["11111111-1111-4111-8111-111111111111"]'),
        ("COGNEE_SESSION_COMPANION_DATASET", "true"),
    ],
)
def test_the_two_dataset_settings_load_from_the_env_file(
    suite, isolated_modules, temp_home, monkeypatch, name, value
):
    import os

    _as_hook(monkeypatch, suite)
    monkeypatch.delenv(name, raising=False)
    _env_file(temp_home, f"{name}={value}\n")
    isolated_modules(suite, "_env_file").load_env_file()
    assert os.environ.get(name) == value


# Read by the scripts, deliberately not loadable from the file: hand-offs the
# plugin sets for its own child processes, and settings that are shell-only.
_NOT_FROM_THE_FILE = {
    # set by the plugin for its own processes
    "COGNEE_AGENT_SESSION_NAME",
    "COGNEE_SESSION_KEY",
    "COGNEE_SYNC_DATASET",
    "COGNEE_SYNC_SESSION_ID",
    "COGNEE_USER_ID",
    "COGNEE_PLUGIN_IN_VENV",
    "COGNEE_PLUGIN_LLM_ENV_APPLIED",
    # shell-only by design: which file to read, a session to resume, a path to
    # an executable, and the local URL (loopback only, checked separately)
    "COGNEE_ENV_FILE",
    "COGNEE_SESSION_ID",
    "COGNEE_LOCAL_API_URL",
}

_ENV_READ = re.compile(
    r"""(?:environ\.get\(|getenv\(|environ\[|_env\(|_float_env\(|_int_env\(|_bool_env\()\s*["'](COGNEE_[A-Z0-9_]+)["']"""
)


def test_every_documented_setting_the_scripts_read_is_accounted_for(suite, isolated_modules):
    """A user-facing ``COGNEE_*`` setting must be loadable from the file,
    refused (a credential or connection setting), or listed above as internal
    or shell-only: an allowlist that silently drops one changes behaviour on
    upgrade with no message."""
    env_file = isolated_modules(suite, "_env_file")
    plugin = suite.scripts_dir.parent
    read: set[str] = set()
    for path in list(plugin.rglob("*.py")) + list(plugin.rglob("*.sh")):
        if "__pycache__" not in path.parts:
            read |= set(_ENV_READ.findall(path.read_text(encoding="utf-8", errors="replace")))
    documented = set(re.findall(r"COGNEE_[A-Z0-9_]+", (plugin / "README.md").read_text("utf-8")))
    unaccounted = sorted(
        name
        for name in read & documented
        if name not in env_file._TUNING_NAMES
        and not env_file.refused(name)
        and name not in _NOT_FROM_THE_FILE
    )
    assert unaccounted == []


# ── the local server's environment and extras ─────────────────────────────


def test_the_server_gets_the_telemetry_opt_out(suite, hook_module, monkeypatch):
    _as_hook(monkeypatch, suite)
    monkeypatch.setenv("TELEMETRY_DISABLED", "1")
    session_start = hook_module(suite, "session-start.py")
    assert session_start._server_env().get("TELEMETRY_DISABLED") == "1"


@pytest.mark.parametrize("source", ["file", "shell"])
def test_storage_settings_never_reach_the_server_and_are_named_once(
    suite, hook_module, temp_home, monkeypatch, source
):
    _as_hook(monkeypatch, suite)
    for name in ("DATA_ROOT_DIRECTORY", "SYSTEM_ROOT_DIRECTORY", "DB_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    if source == "file":
        _env_file(
            temp_home,
            "DATA_ROOT_DIRECTORY=/data/mine\nSYSTEM_ROOT_DIRECTORY=/data/sys\nDB_PROVIDER=postgres\n",
        )
    else:
        monkeypatch.setenv("DATA_ROOT_DIRECTORY", "/data/mine")
        monkeypatch.setenv("SYSTEM_ROOT_DIRECTORY", "/data/sys")
        monkeypatch.setenv("DB_PROVIDER", "postgres")
    session_start = hook_module(suite, "session-start.py")

    env = session_start._server_env()
    assert env["DATA_ROOT_DIRECTORY"] == str(temp_home / ".cognee" / "data")
    assert env["SYSTEM_ROOT_DIRECTORY"] == str(temp_home / ".cognee" / "system")
    assert "DB_PROVIDER" not in env

    first = session_start._option_notices()
    assert session_start.STORAGE_NOTICE in first
    assert "/data/mine" not in json.dumps(first)
    flag = json.loads(session_start._STORAGE_NOTICE_FLAG.read_text(encoding="utf-8"))
    assert {"DATA_ROOT_DIRECTORY", "SYSTEM_ROOT_DIRECTORY", "DB_PROVIDER"} <= set(flag["names"])
    assert session_start.STORAGE_NOTICE not in session_start._option_notices()


def test_no_storage_settings_means_no_storage_notice(suite, hook_module, monkeypatch):
    _as_hook(monkeypatch, suite)
    for name in (
        "DATA_ROOT_DIRECTORY",
        "SYSTEM_ROOT_DIRECTORY",
        "DB_PROVIDER",
        "VECTOR_DB_PROVIDER",
        "GRAPH_DATABASE_PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)
    session_start = hook_module(suite, "session-start.py")
    assert session_start.STORAGE_NOTICE not in session_start._option_notices()


@pytest.mark.parametrize(
    "name, value",
    [
        ("DB_PROVIDER", "postgres"),
        ("VECTOR_DB_PROVIDER", "pgvector"),
        ("GRAPH_DATABASE_PROVIDER", "neo4j"),
        ("LLM_PROVIDER", "ollama"),
    ],
)
def test_a_provider_the_server_will_not_use_needs_no_extra(
    suite, hook_module, monkeypatch, name, value
):
    """The plugin's server runs on the built-in stores and has no Ollama
    support in this version: none of these may block the start for a driver."""
    _as_hook(monkeypatch, suite)
    session_start = hook_module(suite, "session-start.py")
    monkeypatch.setenv(name, value)
    assert session_start._detect_required_extras() == []


def test_fastembed_still_needs_its_extra(suite, hook_module, monkeypatch):
    _as_hook(monkeypatch, suite)
    session_start = hook_module(suite, "session-start.py")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fastembed")
    assert session_start._detect_required_extras() == ["fastembed"]


def test_no_shipped_text_offers_ollama_as_a_provider(suite):
    plugin = suite.scripts_dir.parent
    options = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text("utf-8"))[
        "userConfig"
    ]
    for name in ("llm_provider", "embedding_provider"):
        assert "ollama" not in options[name]["description"].lower(), name
    readme = (plugin / "README.md").read_text("utf-8")
    offered = [
        line
        for line in readme.splitlines()
        if "ollama" in line.lower() and "not supported" not in line.lower()
    ]
    assert offered == []
