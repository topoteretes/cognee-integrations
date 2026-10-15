"""Opt-in answer sampling. Capture is local; delivery runs outside the answer path."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError

from _file_lock import file_lock
from _injection_ledger import atomic_json

HOST = "codex"

_PATTERNS = [
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
    r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+",
    r"(?i)\b(?:password|passwd|api[_-]?key|secret|token)\s*[:=]\s*[^\s,;]+",
    r"\b(?:sk-(?:proj-|ant-)?|gh[pousr]_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{12,}",
    r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
    r"(?i)\b(?:https?|postgres(?:ql)?|redis|mongodb)://[^\s<>]+",
    r"(?:/Users/|/home/|[A-Z]:\\Users\\)[^\s/\\]+",
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
]


def sanitize(value):
    mapping = {}

    def text(s):
        def replace(match):
            original = match.group(0)
            if original not in mapping:
                mapping[original] = f"[REDACTED_{len(mapping) + 1}]"
            return mapping[original]

        for pattern in _PATTERNS:
            s = re.sub(pattern, replace, s)
        return s

    def visit(v):
        if isinstance(v, str):
            return text(v)
        if isinstance(v, list):
            return [visit(x) for x in v]
        if isinstance(v, dict):
            return {k: visit(x) for k, x in v.items()}
        return v

    return visit(value)


def enabled():
    return os.environ.get("COGNEE_EVALUATION_CAPTURE", "false").lower() == "true"


def identity():
    from _cloud_worker import root, target
    from _plugin_common import load_resolved

    resolved = load_resolved(identity=False)
    dataset_id = resolved.get("dataset_id", "")
    binding = {**target(), "dataset_id": dataset_id}
    key = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    return root() / "evaluations" / key, binding


def read(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def remember_context(text):
    if not enabled():
        return
    from _capture_policy import redact
    from _plugin_common import get_session_key

    directory, binding = identity()
    key = get_session_key()
    if not key or not binding["dataset_id"]:
        return
    path = directory / (hashlib.sha256(key.encode()).hexdigest() + ".context")
    # Keep the bounded evidence already injected this session; never re-query later memory.
    with file_lock(directory / "capture.lock", timeout=0.1) as acquired:
        if not acquired:
            return
        old = read(path)
        previous = old.get("text", "") if time.time() - old.get("at", 0) < 86400 else ""
        safe = sanitize(redact(text))
        combined = (previous + "\n" + safe)[-16000:] if safe not in previous else previous
        atomic_json(path, {"at": time.time(), "text": combined})


def enqueue(prompt, answer, *, model="unknown", event_id=""):
    if not enabled() or not prompt or not answer:
        return
    from _capture_policy import redact
    from _plugin_common import get_session_key

    directory, binding = identity()
    if not binding["dataset_id"]:
        return  # Never guess which dataset owns an evaluation.
    key = get_session_key()
    with file_lock(directory / "capture.lock", timeout=0.1) as acquired:
        if not acquired:
            return
        for path in directory.glob("*.context"):
            if time.time() - path.stat().st_mtime > 86400:
                path.unlink(missing_ok=True)
        evidence = read(directory / (hashlib.sha256(key.encode()).hexdigest() + ".context"))
        text = evidence.get("text", "") if time.time() - evidence.get("at", 0) < 86400 else ""
        # Deterministic for repeated Stop delivery of the same host turn.
        event = (
            str(uuid.uuid5(uuid.NAMESPACE_URL, key + ":" + event_id))
            if event_id
            else str(uuid.uuid4())
        )
        path = directory / (event + ".json")
        if path.exists():
            return
        payload = {
            "event_id": event,
            "dataset_id": binding["dataset_id"],
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "consent_version": "answer-evaluation-v1",
            "prompt": prompt[:8000],
            "answer": answer[:16000],
            "context": "",
            "model": str(model)[:200],
            "plugin": HOST,
            "recall_budget_seconds": 45,
            "evidence": [
                {
                    "source_id": "plugin-injection",
                    "version": hashlib.sha256(text.encode()).hexdigest(),
                    "text": text,
                }
            ]
            if text
            else [],
        }
        payload = sanitize(redact(payload))
        if len(json.dumps(payload).encode()) > 64000:
            return
        atomic_json(path, {"binding": binding, "body": payload, "created_at": time.time()})
        files = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for old in files[:-100]:
            old.unlink(missing_ok=True)


def launch_delivery():
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve())],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def drain():
    from _plugin_common import _json_http_request
    from _quality_summary import refresh_current

    directory, binding = identity()
    # Expiry and opt-out sweep all bindings, including retired datasets/accounts.
    from _cloud_worker import root

    for old in (root() / "evaluations").glob("*/*"):
        if old.suffix in {".json", ".context"} and (
            not enabled() or time.time() - old.stat().st_mtime > 86400
        ):
            old.unlink(missing_ok=True)
    with file_lock(directory / "delivery.lock") as acquired:
        if not acquired:
            return
        # Refresh works with both local and cloud URLs, even with capture disabled.
        refresh_current()
        for path in sorted(directory.glob("*.json"))[:6]:
            row = read(path)
            if not enabled() or time.time() - row.get("created_at", 0) > 86400:
                path.unlink(missing_ok=True)
                continue
            if row.get("binding") != binding:
                continue
            try:
                result = _json_http_request("/api/v1/evaluations/traces", row["body"], timeout=5)
                if result.get("status") == "accepted" and result.get("receipt"):
                    path.unlink(missing_ok=True)
            except HTTPError as exc:
                if exc.code in {400, 403, 409, 413, 422}:
                    path.unlink(missing_ok=True)
                break
            except Exception:
                break


if __name__ == "__main__":
    from _env_file import load_env_file

    load_env_file()
    drain()
