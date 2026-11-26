"""A fake ``opener`` for the code analysis client and an indexed checkout.

The client (``_code_analysis`` and the modules over it) takes an ``opener`` the
way ``_code_graph`` does. ``FakeServer`` answers by ``(method, path)`` and
records every request, so a test states what the server says and reads what
the plugin asked. No socket is opened.
"""

from __future__ import annotations

import io
import json
import subprocess
import urllib.error
from pathlib import Path

ANALYZE = "/api/v1/code/analyze"
RECALL = "/api/v1/recall"
OPENAPI = "/openapi.json"
SPEC_WITH_ROUTE = {"paths": {ANALYZE: {"post": {}}, RECALL: {"post": {}}}}
SPEC_WITHOUT_ROUTE = {"paths": {RECALL: {"post": {}}}}


class _Reply:
    def __init__(self, status: int, body):
        self.status = status
        self._raw = json.dumps(body).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeServer:
    """``answers[(method, path)]`` is ``(status, body)``, a callable taking the
    request's JSON body and returning one, or an exception to raise."""

    def __init__(self, url: str = "http://127.0.0.1:1"):
        self.url = url
        self.rt = {"service_url": url, "api_key": "key"}
        self.answers = {("GET", OPENAPI): (200, SPEC_WITH_ROUTE)}
        self.calls: list = []

    def on(self, method: str, path: str, answer) -> "FakeServer":
        self.answers[(method, path)] = answer
        return self

    def bodies(self, path: str) -> list:
        return [body for method, called, body in self.calls if called == path]

    def __call__(self, req, timeout=None):
        path = req.full_url[len(self.url) :].split("?", 1)[0]
        body = json.loads(req.data.decode("utf-8")) if req.data else None
        self.calls.append((req.get_method(), path, body))
        answer = self.answers.get((req.get_method(), path))
        if answer is None:
            answer = (404, {"detail": "Not Found"})
        if callable(answer):
            answer = answer(body)
        if isinstance(answer, BaseException):
            raise answer
        status, payload = answer
        if status >= 400:
            raise urllib.error.HTTPError(
                req.full_url, status, "error", {}, io.BytesIO(json.dumps(payload).encode("utf-8"))
            )
        return _Reply(status, payload)


def code_rows(raw: dict) -> list:
    """A ``/api/v1/recall`` reply carrying one CODE operation result."""
    return [{"kind": "code", "source": "code", "text": json.dumps(raw), "raw": raw}]


def make_repo(root: Path) -> Path:
    """A one-commit git repository with one Python file."""
    root.mkdir(parents=True, exist_ok=True)

    def git(*args):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    git("config", "commit.gpgsign", "false")
    (root / "pricing.py").write_text(
        "def line_total(price, qty):\n    return price * qty\n\n\n"
        "def total(lines):\n    return sum(line_total(p, q) for p, q in lines)\n",
        encoding="utf-8",
    )
    git("add", "-A")
    git("commit", "-q", "-m", "first")
    return root


def index_by_path(code_graph, repo: Path, dataset: str = "codebase-proj-1234abcd") -> dict:
    """Record ``repo`` as indexed by path, as ``cognee-index-repo.sh`` leaves it."""
    import os

    root = os.path.realpath(str(repo))
    state = {
        "spec": root,
        "spec_kind": "path",
        "repo_root": root,
        "dataset": dataset,
        "fingerprint": code_graph.git_fingerprint(root),
    }
    code_graph.save_repo_state(state)
    return state
