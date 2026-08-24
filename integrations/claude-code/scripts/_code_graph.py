#!/usr/bin/env python3
"""Code-graph (enola) pipeline helpers: repo indexing, freshness, recall gating.

Standalone, stdlib-only, so it runs under the system ``python3`` without the
plugin venv (the same constraint ``_remember_http.py`` / ``_recall_http.py``
already work under). Requires a cognee server >= 1.5.3 (the release that
opened ``content_type="code"`` on /api/v1/remember and the ``code`` recall
scope).

Four responsibilities, all shared by the wrapper CLI and the hooks:

1. **Repo indexing** (``do_index_repo``): POST ``content_type="code"`` +
   ``repositories`` to ``/api/v1/remember``. One code graph per repository
   spec (a local path when the server shares this filesystem, or a git URL
   the server clones). The transport contract mirrors ``_remember_http.py``:
   ``{"ok": true, ...}`` on 2xx, an error envelope on HTTP errors (no CLI
   fallback — there is no CLI equivalent for this route), and the
   ``UNREACHABLE`` sentinel only when the server is positively absent.

2. **Freshness** (``reingest_if_changed``): a per-repo state file records the
   git fingerprint at the last successful index. The Stop hook calls this
   with the turn's cwd; when the working tree changed since the last index,
   the repo is re-submitted in the background. The fingerprint is only a
   cheap client-side gate — the server re-hashes every covered file anyway
   (manifest content hash + enola snapshot identity), so a false positive
   costs one skipped submission, never a wasted re-parse. A failed submission
   leaves the fingerprint untouched so the edits stay pending, and an
   escalating time backoff (``retry_backoff_seconds``) keeps an unresolved
   failure from re-submitting once per turn forever.

3. **Recall gating** (``auto_code_lane``): the per-prompt recall hook adds a
   ``code`` scope lane ONLY when (a) the prompt contains an
   identifier-shaped token and (b) the cwd sits inside a repo this plugin
   has indexed. The lane is additive — it never replaces the semantic
   scopes — and a seed the code graph cannot resolve contributes nothing
   server-side, so misfires are cheap by design.

4. **Session-start auto-indexing** (``autoindex_on_session_start``): opening
   an agent in a repo should not require a setup step, so a new repo is
   indexed and an already-indexed one refreshed. Indexing a repo nobody asked
   about is gated (see ``autoindex_mode``); refreshing one that WAS asked for
   is not.

State lives under ``~/.cognee-plugin/<integration>/code-graph/<slug>.json``.
A repo has a state file once it has been indexed — explicitly via the wrapper
or skill, or automatically at session start — and only repos with a state file
are ever refreshed or queried by the recall lane.

**Freshness ceiling.** What a code graph can possibly reflect is set by how the
server reads the repo, and the two cases differ permanently:

* ``spec_kind == "path"`` — the server shares this filesystem and reads the
  working tree in place, so the graph covers uncommitted and untracked changes
  and the per-turn refresh keeps it current.
* ``spec_kind == "url"`` — the server clones the repo and can only ever see
  PUSHED commits. A local edit cannot reach it, so the freshness paths here
  deliberately skip URL-indexed repos rather than re-submitting: a re-pull
  would fetch the same commits and rewrite the same graph. Such a graph
  advances only when the user pushes and the repo is indexed again.

This is normal behavior, not a gap to close client-side — but the two produce
identical-looking results, which is why the skills tell the agent to say which
one it is answering from when the work is in progress.
"""

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

# The one line that differs between the claude-code and codex copies.
_INTEGRATION = "claude-code"

UNREACHABLE = "UNREACHABLE"

_STATE_DIR = Path.home() / ".cognee-plugin" / _INTEGRATION / "code-graph"

_GIT_TIMEOUT_SECONDS = 10.0

# Mirrors cognee's CodeLoader SUPPORTED_CODE_EXTENSIONS (v1.5.3) — the
# extensions the per-file CODE route claims. Used for the --file remember
# path's hinting and the identifier extractor's filename pattern.
CODE_EXTENSIONS = frozenset(
    {
        "c",
        "cc",
        "cpp",
        "cs",
        "cxx",
        "dart",
        "fs",
        "go",
        "h",
        "hcl",
        "hh",
        "hpp",
        "java",
        "js",
        "jsx",
        "kt",
        "kts",
        "php",
        "proto",
        "py",
        "rake",
        "rb",
        "rs",
        "scala",
        "svelte",
        "swift",
        "tf",
        "ts",
        "tsx",
        "vb",
        "vue",
    }
)

# CamelCase words that are common prose, product names, or agent vocabulary —
# not symbols worth seeding a code-graph query with. Misfires are cheap
# (seed-not-found returns an empty lane), so this list stays small and only
# covers the words that would fire on nearly every prompt in this ecosystem.
_CAMEL_STOPLIST = frozenset(
    {
        "Claude",
        "ClaudeCode",
        "Codex",
        "Cognee",
        "GitHub",
        "GitLab",
        "JavaScript",
        "TypeScript",
        "PostgreSQL",
        "MongoDB",
        "OpenAI",
        "MacOS",
        "ReadMe",
        "WiFi",
        "OAuth",
        "TODOs",
    }
)

# Ordered by confidence: backticked wins, then file paths, dotted paths,
# snake_case, CamelCase. Each pattern is deliberately conservative — the gate
# exists to keep the code lane OFF conversational prompts, not to catch every
# possible symbol.
_BACKTICK_RE = re.compile(r"`([^`\n]{2,120})`")
_FILEPATH_RE = re.compile(r"\b[\w./-]{1,120}\.(?:%s)\b" % "|".join(sorted(CODE_EXTENSIONS)))
_DOTTED_RE = re.compile(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\b")
_SNAKE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_CAMEL_RE = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+\b")

_IDENTIFIER_CHARS_RE = re.compile(r"^[\w./:-]+$")


def extract_identifiers(prompt: str, limit: int = 2) -> list:
    """Identifier-shaped tokens from a prompt, best first, at most ``limit``.

    An empty list means the syntactic gate did not fire and the code lane
    should be skipped for this prompt.
    """
    if not prompt:
        return []
    found: list = []
    seen = set()

    def _add(token: str) -> None:
        token = token.strip().strip(".,:;()[]{}")
        if len(token) < 3 or len(token) > 120:
            return
        if token in _CAMEL_STOPLIST:
            return
        key = token.casefold()
        if key in seen:
            return
        seen.add(key)
        found.append(token)

    for match in _BACKTICK_RE.finditer(prompt):
        inner = match.group(1).strip()
        # Backticks quote commands and prose too; only take single
        # identifier-shaped tokens (no spaces, identifier charset).
        if " " not in inner and _IDENTIFIER_CHARS_RE.match(inner):
            _add(inner)
    for pattern in (_FILEPATH_RE, _DOTTED_RE, _SNAKE_RE, _CAMEL_RE):
        for match in pattern.finditer(prompt):
            token = match.group(0)
            if pattern is _DOTTED_RE:
                # Drop prose artifacts like "e.g" / "i.e": require some meat.
                parts = token.split(".")
                if len(token) < 5 or not any(len(p) >= 3 for p in parts):
                    continue
                # Version-ish or domain-ish tokens ("v1.5.3", "example.com")
                # are not symbols; require an underscore-free camel or a
                # known code extension to be safe? Keep it simple: skip pure
                # lowercase two-part tokens that end in a TLD-looking part.
                if len(parts) == 2 and parts[1].lower() in ("com", "org", "net", "io", "ai", "dev"):
                    continue
            _add(token)

    return found[:limit]


def build_code_query(identifier: str, limit: int = 5) -> dict:
    """The auto-recall code_query: a bounded substring fact lookup.

    query_facts (not explore) on purpose: explore needs a resolvable seed and
    errors on ambiguity, while query_facts degrades to an empty page — the
    right failure mode for a lane that must never disturb the prompt path.
    """
    return {"operation": "query_facts", "name": identifier, "limit": limit}


# ---------------------------------------------------------------------------
# Git fingerprint + per-repo state
# ---------------------------------------------------------------------------


def _run_git(args, cwd: str) -> str:
    """Run git and return stdout, or "" on any failure (missing git included)."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            text=True,
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout


def git_repo_root(cwd: str) -> str:
    """The repo root containing ``cwd``, or "" when not inside a git repo."""
    if not cwd or not os.path.isdir(cwd):
        return ""
    out = _run_git(["rev-parse", "--show-toplevel"], cwd).strip()
    return os.path.realpath(out) if out else ""


def git_fingerprint(root: str) -> str:
    """A cheap content fingerprint of the working tree.

    Covers HEAD, the dirty-path set, tracked content changes (``git diff
    HEAD``), and untracked files by (path, size, mtime) — porcelain alone
    would miss a re-edit of an already-dirty file. Returns "" when the root
    is not a usable git repo; callers treat "" as "cannot fingerprint" and
    skip the freshness gate (the server-side hashes still dedupe).
    """
    if not root:
        return ""
    head = _run_git(["rev-parse", "HEAD"], root).strip()
    if not head:
        return ""
    porcelain = _run_git(["status", "--porcelain"], root)
    digest = hashlib.sha256()
    digest.update(head.encode("utf-8"))
    digest.update(porcelain.encode("utf-8"))
    digest.update(_run_git(["diff", "HEAD"], root).encode("utf-8", errors="replace"))
    for line in porcelain.splitlines():
        if not line.startswith("??"):
            continue
        rel = line[3:].strip().strip('"')
        path = os.path.join(root, rel)
        try:
            stat = os.stat(path)
            digest.update(f"{rel}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8"))
        except OSError:
            digest.update(f"{rel}:gone".encode("utf-8"))
    return digest.hexdigest()


def canonical_spec(spec: str) -> str:
    """The stable identity of an indexed repository.

    Local paths resolve through symlinks so the same checkout reached by two
    routes is one repository; remote URLs drop a trailing slash and the ``.git``
    suffix so ``…/repo`` and ``…/repo.git`` do not index twice.
    """
    spec = str(spec).strip()
    if is_remote_repo(spec):
        canonical = spec.rstrip("/")
        if canonical.endswith(".git"):
            canonical = canonical[: -len(".git")]
        return canonical
    return os.path.realpath(spec)


def _readable_tail(canonical: str) -> str:
    tail = os.path.basename(canonical.rstrip("/")) or "repo"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", tail).strip("-.") or "repo"


def _repo_slug(root_or_spec: str) -> str:
    canonical = canonical_spec(root_or_spec)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"{_readable_tail(canonical)}-{digest}"


def default_code_dataset(spec: str) -> str:
    """A stable, readable, collision-free per-repo dataset name.

    Narrow datasets keep the CODE snapshot cache and the delta pre-read small,
    so one dataset per indexed repository is the default. The basename alone
    cannot be that identity: checkouts routinely share one (``~/work/a/service``
    and ``~/work/b/service``), and two repos landing in one dataset share a
    graph database — where cognee's stale-node sweep, scoped by repo name,
    would let each ingestion delete the other's nodes. The path digest makes
    the name unique per checkout while the readable tail keeps it recognizable
    in a dataset listing.
    """
    canonical = canonical_spec(spec)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]
    return f"codebase-{_readable_tail(canonical).lower()}-{digest}"


def is_remote_repo(spec: str) -> bool:
    return isinstance(spec, str) and spec.startswith(("https://", "http://", "git@", "ssh://"))


def _state_path(key: str) -> Path:
    return _STATE_DIR / f"{_repo_slug(key)}.json"


def save_repo_state(state: dict) -> None:
    """Persist one repo's index state. ``state`` must carry ``spec``."""
    key = state.get("repo_root") or state.get("spec") or ""
    if not key:
        return
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    _state_path(key).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def load_repo_states() -> list:
    """All recorded repo states (invalid files are skipped, never raised)."""
    states = []
    try:
        entries = sorted(_STATE_DIR.glob("*.json"))
    except OSError:
        return states
    for path in entries:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(state, dict):
            states.append(state)
    return states


def find_indexed_repo(cwd: str) -> dict:
    """The index state whose repo_root contains ``cwd``, or {}.

    Only locally-indexed repos (spec = path) are matched by cwd; a repo
    indexed by URL has no local root to match. Longest matching root wins so
    nested checkouts resolve to the innermost indexed repo.
    """
    real = os.path.realpath(cwd) if cwd else ""
    if not real:
        return {}
    best: dict = {}
    for state in load_repo_states():
        root = str(state.get("repo_root") or "")
        if not root:
            continue
        if real == root or real.startswith(root.rstrip(os.sep) + os.sep):
            if len(root) > len(str(best.get("repo_root") or "")):
                best = state
    return best


def auto_code_lane(prompt: str, cwd: str) -> dict:
    """The per-prompt recall gate. Returns {} when the lane must not fire.

    Fires only when the prompt carries an identifier-shaped token AND the
    cwd sits inside a repo this plugin indexed — never on conversational
    prompts, never on repos nobody asked to index.
    """
    identifiers = extract_identifiers(prompt)
    if not identifiers:
        return {}
    state = find_indexed_repo(cwd)
    if not state or not state.get("dataset"):
        return {}
    return {
        "dataset": str(state["dataset"]),
        "identifier": identifiers[0],
        "code_query": build_code_query(identifiers[0]),
    }


# ---------------------------------------------------------------------------
# Transport: index / re-ingest via /api/v1/remember (content_type="code")
# ---------------------------------------------------------------------------


def _multipart_body(fields):
    """Multipart encoder over (name, value) pairs — repeated names allowed,
    which List[str] Form fields (repositories) require."""
    boundary = f"----cogneeCodeGraph{uuid.uuid4().hex}"
    chunks = []
    for name, value in fields:
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), boundary


def _error(status, message, *, transient=False):
    """Reachable server, failed/rejected request — the caller must NOT treat
    this as 'server absent' (there is no fallback path for this route)."""
    envelope = {"error": message, "status": status, "authoritative": False}
    if transient:
        envelope["transient"] = True
    return envelope


def do_index_repo(
    service_url,
    api_key,
    repo_spec,
    dataset,
    *,
    index_vectors=False,
    run_in_background=True,
    opener=urllib.request.urlopen,
    timeout=120.0,
):
    """Submit one repository for code-graph indexing.

    Returns {"ok": true, ...} on 2xx, an error envelope on HTTP errors, and
    UNREACHABLE only on a positively absent server. A timeout is NOT
    unreachable: background submits return fast, so a timeout usually means
    a slow synchronous run that may still land — surfaced as a transient
    note, never retried blindly.
    """
    url = service_url.rstrip("/") + "/api/v1/remember"
    body, boundary = _multipart_body(
        [
            ("datasetName", dataset),
            ("content_type", "code"),
            ("repositories", str(repo_spec)),
            ("run_in_background", "true" if run_in_background else "false"),
            ("index_vectors", "true" if index_vectors else "false"),
        ]
    )
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if api_key:
        headers["X-Api-Key"] = api_key

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with opener(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            payload = json.loads(e.read().decode("utf-8") or "{}")
            detail = str(payload.get("detail") or payload.get("error") or "")[:300]
        except Exception:
            pass
        if e.code in (401, 403):
            msg = "unauthorized (HTTP %s) — check COGNEE_API_KEY / credentials" % e.code
        elif e.code == 400 and "content_type" in detail:
            # An older server (< 1.5.3) rejects content_type='code' outright.
            msg = (
                "server rejected content_type='code' (HTTP 400) — repo indexing "
                "requires cognee >= 1.5.3; restart the session to upgrade the "
                "plugin server, or upgrade the remote deployment. Detail: " + detail
            )
        else:
            msg = "server returned HTTP %s for /api/v1/remember" % e.code
            if detail:
                msg += " — " + detail
        sys.stderr.write("[cognee-code-graph] %s\n" % msg)
        return _error(e.code, msg)
    except (TimeoutError, socket.timeout):
        sys.stderr.write(
            "[cognee-code-graph] timed out after %ss waiting for confirmation; the "
            "submission may have landed — not retrying\n" % timeout
        )
        return _error(0, "index submitted; timed out after %ss" % timeout, transient=True)
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), (TimeoutError, socket.timeout)):
            return _error(0, "index submitted; timed out after %ss" % timeout, transient=True)
        sys.stderr.write(
            "[cognee-code-graph] server unreachable at %s: %s\n" % (service_url, str(e)[:160])
        )
        return UNREACHABLE
    except Exception as e:
        sys.stderr.write(
            "[cognee-code-graph] server unreachable at %s: %s\n" % (service_url, str(e)[:160])
        )
        return UNREACHABLE

    # A 2xx the caller cannot parse is NOT reported as success, unlike the
    # fire-and-forget remember path. Callers here treat "ok" as confirmation
    # and advance the repo's stored fingerprint on it — so an unparseable body
    # would mark this turn's edits as indexed, and the next turn would see an
    # unchanged tree and never retry, leaving the graph silently stale. The
    # opposite mistake is cheap: keeping the old fingerprint costs one
    # re-submission, which the server's content hash skips if the write did
    # land.
    try:
        data = json.loads(raw or "{}")
    except (json.JSONDecodeError, ValueError) as e:
        sys.stderr.write(
            "[cognee-code-graph] malformed JSON from /api/v1/remember: %s\n" % str(e)[:160]
        )
        return _error(200, "malformed JSON response from /api/v1/remember")
    if isinstance(data, dict) and data.get("error"):
        msg = str(data.get("error"))[:200]
        sys.stderr.write("[cognee-code-graph] server returned error: %s\n" % msg)
        return _error(200, msg)

    result = {"ok": True, "repository": str(repo_spec)}
    if isinstance(data, dict):
        for field in ("dataset_id", "pipeline_run_id", "status"):
            if data.get(field):
                result[field] = str(data[field])
        if data.get("items"):
            result["items"] = data["items"]
    return result


def poll_code_graph_status(
    service_url,
    api_key,
    dataset_id,
    deadline_seconds,
    *,
    opener=urllib.request.urlopen,
    interval_seconds=3.0,
    request_timeout=10.0,
):
    """Poll /api/v1/datasets/status for the code_graph_pipeline.

    Returns "completed" | "errored" | "timeout" | "unknown" — the plugins'
    cognify poll targets pipeline=cognify_pipeline, which never sees code
    runs, so this variant exists specifically for the code route.
    """
    if not dataset_id:
        return "unknown"
    url = (
        service_url.rstrip("/")
        + "/api/v1/datasets/status?dataset="
        + urllib.parse.quote(str(dataset_id))
        + "&pipeline=code_graph_pipeline"
    )
    headers = {}
    if api_key:
        headers["X-Api-Key"] = api_key
    deadline = time.monotonic() + max(0.0, deadline_seconds)
    while True:
        status = ""
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with opener(req, timeout=request_timeout) as resp:
                raw = resp.read().decode("utf-8")
            parsed = json.loads(raw or "{}")
            if isinstance(parsed, dict) and parsed:
                val = parsed.get(str(dataset_id))
                if val is None and len(parsed) == 1:
                    val = next(iter(parsed.values()))
                if isinstance(val, dict):
                    val = val.get("code_graph_pipeline")
                status = str(val or "").upper()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return "unknown"
        except Exception:
            pass
        if status.endswith("COMPLETED"):
            return "completed"
        if status.endswith("ERRORED"):
            return "errored"
        if time.monotonic() >= deadline:
            return "timeout"
        time.sleep(max(0.1, interval_seconds))


def index_repo(
    service_url,
    api_key,
    spec,
    dataset="",
    *,
    index_vectors=False,
    wait_seconds=0.0,
    opener=urllib.request.urlopen,
):
    """Index one repo AND record its state file (the opt-in the hooks honor).

    Local paths are fingerprinted so the Stop hook can re-ingest on change;
    URL specs record no fingerprint (server-side clones only see pushes).

    ``repo_root`` is the indexed path itself, not its enclosing git root: the
    indexed unit is exactly what the caller named, so cwd matching stays
    precise (indexing one subdirectory must not claim the whole repository)
    and two subdirectories of one repository cannot record the same root and
    then match a cwd in arbitrary order.
    """
    spec = canonical_spec(spec)
    remote = is_remote_repo(spec)
    repo_root = "" if remote else spec
    dataset = dataset or default_code_dataset(spec)

    result = do_index_repo(
        service_url,
        api_key,
        spec,
        dataset,
        index_vectors=index_vectors,
        opener=opener,
    )
    if result == UNREACHABLE or (isinstance(result, dict) and result.get("error")):
        return result

    if wait_seconds > 0 and isinstance(result, dict) and result.get("dataset_id"):
        outcome = poll_code_graph_status(
            service_url, api_key, result["dataset_id"], wait_seconds, opener=opener
        )
        result["wait_outcome"] = outcome
        result["queryable"] = outcome == "completed"

    state = {
        "spec": spec,
        "spec_kind": "url" if remote else "path",
        "repo_root": repo_root,
        "dataset": dataset,
        "index_vectors": bool(index_vectors),
        "fingerprint": git_fingerprint(repo_root) if repo_root else "",
        "last_index_at": time.time(),
        "last_status": str(result.get("status") or "submitted") if isinstance(result, dict) else "",
    }
    save_repo_state(state)
    result["dataset"] = dataset
    return result


# Escalating retry backoff for a repo whose re-index keeps failing.
# A failed submission deliberately leaves the fingerprint alone so the edits
# stay pending — but the tree then differs from the stored fingerprint on EVERY
# later turn, conversation-only turns included, so an unresolved failure (a bad
# API key, a server too old for content_type='code') would otherwise re-submit
# once per turn forever and append a line to a hook log that never rotates.
#
# Time, not error class, is the gate. The failures worth calling "permanent"
# here — 401, 403, a 400 from an old server — are all fixable by the user
# mid-session, and fixing them is exactly when a prompt retry should catch the
# graph up. Suppressing retries by error class (or, worse, advancing the
# fingerprint to stop them) would trade a noisy failure for a silently stale
# graph. The cap bounds recovery lag instead.
_RETRY_BACKOFF_BASE_SECONDS = 30.0
_RETRY_BACKOFF_MAX_SECONDS = 900.0


def retry_backoff_seconds(error_count: int) -> float:
    """Seconds to wait after ``error_count`` consecutive failures."""
    if error_count <= 0:
        return 0.0
    return min(
        _RETRY_BACKOFF_BASE_SECONDS * (2 ** (error_count - 1)),
        _RETRY_BACKOFF_MAX_SECONDS,
    )


def _retry_suppressed(state: dict, now: float) -> bool:
    """Whether a failing repo is still inside its backoff window."""
    try:
        error_count = int(state.get("error_count") or 0)
        last_error_at = float(state.get("last_error_at") or 0.0)
    except (TypeError, ValueError):
        return False
    if error_count <= 0 or last_error_at <= 0:
        return False
    # A clock that moved backwards (system time change) must not park a repo
    # in backoff indefinitely — treat it as due.
    if now < last_error_at:
        return False
    return (now - last_error_at) < retry_backoff_seconds(error_count)


def _record_retry_failure(state: dict, error: str, now: float) -> None:
    """Advance the backoff counters. Never touches ``fingerprint``."""
    try:
        state["error_count"] = int(state.get("error_count") or 0) + 1
    except (TypeError, ValueError):
        state["error_count"] = 1
    state["last_error_at"] = now
    state["last_error"] = str(error)[:200]
    save_repo_state(state)


def _clear_retry_failure(state: dict) -> None:
    """Forget a past failure once an index succeeds."""
    for field in ("error_count", "last_error_at", "last_error"):
        state.pop(field, None)


def reingest_if_changed(cwd, service_url, api_key, *, opener=urllib.request.urlopen) -> dict:
    """Stop-hook freshness pass: re-submit the cwd's indexed repo if it changed.

    Returns a small outcome dict for logging; {} when there is nothing to do
    (no indexed repo for this cwd, URL-indexed repo, or unchanged tree).
    Never raises — this runs on the (async) Stop hook.

    URL-indexed repos are skipped by design, not by omission: the server built
    that graph from its own clone, which only ever holds pushed commits, so
    re-submitting after a local edit would re-pull the same commits and produce
    the same graph. Those graphs advance when the user pushes and re-indexes.
    """
    try:
        state = find_indexed_repo(cwd)
        if not state or state.get("spec_kind") != "path":
            return {}
        root = str(state.get("repo_root") or "")
        fingerprint = git_fingerprint(root)
        if not fingerprint:
            return {}
        if fingerprint == str(state.get("fingerprint") or ""):
            return {"changed": False, "repo_root": root}

        # Still inside the backoff window after earlier failures: make no
        # request and return the "nothing to do" shape, which the Stop hook
        # does not log. The pending edits stay pending (the fingerprint is
        # untouched) and the next attempt happens when the window expires.
        now = time.time()
        if _retry_suppressed(state, now):
            return {}

        result = do_index_repo(
            service_url,
            api_key,
            state.get("spec") or root,
            str(state.get("dataset") or default_code_dataset(root)),
            index_vectors=bool(state.get("index_vectors")),
            opener=opener,
        )
        outcome = {"changed": True, "repo_root": root, "dataset": state.get("dataset")}
        if isinstance(result, dict) and result.get("ok"):
            state["fingerprint"] = fingerprint
            state["last_index_at"] = now
            state["last_status"] = str(result.get("status") or "submitted")
            _clear_retry_failure(state)
            save_repo_state(state)
            outcome["submitted"] = True
            if result.get("pipeline_run_id"):
                outcome["pipeline_run_id"] = result["pipeline_run_id"]
        else:
            # Keep the OLD fingerprint on failure so the edits stay pending and
            # a later turn retries; only the backoff counters advance.
            outcome["submitted"] = False
            outcome["error"] = (
                "unreachable"
                if result == UNREACHABLE
                else str(result.get("error") if isinstance(result, dict) else result)[:200]
            )
            _record_retry_failure(state, outcome["error"], now)
            outcome["retry_in"] = round(retry_backoff_seconds(state["error_count"]))
        return outcome
    except Exception as exc:  # never break the Stop hook
        return {"changed": None, "error": str(exc)[:200]}


# ---------------------------------------------------------------------------
# Session-start auto-indexing
# ---------------------------------------------------------------------------

# A repo bigger than this many source files is not auto-indexed: the enola pass
# is CPU-bound over the whole tree, and silently spending that on a monorepo
# nobody asked to index is worse than doing nothing. Explicit indexing (the
# wrapper / skill) has no cap — an explicit request IS the consent.
_AUTOINDEX_MAX_FILES = 3000


def autoindex_mode() -> str:
    """How session start treats an unindexed repo: "auto" | "off" | "always".

    - ``auto`` (default): index a new repo only when the server is LOCAL, where
      the code never leaves this machine — the server reads the working tree in
      place. A cloud server is skipped: shipping a private checkout to a hosted
      tenant is not something a plugin should decide on the user's behalf (and
      the server rejects local paths anyway, so it could only ever work by
      pushing).
    - ``always``: also auto-index against a cloud/remote server.
    - ``off``: never auto-index; only refresh repos already indexed explicitly.

    Refreshing an ALREADY-indexed repo is not governed by this setting — that
    repo's index command was the consent, and a stale graph is the failure mode
    the freshness loop exists to prevent.
    """
    value = str(os.environ.get("COGNEE_CODE_AUTOINDEX", "") or "").strip().lower()
    if value in ("off", "0", "false", "no"):
        return "off"
    if value in ("always", "1", "true", "yes", "on"):
        return "always"
    return "auto"


def _source_file_count(root: str, cap: int) -> int:
    """Count code files under ``root``, stopping once ``cap`` is exceeded.

    Bounded on purpose: this runs at session start, so it must cost the same
    on a monorepo as on a small service.
    """
    skip = {".git", "node_modules", ".venv", "venv", "dist", "build", "target", "__pycache__"}
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        for name in filenames:
            if name.rsplit(".", 1)[-1].lower() in CODE_EXTENSIONS:
                seen += 1
                if seen > cap:
                    return seen
    return seen


def autoindex_on_session_start(
    cwd,
    service_url,
    api_key,
    *,
    is_local_server=True,
    opener=urllib.request.urlopen,
) -> dict:
    """Index (or refresh) the repo a session opened in. Never raises.

    Runs from the detached session-start worker, so it never blocks the first
    prompt. Returns a small outcome dict for logging; ``{}`` when there is
    nothing to do.

    Two distinct cases, deliberately governed by different rules:
      * the repo is ALREADY indexed -> refresh it if the tree moved since the
        last index (covers edits made while the agent was away);
      * the repo is NEW -> index it only when ``autoindex_mode()`` allows,
        which by default means a local server only.
    """
    try:
        root = git_repo_root(cwd)
        if not root:
            return {}

        indexed = find_indexed_repo(root)
        if indexed:
            # A session start is the gesture a user makes after fixing what
            # broke indexing (a corrected API key, a restarted/upgraded
            # server), so it always gets one attempt rather than waiting out a
            # backoff window earned by the previous session. Bounded by
            # launches, not turns — which is what the per-turn backoff guards.
            if indexed.get("error_count"):
                _clear_retry_failure(indexed)
                save_repo_state(indexed)
            outcome = reingest_if_changed(root, service_url, api_key, opener=opener)
            if outcome:
                outcome["reason"] = "refresh"
            return outcome

        mode = autoindex_mode()
        if mode == "off":
            return {"skipped": "autoindex_off", "repo_root": root}
        if mode == "auto" and not is_local_server:
            # Remote server: indexing would mean shipping this checkout to a
            # hosted tenant. Explicit opt-in only (COGNEE_CODE_AUTOINDEX=always
            # or the index command).
            return {"skipped": "remote_server", "repo_root": root}

        count = _source_file_count(root, _AUTOINDEX_MAX_FILES)
        if count == 0:
            return {"skipped": "no_code_files", "repo_root": root}
        if count > _AUTOINDEX_MAX_FILES:
            return {
                "skipped": "too_large",
                "repo_root": root,
                "source_files": f">{_AUTOINDEX_MAX_FILES}",
            }

        result = index_repo(service_url, api_key, root, opener=opener)
        if result == UNREACHABLE or (isinstance(result, dict) and result.get("error")):
            return {
                "indexed": False,
                "repo_root": root,
                "error": "unreachable" if result == UNREACHABLE else str(result.get("error"))[:200],
            }
        return {
            "indexed": True,
            "reason": "first_seen",
            "repo_root": root,
            "dataset": result.get("dataset", ""),
            "source_files": count,
        }
    except Exception as exc:  # never break session start
        return {"error": str(exc)[:200]}


def main(argv):
    """CLI: ``index <repo-path-or-url> [dataset]`` or ``dataset <path>``.

    argv mirrors the sibling scripts: service_url, api_key, command, spec,
    dataset, index_vectors, wait_seconds. The ``dataset`` command prints the
    code dataset covering a path (empty line when that path is not indexed) —
    dataset names carry a path digest, so callers resolve the name from the
    checkout instead of reconstructing it.
    """
    a = list(argv) + [""] * 7
    service_url, api_key, command, spec = a[0], a[1], a[2], a[3]
    dataset = a[4]
    index_vectors = str(a[5]).strip().lower() in ("1", "true", "yes", "on")
    try:
        wait_seconds = float(a[6]) if str(a[6]).strip() else 0.0
    except ValueError:
        wait_seconds = 0.0

    if command == "dataset":
        state = find_indexed_repo(spec or os.getcwd())
        print(str(state.get("dataset") or ""))
        return

    if command != "index" or not spec:
        print(
            json.dumps(
                {
                    "error": "usage: _code_graph.py <service_url> <api_key> "
                    "index <repo-path-or-url> [dataset] [index_vectors] "
                    "[wait_seconds] | dataset [path]",
                    "status": 2,
                }
            )
        )
        return
    result = index_repo(
        service_url,
        api_key,
        spec,
        dataset,
        index_vectors=index_vectors,
        wait_seconds=wait_seconds,
    )
    print(UNREACHABLE if result == UNREACHABLE else json.dumps(result))


if __name__ == "__main__":
    main(sys.argv[1:])
