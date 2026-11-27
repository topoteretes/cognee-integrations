#!/usr/bin/env python3
"""Code-graph (enola) pipeline helpers: repo indexing, freshness, recall gating.

Standalone, stdlib-only, so it runs under the system ``python3`` without the
plugin venv (the same constraint ``_remember_http.py`` / ``_recall_http.py``
already work under). Requires a cognee server >= 1.5.4. 1.5.3 opened
``content_type="code"`` on /api/v1/remember and the ``code`` recall scope;
1.5.4 renamed the repo-spec form field from ``repositories`` to ``raw_data``,
and this module sends the new name only.

Four responsibilities, all shared by the wrapper CLI and the hooks:

1. **Repo indexing** (``do_index_repo``): POST ``content_type="code"`` +
   ``raw_data`` to ``/api/v1/remember``. One code graph per repository
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

4. **Session-start indexing** (``index_offer`` / ``autoindex_on_session_start``):
   a repo that was indexed is refreshed when it moved. A NEW repo is the user's
   call: ``index_offer`` describes it so SessionStart can have the agent ask,
   and the index runs only on a yes (see ``autoindex_mode``). A "never" is
   recorded per repo and per kind of server, and never asked again.

State lives under ``~/.cognee-plugin/<integration>/code-graph/``, one file per
indexed repo and server (``<slug>@<server digest>.json``). A repo has a state
file once it has been indexed, and only repos with a state file are ever
refreshed or queried by the recall lane. A recorded "never" lives in
``code-graph/declined/<slug>.json``.

**One graph per server.** A graph lives on the server that built it, so each
state records that server (``server``, see ``server_identity``), and only a
graph built on the server in use counts: it alone is refreshed, recalled, or
taken as an answer to the session-start question. A checkout indexed on one
Cognee tenant is unindexed on another, so it is asked about there, and nothing
is ever submitted to a server the user did not say yes to. A state written
before states recorded their server matches any server of its kind, and is
stamped with the server at its next successful refresh.

**Local and remote servers.** A local server reads the checkout in place, so
the spec it is sent is the path. A remote server (Cognee Cloud, any non-loopback
host) cannot read this disk: for a checkout it is sent the repository's git
remote as an ``https`` URL and clones it itself (``remote_clone_url``). The
state then carries both the URL (``spec``) and the checkout (``repo_root``), so
the cwd still resolves to the graph. Nothing but that URL leaves the machine:
no file content, and never a credential from the local git config.

**Freshness ceiling.** What a code graph can possibly reflect is set by how the
server reads the repo, and the two cases differ permanently:

* ``spec_kind == "path"`` — the server shares this filesystem and reads the
  working tree in place, so the graph covers uncommitted and untracked changes
  and the per-turn refresh keeps it current.
* ``spec_kind == "url"`` — the server clones the repo and can only ever see
  PUSHED commits on the default branch. A local edit cannot reach it, so a
  working-tree change never re-submits one: a re-pull would fetch the same
  commits and rewrite the same graph. When the URL was indexed from a checkout
  (``repo_root`` set), the freshness gate is the pushed commit instead
  (``pushed_fingerprint``), so the graph advances once a push lands. A bare
  URL, with no checkout to watch, advances only when it is indexed again.

This is normal behavior, not a gap to close client-side — but the two produce
identical-looking results, which is why the skills tell the agent to say which
one it is answering from when the work is in progress.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

# Set per host copy. The Codex, Cursor and Antigravity copies also still index a
# new repository unasked and send a remote server the local path (the ask-first
# and clone-by-remote behaviour below is this copy's so far).
_INTEGRATION = "claude-code"

UNREACHABLE = "UNREACHABLE"

_STATE_DIR = Path.home() / ".cognee-plugin" / _INTEGRATION / "code-graph"

_GIT_TIMEOUT_SECONDS = 10.0

# Mirrors cognee's CodeLoader SUPPORTED_CODE_EXTENSIONS (v1.5.3) — the
# extensions the per-file CODE route claims. Used by the identifier
# extractor's filename pattern and the auto-index source-file count.
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


def _run_git(args, cwd: str, timeout: float = _GIT_TIMEOUT_SECONDS) -> str:
    """Run git and return stdout, or "" on any failure (missing git included)."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
            # git writes paths as UTF-8 (Git for Windows too); the console code
            # page would turn a non-ASCII checkout path into one that does not exist.
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout


def _git_output_or_none(args, cwd: str, timeout: float):
    """Like ``_run_git``, but None when git did not answer within ``timeout``."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
            # git writes paths as UTF-8 (Git for Windows too); the console code
            # page would turn a non-ASCII checkout path into one that does not exist.
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return None
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _bounded_timeout(timeout: float, deadline=None) -> float:
    """``timeout``, cut to what is left before ``deadline`` (a ``time.monotonic()`` value).

    Raises ``TimeoutError`` once the deadline has passed, so a caller with a
    budget for several git calls stops instead of starting another.
    """
    if deadline is None:
        return timeout
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("git did not answer in time")
    return min(timeout, left)


def _git_in_time(args, cwd: str, timeout: float, deadline=None) -> str:
    """``_run_git``, or with ``deadline`` a call that must answer before it.

    Without a deadline a git that does not answer reads as no output, as
    everywhere else in this module. With one, it raises ``TimeoutError``: a
    budget spent inside a call must not pass for "not a repository" or "no
    remote", which would quietly drop the session-start question.
    """
    if deadline is None:
        return _run_git(args, cwd, timeout=timeout)
    out = _git_output_or_none(args, cwd, _bounded_timeout(timeout, deadline))
    if out is None:
        raise TimeoutError("git did not answer in time")
    return out


def git_repo_root(cwd: str, timeout: float = _GIT_TIMEOUT_SECONDS, deadline=None) -> str:
    """The repo root containing ``cwd``, or "" when not inside a git repo."""
    if not cwd or not os.path.isdir(cwd):
        return ""
    out = _git_in_time(["rev-parse", "--show-toplevel"], cwd, timeout, deadline).strip()
    return os.path.realpath(out) if out else ""


# enola writes its snapshot INTO the indexed repository (<repo>/.enola/) and
# rotates files there on every run. Left visible to the fingerprint, the
# indexer's own output would change the fingerprint after every index, so the
# next turn would see a "changed" tree and re-index — a full enola parse per
# turn, forever, for every locally indexed repo (verified: two enola runs with
# no source edit produce two different fingerprints). Excluded at the git level
# so neither the dirty-path set nor the untracked stats can see it.
_ENOLA_SNAPSHOT_DIR = ".enola"
_FINGERPRINT_PATHSPEC = ["--", ".", f":(exclude){_ENOLA_SNAPSHOT_DIR}"]


def git_fingerprint(root: str) -> str:
    """A cheap content fingerprint of the working tree.

    Covers HEAD, the dirty-path set, tracked content changes (``git diff
    HEAD``), and untracked files by (path, size, mtime) — porcelain alone
    would miss a re-edit of an already-dirty file. The enola snapshot dir the
    indexer itself writes is excluded (see ``_FINGERPRINT_PATHSPEC``). Returns
    "" when the root is not a usable git repo; callers treat "" as "cannot
    fingerprint" and skip the freshness gate (the server-side hashes still
    dedupe).
    """
    if not root:
        return ""
    head = _run_git(["rev-parse", "HEAD"], root).strip()
    if not head:
        return ""
    porcelain = _run_git(["status", "--porcelain", *_FINGERPRINT_PATHSPEC], root)
    digest = hashlib.sha256()
    digest.update(head.encode("utf-8"))
    digest.update(porcelain.encode("utf-8"))
    digest.update(
        _run_git(["diff", "HEAD", *_FINGERPRINT_PATHSPEC], root).encode("utf-8", errors="replace")
    )
    for line in porcelain.splitlines():
        if not line.startswith("??"):
            continue
        rel = line[3:].strip().strip('"')
        # Belt and braces: an older git that ignores the exclude pathspec must
        # still never let the snapshot dir drive the fingerprint.
        if rel.rstrip("/") == _ENOLA_SNAPSHOT_DIR or rel.startswith(_ENOLA_SNAPSHOT_DIR + "/"):
            continue
        path = os.path.join(root, rel)
        try:
            stat = os.stat(path)
            digest.update(f"{rel}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8"))
        except OSError:
            digest.update(f"{rel}:gone".encode("utf-8"))
    return digest.hexdigest()


def server_shares_files() -> bool:
    """``COGNEE_CODE_SERVER_SHARES_FILES``: the server reads this disk from elsewhere.

    A loopback URL is the only thing that says so by itself. A server reached
    by another name (a container's service name, a LAN host) that mounts the
    same checkout at the same path has to be declared; it is then treated as a
    local one everywhere here, and sent the path instead of a clone URL.
    """
    value = str(os.environ.get("COGNEE_CODE_SERVER_SHARES_FILES", "") or "").strip().lower()
    return value in ("1", "true", "yes", "on")


def _reads_disk(is_local_server):
    """``is_local_server`` as the functions below mean it: the server reads this disk."""
    return True if server_shares_files() else is_local_server


def service_is_local(service_url: str) -> bool:
    """Whether the server at ``service_url`` reads the checkout in place.

    The loopback hosts are the ones ``_plugin_common.service_url_is_local``
    lists; this module imports nothing of the plugin's, so the list is repeated
    and a test holds the two together. They differ on an empty URL on purpose:
    the hooks default it to the local server, while a command given no server
    has none to read a path.
    """
    if server_shares_files():
        return True
    host = (urllib.parse.urlparse(str(service_url or "")).hostname or "").lower()
    return host in _LOOPBACK_HOSTS


_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0")
_DEFAULT_PORTS = {"http": 80, "https": 443}


def server_identity(service_url: str) -> str:
    """The server ``service_url`` names, as one string two URLs can be compared by.

    Scheme and host are lowercased, a default port and a trailing slash are
    dropped, and the loopback names ``service_is_local`` accepts count as one
    host, ``localhost``: ``http://127.0.0.1:8011/`` and ``http://localhost:8011``
    are the same server. The port and any path stay, since a second local
    server or a deployment under a path prefix is another server. "" when the
    URL names no host.
    """
    raw = str(service_url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    parts = urllib.parse.urlsplit(raw)
    scheme = (parts.scheme or "http").lower()
    host = (parts.hostname or "").lower()
    if not host:
        return ""
    if host in _LOOPBACK_HOSTS:
        host = "localhost"
    elif ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = host if port in (None, _DEFAULT_PORTS.get(scheme)) else f"{host}:{port}"
    return f"{scheme}://{netloc}{parts.path.rstrip('/')}"


_SCP_REMOTE_RE = re.compile(r"^(?:[^@/\s]+@)?([^:/\s]+):(?!/)(.+)$")
# The clone URL is shown to the agent inside the session-start question, and the
# remote it comes from is whatever the checkout's git config says. Only the
# characters a repository URL needs get through, so a crafted remote cannot
# carry a sentence into that instruction.
_CLONE_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+(?::\d+)?$")
_CLONE_PATH_RE = re.compile(r"^[A-Za-z0-9._~%/-]+$")


def _https_clone_url(remote: str) -> str:
    """A git remote as the ``https`` URL a server with no keys of ours can clone.

    ``git@host:org/repo.git`` and ``ssh://git@host/org/repo.git`` become
    ``https://host/org/repo``: the server holds none of this machine's SSH
    keys, so the SSH form could never work there. Userinfo is dropped from an
    http(s) remote, since a token saved in the local git config must not be
    sent anywhere. Remotes a server cannot reach at all (a path, ``file://``,
    ``git://``) give "", and so does one whose host or path holds anything but
    URL characters.
    """
    remote = str(remote or "").strip()
    if not remote:
        return ""
    if "://" in remote:
        parts = urllib.parse.urlsplit(remote)
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https", "ssh", "git+ssh") or not parts.hostname:
            return ""
        host = parts.hostname
        # An SSH port says nothing about where the host serves https.
        if scheme in ("http", "https") and parts.port:
            host = f"{host}:{parts.port}"
        path = parts.path.strip("/")
        scheme = scheme if scheme in ("http", "https") else "https"
    else:
        match = _SCP_REMOTE_RE.match(remote)
        if not match:
            return ""
        scheme, host, path = "https", match.group(1), match.group(2).strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    if not path or "." not in host:
        return ""
    if not _CLONE_HOST_RE.match(host) or not _CLONE_PATH_RE.match(path):
        return ""
    return f"{scheme}://{host}/{path}"


def _remote_name(root: str, timeout: float = _GIT_TIMEOUT_SECONDS, deadline=None) -> str:
    """``origin``, or the only remote when there is exactly one; else ""."""
    listed = _git_in_time(["remote"], root, timeout, deadline)
    remotes = [line.strip() for line in listed.splitlines() if line.strip()]
    if "origin" in remotes:
        return "origin"
    return remotes[0] if len(remotes) == 1 else ""


def remote_clone_url(root: str, timeout: float = _GIT_TIMEOUT_SECONDS, deadline=None) -> str:
    """The URL a remote server would clone for the checkout at ``root``, or ""."""
    if not root:
        return ""
    name = _remote_name(root, timeout, deadline)
    if not name:
        return ""
    return _https_clone_url(_git_in_time(["remote", "get-url", name], root, timeout, deadline))


def default_branch_ref(root: str) -> str:
    """The remote-tracking ref of the branch a clone of this checkout's remote takes.

    A clone takes the remote's default branch. ``refs/remotes/<remote>/HEAD``
    names it once a full ``git clone`` or ``git remote set-head`` has written
    it. A checkout that got its remote another way (``git init``, ``git remote
    add`` and a push) has no such ref, and the branch is told from the others:
    the remote's only branch, else ``main``, else ``master``. The only branch
    counts just when the checkout fetches every branch of the remote: a
    single-branch clone (``--single-branch`` or ``--depth`` with ``-b``) holds
    the branch it was cloned from, whatever the default is. Otherwise it is ""
    rather than a guess, until the default branch is fetched and recorded
    (``git remote set-head <remote> --auto``).
    """
    name = _remote_name(root) if root else ""
    if not name:
        return ""
    head = f"refs/remotes/{name}/HEAD"
    if _run_git(["rev-parse", "--verify", "--quiet", head], root).strip():
        return head
    listed = _run_git(["for-each-ref", "--format=%(refname)", f"refs/remotes/{name}/"], root)
    branches = [ref for ref in (line.strip() for line in listed.splitlines()) if ref]
    branches = [ref for ref in branches if ref != head]
    if len(branches) == 1 and _fetches_every_branch(root, name):
        return branches[0]
    for branch in ("main", "master"):
        if f"refs/remotes/{name}/{branch}" in branches:
            return f"refs/remotes/{name}/{branch}"
    return ""


def _fetches_every_branch(root: str, name: str) -> bool:
    """Whether the remote's fetch refspecs take all of its branches (``refs/heads/*``)."""
    refspecs = _run_git(["config", "--get-all", f"remote.{name}.fetch"], root)
    return any(
        line.strip().lstrip("+").split(":", 1)[0] == "refs/heads/*"
        for line in refspecs.splitlines()
    )


def pushed_fingerprint(root: str) -> str:
    """The commit a server-side clone of this checkout's remote would hold.

    The tip of ``default_branch_ref`` as the last fetch or push left it, read
    locally with no network call: a hook must not hit the remote with the
    user's credentials. "" when the remote's default branch is unknown: the
    cloned graph is then not refreshed, and ``reingest_if_changed`` says why.
    """
    ref = default_branch_ref(root)
    if not ref:
        return ""
    return _run_git(["rev-parse", "--verify", "--quiet", ref], root).strip()


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
    # Dots are fine in the repo slug but cognee rejects them in a dataset name
    # (check_dataset_name), so a repo like ``foo.js`` could never be indexed.
    tail = _readable_tail(canonical).lower().replace(".", "-")
    return f"codebase-{tail}-{digest}"


def is_remote_repo(spec: str) -> bool:
    return isinstance(spec, str) and spec.startswith(("https://", "http://", "git@", "ssh://"))


def _state_path(key: str, server: str = "", spec_kind: str = "") -> Path:
    """The state file of one indexed repo on one server, for one way of reading it.

    ``<slug>@<server digest>.json`` for a graph read from the path,
    ``….remote.json`` for one cloned from the remote: a server can hold both,
    one after the other, when ``COGNEE_CODE_SERVER_SHARES_FILES`` changes.
    ``<slug>.json`` when ``server`` is empty: the name every state had before
    states recorded their server, still read and still written for such a state.
    """
    if not server:
        return _STATE_DIR / f"{_repo_slug(key)}.json"
    digest = hashlib.sha256(server.encode("utf-8")).hexdigest()[:12]
    suffix = ".remote.json" if spec_kind == "url" else ".json"
    return _STATE_DIR / f"{_repo_slug(key)}@{digest}{suffix}"


def save_repo_state(state: dict) -> None:
    """Persist one repo's index state. ``state`` must carry ``spec``.

    One file per repo and server: a checkout indexed on two servers (a local
    one and Cognee Cloud, or two tenants) is two graphs in two places, and
    indexing on one must not erase what is known of the other. A state that
    names its server replaces the server-less file of the same repo and kind,
    which it supersedes.
    """
    key = state.get("repo_root") or state.get("spec") or ""
    if not key:
        return
    server = str(state.get("server") or "")
    spec_kind = str(state.get("spec_kind") or "")
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    _state_path(key, server, spec_kind).write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8"
    )
    if server:
        _drop_superseded_states(key, spec_kind, server)


def _drop_superseded_states(key: str, spec_kind: str, server: str) -> None:
    """Remove the states under an older name that the bound one replaces.

    ``<slug>.json`` is what released versions write; ``<slug>.remote.json`` is
    what development builds of server binding wrote for a cloned checkout. One
    of the same kind either names no server, or names this one: a released
    version still running elsewhere loads a bound state and saves it back
    under its own name, and that copy must not outlive the state it copies.
    """
    slug = _repo_slug(key)
    for legacy in (_STATE_DIR / f"{slug}.json", _STATE_DIR / f"{slug}.remote.json"):
        try:
            old = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (
            isinstance(old, dict)
            and old.get("spec_kind") == spec_kind
            and str(old.get("server") or "") in ("", server)
        ):
            try:
                legacy.unlink()
            except OSError:
                pass


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


def find_indexed_repo(cwd: str, service_url=None, is_local_server=None) -> dict:
    """The index state whose repo_root contains ``cwd``, or {}.

    Matched by the checkout a state was indexed from: a path given to a local
    server, or the checkout whose remote a remote server cloned. A bare URL has
    no checkout and matches no cwd. Longest matching root wins so nested
    checkouts resolve to the innermost indexed repo.

    ``service_url`` is the server in use now. When given, only a graph built on
    that server counts (``state_serves``): any other sits in a dataset this
    server does not have. ``is_local_server`` overrides what the URL says about
    the kind of server, which only states that predate ``server`` are matched by.
    """
    real = os.path.realpath(cwd) if cwd else ""
    if not real:
        return {}
    best: dict = {}
    for state in load_repo_states():
        root = str(state.get("repo_root") or "")
        if not root:
            continue
        if service_url is not None and not state_serves(state, service_url, is_local_server):
            continue
        if real == root or real.startswith(root.rstrip(os.sep) + os.sep):
            if not best or _state_rank(state) > _state_rank(best):
                best = state
    return best


def _state_rank(state: dict) -> tuple:
    """How one matching state beats another: the innermost checkout first; then a
    state that names its server over one that predates that; then the latest
    index, so a copy a released version saved under its own name cannot shadow
    the state it was copied from."""
    try:
        indexed_at = float(state.get("last_index_at") or 0.0)
    except (TypeError, ValueError):
        indexed_at = 0.0
    return (len(str(state.get("repo_root") or "")), bool(state.get("server")), indexed_at)


def state_serves(state: dict, service_url: str, is_local_server=None) -> bool:
    """Whether ``state`` is the graph the server at ``service_url`` reads this repo by.

    A graph lives on the server that built it: a checkout indexed on one Cognee
    tenant is still unindexed on another, and one indexed locally is unindexed
    on Cognee Cloud. It must also be read the way that server reads the repo
    now (a path for a server that reads this disk, a clone for one that does
    not): after ``COGNEE_CODE_SERVER_SHARES_FILES`` changes, the other kind of
    graph is not refreshed, and the repo is asked about again.

    A state written before states recorded their server matches any server of
    its kind, as it always did. Except a cloned checkout with no server: no
    release wrote one (only development builds of this binding did), and which
    tenant holds that graph is unknown, so it answers for none.
    ``is_local_server`` overrides what the URL says about the kind.
    """
    if is_local_server is None:
        is_local_server = service_is_local(service_url)
    kind_matches = (state.get("spec_kind") == "path") == bool(_reads_disk(is_local_server))
    recorded = str(state.get("server") or "")
    if recorded:
        return kind_matches and recorded == server_identity(service_url)
    if state.get("spec_kind") != "path" and state.get("repo_root"):
        return False
    return kind_matches


# ---------------------------------------------------------------------------
# Declined repos: a "never" answered to the session-start question
# ---------------------------------------------------------------------------

_DECLINED_DIR = _STATE_DIR / "declined"


def _server_kind(is_local_server: bool) -> str:
    return "local" if is_local_server else "remote"


def _declined_path(root: str) -> Path:
    return _DECLINED_DIR / f"{_repo_slug(root)}.json"


def _read_declined(root: str) -> dict:
    try:
        record = json.loads(_declined_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def is_declined(root: str, is_local_server: bool) -> bool:
    """Whether the user said never to index ``root`` on this kind of server.

    Kept per kind of server: declining to send a repository to a hosted tenant
    says nothing about a local server, where the code stays on the machine.
    """
    declined = _read_declined(root).get("declined")
    return isinstance(declined, dict) and bool(declined.get(_server_kind(is_local_server)))


def record_declined(root: str, is_local_server: bool) -> None:
    record = _read_declined(root)
    declined = record.get("declined") if isinstance(record.get("declined"), dict) else {}
    declined[_server_kind(is_local_server)] = time.time()
    _DECLINED_DIR.mkdir(parents=True, exist_ok=True)
    _declined_path(root).write_text(
        json.dumps({"repo_root": canonical_spec(root), "declined": declined}), encoding="utf-8"
    )


def clear_declined(root: str, is_local_server: bool) -> None:
    """Forget a "never": indexing the repo explicitly is a later yes."""
    record = _read_declined(root)
    declined = record.get("declined") if isinstance(record.get("declined"), dict) else {}
    if not declined.pop(_server_kind(is_local_server), None):
        return
    try:
        if declined:
            _declined_path(root).write_text(
                json.dumps({"repo_root": canonical_spec(root), "declined": declined}),
                encoding="utf-8",
            )
        else:
            _declined_path(root).unlink()
    except OSError:
        pass


def auto_code_lane(prompt: str, cwd: str, service_url=None, is_local_server=None) -> dict:
    """The per-prompt recall gate. Returns {} when the lane must not fire.

    Fires only when the prompt carries an identifier-shaped token AND the
    cwd sits inside a repo this plugin indexed — never on conversational
    prompts, never on repos nobody asked to index. With ``service_url`` given,
    the repo must be indexed on that server, the one the lane queries.
    """
    identifiers = extract_identifiers(prompt)
    if not identifiers:
        return {}
    state = find_indexed_repo(cwd, service_url, is_local_server)
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
    which List[str] Form fields (raw_data) require."""
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
            # cognee >= 1.5.4 reads the repo specs from raw_data; 1.5.3 called
            # the same field repositories. Sending the old name to a 1.5.4
            # server drops the spec silently (unknown Form parts are ignored),
            # which is what issue #420 reported.
            ("raw_data", str(repo_spec)),
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
        elif e.code == 400 and "unsupported content_type" in detail.lower():
            # An older server rejects the content_type value outright, and says
            # so in those words. Matching on a bare "content_type" substring
            # would also swallow a current server's code-branch 400s (which all
            # name the field), reporting a contract or argument error as "your
            # server is too old" — the misdirection issue #420 reported. Those
            # fall through to the generic branch, which quotes the server.
            msg = (
                "server rejected content_type='code' (HTTP 400) — repo indexing "
                "requires cognee >= 1.5.4; restart the session to upgrade the "
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
    is_local_server=None,
):
    """Index one repo AND record its state file (the opt-in the hooks honor).

    Local paths are fingerprinted so the Stop hook can re-ingest on change;
    a bare URL records no fingerprint (server-side clones only see pushes).

    ``repo_root`` is the indexed path itself, not its enclosing git root: the
    indexed unit is exactly what the caller named, so cwd matching stays
    precise (indexing one subdirectory must not claim the whole repository)
    and two subdirectories of one repository cannot record the same root and
    then match a cwd in arbitrary order.

    A remote server cannot read a local path, so there the path stands for the
    repository it is a checkout of: the server is sent that repository's git
    remote and clones it. A clone is always the whole repository, so
    ``repo_root`` is the git root then, and the fingerprint is the pushed
    commit. ``is_local_server`` defaults to what the service URL says.

    The state records the server (``server_identity``): the graph exists there
    and nowhere else.
    """
    if is_local_server is None:
        is_local_server = service_is_local(service_url)
    is_local_server = _reads_disk(is_local_server)
    spec = canonical_spec(spec)
    remote = is_remote_repo(spec)
    repo_root = "" if remote else spec
    if repo_root and not is_local_server:
        repo_root = git_repo_root(repo_root)
        spec = remote_clone_url(repo_root)
        if not spec:
            return _error(
                0,
                "this server cannot read local paths and the directory has no git "
                "remote it could clone — push the repository to a host the server can "
                "reach, or pass its git URL",
            )
        remote = True
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

    fingerprint = _state_fingerprint(repo_root, remote)
    state = {
        "spec": spec,
        "spec_kind": "url" if remote else "path",
        "repo_root": repo_root,
        "server": server_identity(service_url),
        "dataset": dataset,
        "index_vectors": bool(index_vectors),
        "fingerprint": fingerprint,
        "last_index_at": time.time(),
        "last_status": str(result.get("status") or "submitted") if isinstance(result, dict) else "",
    }
    save_repo_state(state)
    if repo_root:
        # Indexing a repo is a yes, whatever was answered at an earlier session start.
        clear_declined(repo_root, is_local_server)
    if remote and repo_root and not fingerprint:
        name = _remote_name(repo_root) or "origin"
        result["note"] = (
            "this checkout does not know which branch is its remote's default, so the graph "
            "will not follow later pushes until it does: fetch that branch and run "
            f"`git remote set-head {name} --auto` in the checkout"
        )
    result["dataset"] = dataset
    return result


def _state_fingerprint(repo_root: str, remote: bool) -> str:
    """What "unchanged since the last index" means for one indexed checkout."""
    if not repo_root:
        return ""
    return pushed_fingerprint(repo_root) if remote else git_fingerprint(repo_root)


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


def reingest_if_changed(
    cwd, service_url, api_key, *, opener=urllib.request.urlopen, is_local_server=None
) -> dict:
    """Freshness pass: re-submit the cwd's indexed repo if it changed.

    Returns a small outcome dict for logging; {} when there is nothing to do
    (no indexed repo for this cwd, a graph built on the other kind of server,
    or nothing new to index). Never raises — this runs on the (async) Stop hook.

    What "changed" means follows how the server reads the repo. A path is read
    in place, so the working tree is the gate. A URL is cloned, and a clone only
    ever holds pushed commits: a local edit would re-pull the same commits and
    produce the same graph, so the gate there is the pushed commit. When this
    checkout cannot tell which branch that is (``default_branch_ref``), nothing
    is submitted and the outcome says so: ``{"skipped": "no_default_branch"}``.

    Only the graph built on this server is refreshed (``state_serves``): a
    graph elsewhere is another server's, and submitting to this one would index
    the repository on a server nobody said yes to. ``is_local_server``
    overrides what the URL says about the kind of server.
    """
    try:
        state = find_indexed_repo(cwd, service_url, is_local_server)
        if not state or state.get("spec_kind") not in ("path", "url"):
            return {}
        root = str(state.get("repo_root") or "")
        cloned = state.get("spec_kind") == "url"
        fingerprint = _state_fingerprint(root, cloned)
        if not fingerprint:
            if cloned:
                # A clone follows the remote's default branch; with that branch
                # unknown here there is no pushed commit to follow, and a graph
                # that quietly stops following pushes is worse than a logged why.
                return {"skipped": "no_default_branch", "repo_root": root}
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
            # Kept on a refresh, and it costs the change, not the repository:
            # the server writes (and so embeds) a code fact again only when its
            # content changed since the last load, and skips an unchanged
            # snapshot outright.
            index_vectors=bool(state.get("index_vectors")),
            opener=opener,
        )
        outcome = {"changed": True, "repo_root": root, "dataset": state.get("dataset")}
        if isinstance(result, dict) and result.get("ok"):
            state["fingerprint"] = fingerprint
            state["last_index_at"] = now
            state["last_status"] = str(result.get("status") or "submitted")
            # A state that predates ``server`` belongs to the server that just
            # took the refresh.
            state["server"] = state.get("server") or server_identity(service_url)
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
# Session-start indexing: ask first, then keep what was indexed fresh
# ---------------------------------------------------------------------------

# Counting a repository's code files stops past this many. The number only
# describes the repository, in the session-start question and in the log; it
# never decides whether the repository is indexed. A yes and the standing yes
# (COGNEE_CODE_AUTOINDEX=always) both index a repository of any size.
_SIZE_COUNT_CAP = 10000

# The question is prepared inside the SessionStart hook itself, so the git
# calls behind it share one short budget instead of the module-wide timeout.
_OFFER_DEADLINE_SECONDS = 3.0
# The one call there that walks the working tree instead of reading the index.
_OFFER_WALK_TIMEOUT_SECONDS = 1.0


def autoindex_mode() -> str:
    """How session start treats an unindexed repo: "ask" | "off" | "always".

    - ``ask`` (default): the user decides. The session asks once whether to
      index the repository, and nothing is submitted without a yes. A "never"
      is remembered per repo, so it is not asked again.
    - ``always``: index a new repo without asking, on a local or a remote
      server, whatever its size. Setting it is the standing yes.
    - ``off``: never ask and never index; only refresh repos indexed explicitly.

    Refreshing an ALREADY-indexed repo is not governed by this setting — that
    repo's index was the consent, and a stale graph is the failure mode the
    freshness loop exists to prevent.
    """
    return parse_autoindex_mode(os.environ.get("COGNEE_CODE_AUTOINDEX", ""))


def parse_autoindex_mode(value) -> str:
    """A ``COGNEE_CODE_AUTOINDEX`` value as a mode; anything unrecognised is "ask"."""
    value = str(value or "").strip().lower()
    if value in ("off", "0", "false", "no"):
        return "off"
    if value in ("always", "1", "true", "yes", "on"):
        return "always"
    return "ask"


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


def _shown_count(count):
    """A code-file count as it is reported: past ``_SIZE_COUNT_CAP``, ``">N"``."""
    if isinstance(count, int) and count > _SIZE_COUNT_CAP:
        return f">{_SIZE_COUNT_CAP}"
    return count


_CODE_EXTENSIONS_BYTES = frozenset(ext.encode("ascii") for ext in CODE_EXTENSIONS)
# How much of a git listing is read at a time.
_LISTING_BLOCK_BYTES = 65536


def _count_listed_code_files(args, cwd: str, timeout: float):
    """Count, as git streams it, what a ``git ls-files -z`` listing names.

    Returns ``(names, code_files)``. The listing is never held whole: it is
    read a block at a time, and git is stopped as soon as the code files pass
    ``_SIZE_COUNT_CAP``. A listing that is mostly something else (an asset or
    data repository) has no such early end and is bounded by ``timeout``
    alone, which the counting runs inside of. None when git got neither to the
    end nor to the cap within ``timeout``; a git that fails (or is missing)
    lists nothing, ``(0, 0)``.
    """
    end = time.monotonic() + timeout
    try:
        proc = subprocess.Popen(
            ["git", *args],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return (0, 0)
    counted = [0, 0]
    capped = threading.Event()

    def count(name: bytes) -> bool:
        """Count one listed name; True once the code files pass the cap."""
        counted[0] += 1
        if name.rsplit(b".", 1)[-1].lower() in _CODE_EXTENSIONS_BYTES:
            counted[1] += 1
        return counted[1] > _SIZE_COUNT_CAP

    def read() -> None:
        rest = b""
        try:
            while True:
                block = proc.stdout.read1(_LISTING_BLOCK_BYTES)
                if not block:
                    break
                *names, rest = (rest + block).split(b"\0")
                for name in names:
                    if name and count(name):
                        capped.set()
                        return
            if rest:
                count(rest)
        except (OSError, ValueError):
            pass

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(max(0.0, end - time.monotonic()))
    if reader.is_alive():
        # The reader may be blocked inside read1(): leave the pipe to it (closing
        # a buffered file another thread is reading can block) and stop git.
        _stop_git(proc)
        return None
    if capped.is_set():
        _stop_git(proc)
        _close_quietly(proc.stdout)
        return (counted[0], counted[1])
    try:
        returncode = proc.wait(timeout=max(0.05, end - time.monotonic()))
    except subprocess.TimeoutExpired:
        _stop_git(proc)
        return None
    finally:
        _close_quietly(proc.stdout)
    return (counted[0], counted[1]) if returncode == 0 else (0, 0)


def _stop_git(proc) -> None:
    try:
        proc.kill()
        proc.wait(timeout=1.0)
    except (OSError, subprocess.SubprocessError):
        pass


def _close_quietly(stream) -> None:
    try:
        stream.close()
    except (OSError, ValueError):
        pass


def _git_code_file_count(root: str, *, tracked_only: bool, deadline=None):
    """Code files git tracks in ``root``, read from the index with no tree walk.

    The number sizes the question, so tracked files are enough for it, and the
    index answers in the same time on a monorepo as on a small service. It is
    counted as git lists it, up to ``_SIZE_COUNT_CAP`` (a count past it is
    ``_SIZE_COUNT_CAP + 1``, see ``_shown_count``). A checkout that tracks
    files but no code (a dotfiles repository in a home directory) is 0, and
    nothing is walked to find that out. None when git did not list the index
    in time: the size is unknown, which is no reason to keep the question from
    the biggest repositories.

    Only a checkout that tracks nothing at all, a fresh ``git init``, is listed
    with its untracked files, since a local server would read those; that walk
    gets a shorter leash still, and one that does not finish in time counts as
    nothing to offer (a tree that big that git tracks nothing of is not a code
    project anyone is working on).
    """
    budget = _bounded_timeout(_OFFER_DEADLINE_SECONDS, deadline)
    tracked = _count_listed_code_files(["ls-files", "-z"], root, budget)
    if tracked is None:
        return None
    names, code_files = tracked
    if names or tracked_only:
        return code_files
    walk = _bounded_timeout(_OFFER_WALK_TIMEOUT_SECONDS, deadline)
    untracked = _count_listed_code_files(
        ["ls-files", "-z", "--others", "--exclude-standard"], root, walk
    )
    return untracked[1] if untracked else 0


# Format characters that stay in names people use: the joiners inside emoji
# and many scripts, and the soft hyphen. Every other one can reorder or hide
# text (bidi controls, zero-width spaces, tag characters).
_NAME_FORMAT_CHARACTERS = frozenset("\u200c\u200d\u00ad")


def _fits_in_instruction(text: str) -> bool:
    """Whether ``text`` can be shown in the session-start instruction as it is.

    Not when it holds a line or paragraph break or any other control
    character, a character that reorders or hides text, or a backtick, which
    would end the code span the command sits in. Any letter, space or emoji
    is fine, including characters the interpreter's Unicode tables are too
    old to name.
    """
    for ch in text:
        if ch == "`":
            return False
        category = unicodedata.category(ch)
        if category in ("Cc", "Cs", "Zl", "Zp"):
            return False
        if category == "Cf" and ch not in _NAME_FORMAT_CHARACTERS:
            return False
    return True


def index_offer(cwd, *, service_url, is_local_server=None) -> dict:
    """The repository a session should ask about indexing on ``service_url``, or {}.

    {} whenever there is nothing to ask: not a git repo, no code in it, already
    indexed on this server, declined before on this kind of server,
    ``COGNEE_CODE_AUTOINDEX`` not on ``ask``, a remote server with no git remote
    it could clone, or a path that cannot be put in the instruction as it is
    (``_fits_in_instruction``). Otherwise ``repo_root``,
    ``source_files`` (None when git did not count them in time, ``">N"`` past
    ``_SIZE_COUNT_CAP``) and ``server`` ("local" or "remote"), plus
    ``clone_url`` for a remote server: the one thing that would be sent.

    Makes no request, and every git call shares one budget
    (``_OFFER_DEADLINE_SECONDS``): it runs inside the SessionStart hook. An
    error, a spent budget included (``TimeoutError``), is the caller's to catch
    and log: SessionStart records it as ``code_index_offer_failed``.
    """
    if autoindex_mode() != "ask":
        return {}
    deadline = time.monotonic() + _OFFER_DEADLINE_SECONDS
    if is_local_server is None:
        is_local_server = service_is_local(service_url)
    is_local_server = _reads_disk(is_local_server)
    root = git_repo_root(cwd, _OFFER_DEADLINE_SECONDS, deadline)
    if not root or not _fits_in_instruction(root):
        return {}
    if find_indexed_repo(root, service_url, is_local_server):
        return {}
    if is_declined(root, is_local_server):
        return {}
    offer = {"repo_root": root, "server": _server_kind(is_local_server)}
    if not is_local_server:
        offer["clone_url"] = remote_clone_url(root, _OFFER_DEADLINE_SECONDS, deadline)
        if not offer["clone_url"]:
            return {}
    offer["source_files"] = _shown_count(
        _git_code_file_count(root, tracked_only=not is_local_server, deadline=deadline)
    )
    return offer if offer["source_files"] != 0 else {}


def decline_repo(cwd, *, service_url="", is_local_server=None) -> dict:
    """Record a "never" for the repository containing ``cwd``.

    Kept per kind of server, not per server: a "never" given for one Cognee
    tenant also stops the question for another, where a yes covers only the
    server it was given for. ``is_local_server`` overrides what ``service_url``
    says about the kind.
    """
    if is_local_server is None:
        is_local_server = service_is_local(service_url)
    is_local_server = _reads_disk(is_local_server)
    root = git_repo_root(cwd) or canonical_spec(cwd)
    record_declined(root, is_local_server)
    return {"ok": True, "declined": root, "server": _server_kind(is_local_server)}


def autoindex_on_session_start(
    cwd,
    service_url,
    api_key,
    *,
    is_local_server=None,
    opener=urllib.request.urlopen,
) -> dict:
    """Refresh the repo a session opened in; index a new one only if told to.

    Runs from the detached session-start worker, so it never blocks the first
    prompt. Returns a small outcome dict for logging; ``{}`` when there is
    nothing to do.

    Two distinct cases, deliberately governed by different rules:
      * the repo is ALREADY indexed on this server -> refresh it if it moved
        since the last index (edits made while the agent was away, or a push
        when the server clones). A graph on another server is not this one's:
        the repo counts as new here;
      * the repo is NEW -> the user decides. In the default ``ask`` mode nothing
        is submitted here: SessionStart puts the question (``index_offer``) and
        a yes runs the index command. Only ``always`` indexes from here, and
        whatever the repository's size, as a yes does.

    The settings are read from the environment. The long-lived cloud worker
    sets them, around this call, to what the session's own SessionStart read:
    its own environment holds whatever they were when the worker started.
    """
    try:
        if is_local_server is None:
            is_local_server = service_is_local(service_url)
        is_local_server = _reads_disk(is_local_server)
        root = git_repo_root(cwd)
        if not root:
            return {}

        indexed = find_indexed_repo(root, service_url, is_local_server)
        if indexed:
            # A session start is the gesture a user makes after fixing what
            # broke indexing (a corrected API key, a restarted/upgraded
            # server), so it always gets one attempt rather than waiting out a
            # backoff window earned by the previous session. Bounded by
            # launches, not turns — which is what the per-turn backoff guards.
            if indexed.get("error_count"):
                _clear_retry_failure(indexed)
                save_repo_state(indexed)
            outcome = reingest_if_changed(
                root, service_url, api_key, opener=opener, is_local_server=is_local_server
            )
            if outcome:
                outcome["reason"] = "refresh"
            return outcome

        mode = autoindex_mode()
        if mode == "off":
            return {"skipped": "autoindex_off", "repo_root": root}
        if not is_local_server and not remote_clone_url(root):
            # Nothing a remote server could clone, so nothing to ask about either.
            return {"skipped": "no_git_remote", "repo_root": root}
        if is_declined(root, is_local_server):
            # A "never" given for this repository holds against a standing yes too.
            return {"skipped": "declined", "repo_root": root}
        if mode == "ask":
            # The question is SessionStart's to put; an unanswered one indexes nothing.
            return {"skipped": "awaiting_consent", "repo_root": root}

        # Counted to tell "no code here" apart and to log the size; any size is
        # indexed (the count stops past _SIZE_COUNT_CAP).
        count = _source_file_count(root, _SIZE_COUNT_CAP)
        if count == 0:
            return {"skipped": "no_code_files", "repo_root": root}

        result = index_repo(
            service_url, api_key, root, opener=opener, is_local_server=is_local_server
        )
        if result == UNREACHABLE or (isinstance(result, dict) and result.get("error")):
            return {
                "indexed": False,
                "repo_root": root,
                "error": "unreachable" if result == UNREACHABLE else str(result.get("error"))[:200],
            }
        outcome = {
            "indexed": True,
            "reason": "first_seen",
            "repo_root": root,
            "dataset": result.get("dataset", ""),
            "source_files": _shown_count(count),
        }
        if result.get("note"):
            outcome["note"] = result["note"]
        return outcome
    except Exception as exc:  # never break session start
        return {"error": str(exc)[:200]}


def main(argv):
    """CLI: ``index <repo-path-or-url> [dataset]``, ``decline <path>`` or ``dataset <path>``.

    argv mirrors the sibling scripts: service_url, api_key, command, spec,
    dataset, index_vectors, wait_seconds, expected_server. The ``dataset``
    command prints the code dataset covering a path on that server (empty line
    when that path is not indexed there) — dataset names carry a path digest,
    so callers resolve the name from the checkout instead of reconstructing it.
    ``decline`` records the "never" answered to the session-start question and
    submits nothing.

    The settings in ``~/.cognee/.env`` are read first, as the hooks read them:
    whether the server reads this disk (``COGNEE_CODE_SERVER_SHARES_FILES``)
    must be decided the same way here as when the question was put.
    ``expected_server`` ("local" or "remote") is what the question told the
    user about that: a yes or a "never" covers that and nothing else, so when
    this command would read the repository the other way (an environment that
    differs from the hooks'), it stops and changes nothing.
    """
    _load_plugin_env_file()
    a = list(argv) + [""] * 8
    expected_server = str(a[7]).strip().lower()
    service_url, api_key, command, spec = a[0], a[1], a[2], a[3]
    dataset = a[4]
    index_vectors = str(a[5]).strip().lower() in ("1", "true", "yes", "on")
    try:
        wait_seconds = float(a[6]) if str(a[6]).strip() else 0.0
    except ValueError:
        wait_seconds = 0.0

    if command == "dataset":
        state = find_indexed_repo(spec or os.getcwd(), service_url)
        print(str(state.get("dataset") or ""))
        return

    actual_server = _server_kind(service_is_local(service_url))
    if command in ("index", "decline") and expected_server in ("local", "remote"):
        if expected_server != actual_server:
            print(
                json.dumps(
                    {
                        "error": f"the question described a {expected_server} Cognee server, "
                        f"but this command would treat {service_url} as a {actual_server} one; "
                        "nothing was submitted or recorded. Tell the user, and ask again "
                        "with what a new session reports.",
                        "status": 0,
                    }
                )
            )
            return

    if command == "decline":
        try:
            answer = decline_repo(spec or os.getcwd(), service_url=service_url)
        except Exception as exc:
            # No server is involved here: printed, this reaches the user as the
            # answer that could not be kept, not as an unreachable server.
            answer = {"error": f"could not record the answer: {str(exc)[:200]}", "status": 0}
        print(json.dumps(answer))
        return

    if command != "index" or not spec:
        print(
            json.dumps(
                {
                    "error": "usage: _code_graph.py <service_url> <api_key> "
                    "index <repo-path-or-url> [dataset] [index_vectors] "
                    "[wait_seconds] | decline [path] | dataset [path]",
                    "status": 2,
                }
            )
        )
        return
    try:
        result = index_repo(
            service_url,
            api_key,
            spec,
            dataset,
            index_vectors=index_vectors,
            wait_seconds=wait_seconds,
        )
    except Exception as exc:
        # Printed rather than raised: an empty output reads as an unreachable
        # server to the wrapper, and this may fail after the server took it.
        result = {"error": f"indexing failed: {str(exc)[:200]}", "status": 0}
    print(UNREACHABLE if result == UNREACHABLE else json.dumps(result))


def _load_plugin_env_file() -> None:
    """Read ``~/.cognee/.env`` as the hooks do; a missing loader changes nothing."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        from _env_file import load_env_file

        load_env_file()
    except Exception:
        pass


if __name__ == "__main__":
    main(sys.argv[1:])
