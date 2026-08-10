"""Helpers for the live tier: real cognee server, real LLM, real graph.

The driver is the same as ``e2e/``'s — hook scripts run as subprocesses with
synthetic payloads — but the backend is a real cognee server that *the plugin
boots itself*, so the whole memory chain runs for real: identity, capture,
warmup drain, improve, cognify, recall.

Facts this module encodes (each learned the hard way in a spike; changing them
tends to make live tests pass while testing nothing):

* ``COGNEE_BASE_URL`` is what selects server mode. ``COGNEE_LOCAL_API_URL``
  alone leaves ``load_config()`` with an empty ``base_url``, so the plugin runs
  local-SDK mode, boots no server at all, and every hook still exits 0.
* The port must be a pinned ephemeral one. The default is 8011, which is very
  likely a developer's own running cognee — a live test must never attach to it
  and write test data into a real graph.
* ``improve_fired`` is NOT a completion signal: it can report ``ok: true`` with
  empty ``cognify``/``memify`` (no ``dataset_id`` came back, so nothing was
  polled) while the graph was in fact written. The only honest readiness gate is
  "poll recall until the content is retrievable".
* Never shell out through a wrapper: macOS has no GNU ``timeout``, and ``cmd &``
  inside a pipeline gets ``/dev/null`` as stdin, which silently empties hook
  payloads. ``subprocess.run(input=..., timeout=...)`` avoids both.
* With cognee >= 1.4.1 an omitted session id defaults to a dataset-derived
  session, so the **dataset** is the scoping key across sessions. A stable id is
  still threaded within one session because ``improve`` bridges the named
  session's cache, and ``session-start.py`` refuses to register without one.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .suites import Suite, state_dir

#: The port a developer's own cognee almost certainly occupies.
FORBIDDEN_PORT = 8011


def free_port() -> int:
    """An ephemeral port nothing is listening on (never the default 8011)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    if port == FORBIDDEN_PORT:  # pragma: no cover - astronomically unlikely
        raise RuntimeError("refusing to use port 8011 (a real server likely owns it)")
    return port


def seed_plugin_venv(home: Path) -> bool:
    """Symlink the host's plugin venv into ``home`` so SessionStart skips install.

    Building it means ``uv`` plus a pinned cognee download — minutes per run.
    Symlinking caches the *install* only; boot, health-gating, registration and
    every pipeline still run for real. Returns False when no host venv exists
    (the plugin will then build its own, slowly).
    """
    host_venv = Path.home() / ".cognee-plugin" / "venv"
    if not host_venv.exists():
        return False
    link = home / ".cognee-plugin" / "venv"
    link.parent.mkdir(parents=True, exist_ok=True)
    if not link.exists():
        link.symlink_to(host_venv)
    return True


def build_live_env(
    *,
    home: Path,
    project: Path,
    base_url: str,
    dataset: str,
    llm_api_key: str,
    suite: Suite,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """The environment a live hook subprocess runs in.

    Inherited COGNEE_*/LLM_*/CLAUDE_*/CODEX_* are scrubbed so a developer's shell
    (or the parent agent's own session vars) cannot leak in and redirect the run
    at a real server.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("COGNEE_", "LLM_", "CLAUDE", "CODEX"))
    }
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            suite.cwd_env: str(project),
            # Selects server mode AND the port the plugin boots on.
            "COGNEE_BASE_URL": base_url,
            "COGNEE_LOCAL_API_URL": base_url,
            "COGNEE_PLUGIN_DATASET": dataset,
            "LLM_API_KEY": llm_api_key,
            "LLM_INSTRUCTOR_MODE": "json_schema_mode",
            # Boot synchronously: a lazy bootstrap lets the first prompt race the
            # server and silently skip recall.
            "COGNEE_LAZY_BOOTSTRAP": "0",
            "COGNEE_UPDATE_CHECK": "off",
            "COGNEE_IDLE_DISABLED": "1",
            "COGNEE_SYNC_START_DELAY": "0.5",
            # Test-only patience for the per-prompt recall. In production these
            # are deliberately tight (COGNEE_RECALL_TIMEOUT 2.5s per scope,
            # COGNEE_RECALL_BUDGET 4s overall) so memory can never stall an
            # interactive prompt — and on a *cold* server the first graph query
            # exceeds that and is correctly dropped as "slow". These tests ask
            # "does memory cross sessions", not "is cold-start recall fast", so
            # they wait. Cold-start latency deserves its own scenario rather
            # than silently failing this one.
            "COGNEE_RECALL_TIMEOUT": "30",
            "COGNEE_RECALL_BUDGET": "60",
        }
    )
    if extra:
        env.update(extra)
    return env


def server_health(base_url: str, timeout: float = 3.0) -> int | None:
    """The /health status code, or None when nothing is listening."""
    try:
        with urllib.request.urlopen(f"{base_url}/health", timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception:
        return None


def reap_port(port: int) -> list[str]:
    """Kill anything still listening on ``port``; return the pids killed."""
    found = subprocess.run(
        ["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True
    ).stdout.split()
    for pid in found:
        subprocess.run(["kill", pid], capture_output=True)
    return found


def hook_events(suite: Suite, home: Path) -> list[tuple[str, dict]]:
    """Every (event, detail) the hooks have logged so far, in order."""
    path = state_dir(suite, home) / "hook.log"
    if not path.exists():
        return []
    events: list[tuple[str, dict]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except Exception:
            continue
        events.append((str(entry.get("event", "")), entry.get("detail") or {}))
    return events


def wait_for_event(
    suite: Suite, home: Path, name: str, *, deadline: float, interval: float = 2.0
) -> dict | None:
    """Poll hook.log until ``name`` appears; return its detail, or None on timeout."""
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        for event, detail in hook_events(suite, home):
            if event == name:
                return detail
        time.sleep(interval)
    return None


@dataclass
class HookRun:
    """One hook subprocess invocation."""

    script: str
    args: tuple[str, ...]
    returncode: int | str
    stdout: str
    stderr: str
    seconds: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def json_output(self) -> dict:
        """The hook's stdout parsed as the host would parse it ({} when empty)."""
        text = self.stdout.strip()
        if not text:
            return {}
        return json.loads(text)

    def additional_context(self) -> str:
        """The context this hook would have injected into the agent's prompt."""
        payload = self.json_output()
        hook_specific = payload.get("hookSpecificOutput") or {}
        return str(hook_specific.get("additionalContext") or "")


@dataclass
class LiveSession:
    """Drives one simulated agent session through the real hook scripts.

    A "fresh session" is simply another LiveSession on the same dataset — with
    cognee >= 1.4.1 the dataset is the scoping key, so nothing needs correlating
    between them.
    """

    suite: Suite
    home: Path
    project: Path
    env: dict[str, str]
    session_id: str
    runs: list[HookRun] = field(default_factory=list)

    # -- plumbing ----------------------------------------------------------
    def run(self, script: str, payload: dict, *args: str, timeout: float = 900.0) -> HookRun:
        started = time.monotonic()
        try:
            proc = subprocess.run(
                [sys.executable, str(self.suite.scripts_dir / script), *args],
                input=json.dumps(payload),
                env=self.env,
                cwd=str(self.project),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            rc: int | str = proc.returncode
            out, err = proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            rc, out, err = "TIMEOUT", "", ""
        run = HookRun(script, args, rc, out, err, time.monotonic() - started)
        self.runs.append(run)
        return run

    def _payload(self, **fields: Any) -> dict:
        base = {"session_id": self.session_id, "cwd": str(self.project)}
        base.update(fields)
        return base

    # -- the session lifecycle --------------------------------------------
    def start(self, source: str = "startup") -> HookRun:
        """SessionStart — the plugin boots/joins its server and registers."""
        return self.run(
            "session-start.py",
            self._payload(hook_event_name="SessionStart", source=source),
        )

    def prompt(self, text: str, *, turn_id: str = "t1") -> HookRun:
        """UserPromptSubmit — capture the user's prompt for this turn."""
        return self.run(
            "store-user-prompt.py",
            self._payload(hook_event_name="UserPromptSubmit", prompt=text, turn_id=turn_id),
            timeout=300,
        )

    def recall(self, text: str, *, turn_id: str = "t1") -> HookRun:
        """UserPromptSubmit's sibling — what memory would be injected for ``text``.

        The returned run's ``additional_context()`` is the plugin's actual
        contract with the agent: the memory it hands the model.
        """
        return self.run(
            "session-context-lookup.py",
            self._payload(hook_event_name="UserPromptSubmit", prompt=text, turn_id=turn_id),
            timeout=300,
        )

    def answer(self, text: str, *, turn_id: str = "t1") -> HookRun:
        """Stop — capture the assistant's answer for this turn."""
        return self.run(
            "store-to-session.py",
            self._payload(
                hook_event_name="Stop",
                assistant_message=text,
                last_assistant_message=text,
                turn_id=turn_id,
            ),
            "--stop",
            timeout=300,
        )

    def tool(self, name: str, tool_input: Any, output: Any, *, turn_id: str = "t1") -> HookRun:
        """PostToolUse — capture one tool trace."""
        return self.run(
            "store-to-session.py",
            self._payload(
                hook_event_name="PostToolUse",
                tool_name=name,
                tool_input=tool_input,
                tool_response=output,
                tool_output=output,
                turn_id=turn_id,
            ),
            timeout=300,
        )

    def end(self, reason: str = "exit") -> HookRun:
        """SessionEnd — hands the graph write to a detached worker and returns.

        The hook exiting says nothing about the write; wait on
        ``sync_bridge_done`` (and ultimately on recall) instead.
        """
        return self.run(
            "sync-session-to-graph.py",
            self._payload(hook_event_name="SessionEnd", reason=reason),
            "--session-end",
        )

    # -- observability -----------------------------------------------------
    def wait_for_sync(self, *, deadline: float = 600.0) -> dict | None:
        """Wait for the detached worker to report the session was bridged."""
        return wait_for_event(self.suite, self.home, "sync_bridge_done", deadline=deadline)


@dataclass
class GraphClient:
    """Queries the real graph the way cognee-search does, independent of hooks."""

    base_url: str
    dataset: str
    home: Path
    suite: Suite

    @property
    def api_key(self) -> str:
        """The principal key the plugin minted and cached in this temp HOME."""
        cache = self.home / ".cognee-plugin" / "api_key.json"
        if not cache.exists():
            return ""
        try:
            return str((json.loads(cache.read_text(encoding="utf-8")) or {}).get("api_key") or "")
        except Exception:
            return ""

    def recall(self, query: str, *, top_k: int = 5, timeout: float = 180.0) -> str:
        """POST /api/v1/recall scoped to this run's dataset; returns raw text."""
        body = json.dumps(
            {
                "query": query,
                "top_k": top_k,
                "only_context": True,
                "scope": "auto",
                "datasets": [self.dataset],
            }
        ).encode()
        req = urllib.request.Request(
            f"{self.base_url}/api/v1/recall",
            data=body,
            headers={"Content-Type": "application/json", "X-Api-Key": self.api_key},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")

    def wait_until_recalled(
        self,
        query: str,
        *terms: str,
        deadline: float = 600.0,
        interval: float = 10.0,
    ) -> str:
        """Poll recall until every term is present. Returns the matching body.

        This is the readiness gate for a graph write. ``improve_fired`` cannot
        serve that role: it reports ok with empty cognify/memify status when the
        improve response carried no dataset_id, even though the graph was built.
        """
        end = time.monotonic() + deadline
        last = ""
        while time.monotonic() < end:
            try:
                last = self.recall(query)
            except Exception as exc:  # server may be booting or briefly down
                last = f"<recall error: {type(exc).__name__}: {exc}>"
            lowered = last.lower()
            if all(term.lower() in lowered for term in terms):
                return last
            time.sleep(interval)
        missing = [t for t in terms if t.lower() not in last.lower()]
        raise AssertionError(
            f"graph never returned {missing} for {query!r} within {deadline}s.\n"
            f"last recall body: {last[:2000]}"
        )
