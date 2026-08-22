# Project Dataset Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (\`- [ ]\`) syntax for tracking.

**Goal:** Add opt-in, deterministic per-project Cognee datasets to the Claude Code and Codex hook integrations while preserving one unique session per host conversation.

**Architecture:** A pure, byte-identical \`_project_dataset.py\` module derives \`project_<slug>_<hash12>\` from a normalized Git origin, shared Git directory, or canonical workspace. Configuration applies the approved precedence, SessionStart pins the chosen dataset in the existing host-keyed launch record, and every recall/write/worker/status surface reads that pinned value. Current upstream does not contain Qwen or Antigravity, so this branch implements Claude Code and Codex; their open PRs receive parity after merge without blocking issue #356.

**Tech Stack:** Python 3.10+, stdlib \`pathlib\`/\`subprocess\`/\`urllib.parse\`/\`hashlib\`, Bash wrappers, pytest shared integration harness, Ruff, GitHub CLI.

---

## File map

- Create \`integrations/claude-code/scripts/_project_dataset.py\`: pure project identity and dataset resolver.
- Create \`integrations/codex/plugins/cognee/scripts/_project_dataset.py\`: byte-identical independently installable resolver.
- Create \`integrations/tests/tests/unit/test_project_dataset.py\`: remote normalization, fallback, worktree, timeout, and cross-copy contract.
- Create \`integrations/tests/tests/unit/test_project_dataset_config.py\`: configuration precedence and non-persistence contract.
- Create \`integrations/tests/tests/unit/test_project_dataset_session_state.py\`: first-writer dataset pinning in launch records.
- Create \`integrations/tests/tests/e2e/test_project_dataset_pipeline.py\`: SessionStart-through-recall/write/final-sync agreement.
- Create \`integrations/tests/tests/integration/test_project_dataset_cli.py\`: direct search/remember wrapper resolution.
- Modify both \`scripts/config.py\` copies: workspace-aware dataset selection and source metadata.
- Modify both \`scripts/_plugin_common.py\` copies: store and expose pinned dataset/source in launch records.
- Modify both \`scripts/session-start.py\` copies: resolve from payload cwd and pin before workers launch.
- Modify both \`scripts/session-context-lookup.py\` copies: use the pinned dataset for recall.
- Modify both \`scripts/sync-session-to-graph.py\` copies: prefer pinned runtime state and correct stale comments.
- Modify both \`scripts/cognee_statusline_render.py\` copies: show the pinned dataset without network access.
- Modify both \`scripts/cognee-search.sh\` and \`scripts/cognee-remember.sh\` copies: resolve project scope from the invoking cwd.
- Modify both \`scripts/doctor.py\` copies: report effective dataset and source.
- Modify \`integrations/tests/utils/isolation.py\`: isolate the new module in shared tests.
- Modify \`integrations/tests/tests/integration/test_doctor.py\` and status-line tests for the new diagnostics/display.
- Modify both plugin READMEs, changelogs, manifests, and \`.claude-plugin/marketplace.json\`: document and version the feature.

### Task 1: Build the pure project dataset resolver

**Files:**
- Create: \`integrations/claude-code/scripts/_project_dataset.py\`
- Create: \`integrations/codex/plugins/cognee/scripts/_project_dataset.py\`
- Modify: \`integrations/tests/utils/isolation.py\`
- Create: \`integrations/tests/tests/unit/test_project_dataset.py\`

- [ ] **Step 1: Register the new module with test isolation**

Add \`"_project_dataset"\` to \`ISOLATED_MODULES\` so imports are reloaded under the temporary HOME:

\`\`\`python
ISOLATED_MODULES = (
    "config",
    "_plugin_common",
    "_project_dataset",
    "_cognee_client",
    "_env_file",
    "_proc",
    "_recall_http",
    "_remember_http",
    "cognee_plugin",
    "cognee_statusline_render",
    "doctor",
)
\`\`\`

- [ ] **Step 2: Write failing pure-resolver tests**

Create parametrized tests covering equivalent remotes, credential stripping, ports, malformed/local remotes, slug bounds, worktrees, non-Git workspaces, Git failures, and byte identity:

\`\`\`python
from __future__ import annotations

import subprocess
import re
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "remote",
    [
        "git@github.com:Topoteretes/Cognee.git",
        "ssh://git@github.com/Topoteretes/Cognee.git",
        "ssh://git@github.com:22/Topoteretes/Cognee.git",
        "https://token:secret@github.com/Topoteretes/Cognee.git",
    ],
)
def test_equivalent_remotes_normalize_identically(suite, isolated_modules, remote):
    resolver = isolated_modules(suite, "_project_dataset")
    assert resolver.normalize_git_remote(remote) == "git:github.com/Topoteretes/Cognee"


def test_non_default_port_is_part_of_identity(suite, isolated_modules):
    resolver = isolated_modules(suite, "_project_dataset")
    assert (
        resolver.normalize_git_remote("ssh://git@example.com:2222/Org/Repo.git")
        == "git:example.com:2222/Org/Repo"
    )


def test_dataset_name_is_bounded_and_contains_no_credentials(suite, isolated_modules):
    resolver = isolated_modules(suite, "_project_dataset")
    name = resolver.dataset_name(
        "git:example.com/Org/This Is A Very Long Repository Name With Ünicode",
        "This Is A Very Long Repository Name With Ünicode",
    )
    assert re.fullmatch(r"project_[a-z0-9-]{1,32}_[0-9a-f]{12}", name)
    assert len(name) <= 53
    assert "secret" not in name


@pytest.mark.parametrize(
    "remote",
    ["/tmp/local/repo.git", "file:///tmp/local/repo.git", "not a remote"],
)
def test_unsupported_or_malformed_remote_returns_none(suite, isolated_modules, remote):
    resolver = isolated_modules(suite, "_project_dataset")
    assert resolver.normalize_git_remote(remote) is None


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def test_remote_linked_worktrees_share_complete_name(suite, isolated_modules, tmp_path):
    resolver = isolated_modules(suite, "_project_dataset")
    repo = tmp_path / "primary"
    linked = tmp_path / "different-worktree-name"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "remote", "add", "origin", "git@github.com:Org/Repo.git")
    (repo / "seed").write_text("x", encoding="utf-8")
    _git(repo, "add", "seed")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "seed")
    _git(repo, "worktree", "add", str(linked))
    assert resolver.derive_project_dataset(str(repo)) == resolver.derive_project_dataset(str(linked))


def test_remote_less_worktrees_share_complete_name(suite, isolated_modules, tmp_path):
    resolver = isolated_modules(suite, "_project_dataset")
    repo = tmp_path / "primary"
    linked = tmp_path / "linked"
    repo.mkdir()
    _git(repo, "init")
    (repo / "seed").write_text("x", encoding="utf-8")
    _git(repo, "add", "seed")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "seed")
    _git(repo, "worktree", "add", str(linked))
    assert resolver.derive_project_dataset(str(repo)) == resolver.derive_project_dataset(str(linked))


def test_git_failure_falls_back_to_workspace(suite, isolated_modules, tmp_path, monkeypatch):
    resolver = isolated_modules(suite, "_project_dataset")
    workspace = tmp_path / "plain"
    workspace.mkdir()
    monkeypatch.setattr(resolver.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert resolver.derive_project_dataset(str(workspace)).startswith("project_plain_")


def test_git_timeout_falls_back_to_workspace(suite, isolated_modules, tmp_path, monkeypatch):
    resolver = isolated_modules(suite, "_project_dataset")
    workspace = tmp_path / "plain"
    workspace.mkdir()
    monkeypatch.setattr(
        resolver.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd=["git"], timeout=1)
        ),
    )
    assert resolver.derive_project_dataset(str(workspace)).startswith("project_plain_")


def test_invalid_workspace_returns_none(suite, isolated_modules, tmp_path):
    resolver = isolated_modules(suite, "_project_dataset")
    assert resolver.derive_project_dataset(str(tmp_path / "missing")) is None


def test_resolver_copies_are_byte_identical():
    root = Path(__file__).resolve().parents[3]
    claude = root / "claude-code" / "scripts" / "_project_dataset.py"
    codex = root / "codex" / "plugins" / "cognee" / "scripts" / "_project_dataset.py"
    assert claude.read_bytes() == codex.read_bytes()
\`\`\`

- [ ] **Step 3: Run the resolver tests and confirm RED**

Run:

\`\`\`bash
cd integrations/tests
uv run pytest tests/unit/test_project_dataset.py -v
\`\`\`

Expected: collection/import failures because \`_project_dataset.py\` does not exist.

- [ ] **Step 4: Implement the resolver in both plugin packages**

Implement the same public contract in both files:

\`\`\`python
from __future__ import annotations

import hashlib
import re
import subprocess
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

GIT_TIMEOUT_SECONDS = 1.0
_SUPPORTED_SCHEMES = {"git", "http", "https", "ssh"}
_SCP_REMOTE = re.compile(r"^(?:[^@/\s]+@)?(?P<host>\[[^\]]+\]|[^:/\s]+):(?P<path>.+)$")


def _repository_path(value: str) -> str:
    path = str(value or "").strip().strip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    return path.strip("/")


def normalize_git_remote(remote: str) -> str | None:
    value = str(remote or "").strip()
    if not value:
        return None
    if "://" not in value:
        match = _SCP_REMOTE.fullmatch(value)
        if not match or (len(match["host"]) == 1 and match["path"].startswith(("\\", "/"))):
            return None
        host = match["host"].strip("[]").lower()
        path = _repository_path(match["path"])
        return f"git:{host}/{path}" if host and path else None
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        if scheme not in _SUPPORTED_SCHEMES or not parsed.hostname:
            return None
        host = parsed.hostname.lower()
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if scheme == "ssh" and port == 22:
        port = None
    host_part = f"[{host}]" if ":" in host else host
    if port is not None:
        host_part = f"{host_part}:{port}"
    path = _repository_path(parsed.path)
    return f"git:{host_part}/{path}" if path else None


def _slug(value: str) -> str:
    ascii_value = (
        unicodedata.normalize("NFKD", str(value or ""))
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")[:32].rstrip("-")
    return slug or "workspace"


def dataset_name(identity: str, slug_source: str) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"project_{_slug(slug_source)}_{digest}"


def _run_git(workspace: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(workspace),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _canonical_dir(value: str | Path, *, relative_to: Path | None = None) -> Path | None:
    try:
        path = Path(value).expanduser()
        if relative_to is not None and not path.is_absolute():
            path = relative_to / path
        path = path.resolve(strict=True)
        return path if path.is_dir() else None
    except (OSError, RuntimeError, ValueError):
        return None


def derive_project_dataset(workspace: str) -> str | None:
    root = _canonical_dir(workspace)
    if root is None:
        return None
    top_raw = _run_git(root, "rev-parse", "--show-toplevel")
    top = _canonical_dir(top_raw) if top_raw else None
    if top is not None:
        normalized = normalize_git_remote(_run_git(root, "config", "--get", "remote.origin.url"))
        if normalized:
            return dataset_name(normalized, normalized.rsplit("/", 1)[-1])
        common_raw = _run_git(root, "rev-parse", "--git-common-dir")
        common = _canonical_dir(common_raw, relative_to=root) if common_raw else None
        if common is not None:
            slug_source = common.parent.name if common.name == ".git" else common.name.removesuffix(".git")
            return dataset_name(f"gitdir:{common}", slug_source)
    return dataset_name(f"workspace:{root}", root.name)
\`\`\`

- [ ] **Step 5: Run resolver tests and confirm GREEN**

Run: \`uv run pytest tests/unit/test_project_dataset.py -v\`

Expected: all resolver cases pass on both suites.

- [ ] **Step 6: Commit**

\`\`\`bash
git add integrations/claude-code/scripts/_project_dataset.py \
  integrations/codex/plugins/cognee/scripts/_project_dataset.py \
  integrations/tests/utils/isolation.py \
  integrations/tests/tests/unit/test_project_dataset.py
git commit -m "feat: derive stable project datasets"
\`\`\`

### Task 2: Apply configuration precedence

**Files:**
- Modify: \`integrations/claude-code/scripts/config.py\`
- Modify: \`integrations/codex/plugins/cognee/scripts/config.py\`
- Modify: both \`scripts/_env_file.py\` copies
- Create: \`integrations/tests/tests/unit/test_project_dataset_config.py\`
- Modify: \`integrations/tests/tests/unit/test_env_file.py\`

- [ ] **Step 1: Write failing precedence tests**

\`\`\`python
def test_scope_absent_keeps_default(suite, isolated_modules, project_dir):
    config = isolated_modules(suite, "config")
    loaded = config.load_config(str(project_dir))
    assert loaded["dataset"] == "agent_sessions"
    assert loaded["_dataset_source"] == "default"


def test_unknown_scope_keeps_default(suite, isolated_modules, project_dir, monkeypatch):
    config = isolated_modules(suite, "config")
    monkeypatch.setenv("COGNEE_DATASET_SCOPE", "repository")
    loaded = config.load_config(str(project_dir))
    assert (loaded["dataset"], loaded["_dataset_source"]) == ("agent_sessions", "default")


def test_project_scope_derives_dataset(suite, isolated_modules, project_dir, monkeypatch):
    config = isolated_modules(suite, "config")
    monkeypatch.setenv("COGNEE_DATASET_SCOPE", " Project ")
    monkeypatch.setattr(config, "derive_project_dataset", lambda workspace: "project_repo_abc123def456")
    loaded = config.load_config(str(project_dir))
    assert loaded["dataset"] == "project_repo_abc123def456"
    assert loaded["_dataset_source"] == "project"


def test_explicit_dataset_wins_over_project_scope(suite, isolated_modules, project_dir, monkeypatch):
    config = isolated_modules(suite, "config")
    monkeypatch.setenv("COGNEE_DATASET_SCOPE", "project")
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", "explicit")
    loaded = config.load_config(str(project_dir))
    assert (loaded["dataset"], loaded["_dataset_source"]) == ("explicit", "env")


def test_picker_marker_wins_over_project_scope(suite, isolated_modules, project_dir, monkeypatch):
    config = isolated_modules(suite, "config")
    monkeypatch.setenv("COGNEE_DATASET_SCOPE", "project")
    selected = config._apply_project_dataset(
        {"dataset": "picked", "_dataset_source": "picker"},
        str(project_dir),
    )
    assert (selected["dataset"], selected["_dataset_source"]) == ("picked", "picker")


def test_derived_dataset_is_not_persisted_globally(suite, isolated_modules, project_dir, monkeypatch):
    config = isolated_modules(suite, "config")
    monkeypatch.setenv("COGNEE_DATASET_SCOPE", "project")
    monkeypatch.setattr(config, "derive_project_dataset", lambda workspace: "project_repo_abc123def456")
    loaded = config.load_config(str(project_dir))
    config.save_config(loaded)
    saved = json.loads(config._CONFIG_FILE.read_text(encoding="utf-8"))
    assert saved["dataset"] == "agent_sessions"
    assert "_dataset_source" not in saved
\`\`\`

- [ ] **Step 2: Run the precedence tests and confirm RED**

Run: \`uv run pytest tests/unit/test_project_dataset_config.py -v\`

Expected: \`load_config\` rejects the workspace argument and has no source marker.

- [ ] **Step 3: Implement selection and persistence**

In both config copies, import the resolver, add \`COGNEE_DATASET_SCOPE\` to the environment map, and apply selection after the environment layer:

\`\`\`python
from _project_dataset import derive_project_dataset

# in _ENV_MAP
"COGNEE_DATASET_SCOPE": "_dataset_scope",


def _workspace(workspace: str | None) -> str:
    return str(workspace or os.environ.get(_HOST_CWD_ENV) or os.getcwd())


def _apply_project_dataset(config: dict, workspace: str | None = None) -> dict:
    source = str(config.get("_dataset_source") or "default")
    config["_dataset_source"] = source
    if source != "default":
        return config
    scope = str(config.get("_dataset_scope") or "").strip().lower()
    if scope != "project":
        return config
    derived = derive_project_dataset(_workspace(workspace))
    if derived:
        config["dataset"] = derived
        config["_dataset_source"] = "project"
    return config
\`\`\`

Define \`_HOST_CWD_ENV = "CLAUDE_CWD"\` in Claude and \`"CODEX_CWD"\` in Codex. Change the signature to \`load_config(workspace: str | None = None) -> dict\`, initialize \`_dataset_source="default"\`, ignore underscore-prefixed config-file keys, mark a non-empty \`COGNEE_PLUGIN_DATASET\` as \`env\`, then return \`_apply_project_dataset(config, workspace)\`.

Before writing \`to_save\`, keep project-specific values out of the global visibility file:

\`\`\`python
if config.get("_dataset_source") in {"picker", "project"}:
    config = dict(config)
    config["dataset"] = _DEFAULTS["dataset"]
\`\`\`

- [ ] **Step 4: Document the opt-in in the generated env template**

Add this exact optional block to both \`_env_file.py\` templates:

\`\`\`text
# Derive one shared dataset per Git repository/workspace:
# COGNEE_DATASET_SCOPE="project"
# An explicit COGNEE_PLUGIN_DATASET always wins:
# COGNEE_PLUGIN_DATASET="agent_sessions"
\`\`\`

- [ ] **Step 5: Run focused config/env tests**

Run:

\`\`\`bash
uv run pytest tests/unit/test_project_dataset_config.py tests/unit/test_env_file.py -v
\`\`\`

Expected: all tests pass for Claude Code and Codex.

- [ ] **Step 6: Commit**

\`\`\`bash
git add integrations/claude-code/scripts/config.py \
  integrations/codex/plugins/cognee/scripts/config.py \
  integrations/claude-code/scripts/_env_file.py \
  integrations/codex/plugins/cognee/scripts/_env_file.py \
  integrations/tests/tests/unit/test_project_dataset_config.py \
  integrations/tests/tests/unit/test_env_file.py
git commit -m "feat: select project datasets by precedence"
\`\`\`

### Task 3: Pin the selected dataset in launch state

**Files:**
- Modify: both \`scripts/_plugin_common.py\` copies
- Create: \`integrations/tests/tests/unit/test_project_dataset_session_state.py\`

- [ ] **Step 1: Write failing launch-state tests**

\`\`\`python
def test_launch_record_pins_dataset_and_source(suite, isolated_modules, monkeypatch):
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(
        common,
        "_json_http_request",
        lambda *a, **k: (_ for _ in ()).throw(OSError("offline test")),
    )
    common.ensure_launch_record(
        "host-one",
        "/repo",
        dataset="project_repo_111111111111",
        dataset_source="project",
    )
    resolved = common.load_resolved("host-one")
    assert resolved["dataset"] == "project_repo_111111111111"
    assert resolved["dataset_source"] == "project"


def test_later_resolution_cannot_switch_pinned_dataset(suite, isolated_modules):
    common = isolated_modules(suite, "_plugin_common")
    common.ensure_launch_record(
        "host-one", "/repo-a", dataset="project_a_111111111111", dataset_source="project"
    )
    common.ensure_launch_record(
        "host-one", "/repo-b", dataset="project_b_222222222222", dataset_source="project"
    )
    assert common.get_launch_dataset("host-one") == ("project_a_111111111111", "project")


def test_conversations_keep_distinct_sessions_in_one_dataset(suite, isolated_modules):
    common = isolated_modules(suite, "_plugin_common")
    dataset = "project_repo_111111111111"
    first = common.ensure_launch_record("host-one", "/repo", dataset=dataset, dataset_source="project")
    second = common.ensure_launch_record("host-two", "/repo", dataset=dataset, dataset_source="project")
    assert first[0] != second[0]
    assert common.get_launch_dataset("host-one")[0] == common.get_launch_dataset("host-two")[0]


def test_exit_worker_receives_pinned_dataset(suite, hook_module, monkeypatch):
    watcher = hook_module(suite, "exit-watcher.py")
    captured = {}

    class FakeProcess:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(watcher.subprocess, "Popen", FakeProcess)
    watcher._spawn_sync(
        "session-one",
        "project_repo_111111111111",
        session_key="host-one",
    )
    assert captured["env"]["COGNEE_SYNC_DATASET"] == "project_repo_111111111111"
\`\`\`

- [ ] **Step 2: Run and confirm RED**

Run: \`uv run pytest tests/unit/test_project_dataset_session_state.py -v\`

Expected: \`ensure_launch_record\` does not accept dataset fields.

- [ ] **Step 3: Extend launch records without changing session semantics**

Add optional keyword-only fields while preserving the existing tuple return:

\`\`\`python
def _pin_launch_dataset(host_key: str, record: dict, dataset: str, source: str) -> dict:
    if not host_key or not dataset or record.get("dataset"):
        return record
    updated = dict(record)
    updated["dataset"] = dataset
    updated["dataset_source"] = source or "default"
    _write_map_record(host_key, updated)
    return _read_map_record(host_key) or updated


def get_launch_dataset(host_key: str = "") -> tuple[str, str]:
    record = _read_map_record(_sanitize_session_key(host_key) or get_session_key())
    return (
        str(record.get("dataset") or ""),
        str(record.get("dataset_source") or ""),
    )
\`\`\`

Change \`ensure_launch_record\` to accept \`dataset: str = ""\` and \`dataset_source: str = ""\`, include them in a new record, and fill them only when a legacy record has no dataset. In \`load_resolved\`, read the launch record before HTTP lookup and add \`dataset\` and \`dataset_source\` when present.

- [ ] **Step 4: Run focused tests**

Run:

\`\`\`bash
uv run pytest tests/unit/test_project_dataset_session_state.py tests/unit/test_session_id.py -v
\`\`\`

Expected: all launch-state and existing session-ID tests pass.

- [ ] **Step 5: Commit**

\`\`\`bash
git add integrations/claude-code/scripts/_plugin_common.py \
  integrations/codex/plugins/cognee/scripts/_plugin_common.py \
  integrations/tests/tests/unit/test_project_dataset_session_state.py
git commit -m "feat: pin datasets to conversation state"
\`\`\`

### Task 4: Wire SessionStart, recall, workers, and final sync

**Files:**
- Modify: both \`scripts/session-start.py\` copies
- Modify: both \`scripts/session-context-lookup.py\` copies
- Modify: both \`scripts/sync-session-to-graph.py\` copies
- Create: \`integrations/tests/tests/e2e/test_project_dataset_pipeline.py\`

- [ ] **Step 1: Write a failing end-to-end pinning test**

The test must initialize a Git project, start a session with project scope, then change the working directory before recall/write/sync and prove every request still uses the first dataset:

\`\`\`python
def test_pipeline_uses_session_start_dataset(
    suite, run_hook, payloads, mock_server, project_dir, tmp_path
):
    subprocess.run(["git", "init"], cwd=project_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:Org/SharedRepo.git"],
        cwd=project_dir,
        check=True,
    )
    other = tmp_path / "other"
    other.mkdir()
    env = {"COGNEE_DATASET_SCOPE": "project"}
    host_id = "project-scope-host"

    started = run_hook(
        suite,
        "session-start.py",
        stdin=payloads.session_start(session_id=host_id, cwd=str(project_dir)),
        cwd=project_dir,
        service_url=mock_server.url,
        env=env,
    )
    assert started.returncode == 0, started.stderr

    recalled = run_hook(
        suite,
        "session-context-lookup.py",
        stdin=payloads.user_prompt(session_id=host_id, cwd=str(other), prompt="recall project decision"),
        cwd=other,
        service_url=mock_server.url,
        env=env,
    )
    assert recalled.returncode == 0, recalled.stderr

    calls = [call for call in mock_server.calls if call["path"] == "/api/v1/recall"]
    datasets = {tuple(call["json"].get("datasets") or []) for call in calls}
    assert len(datasets) == 1
    only = next(iter(datasets))
    assert len(only) == 1 and only[0].startswith("project_sharedrepo_")
\`\`\`

Continue the same test after the recall assertion:

\`\`\`python
    prompted = run_hook(
        suite,
        "store-user-prompt.py",
        stdin=payloads.user_prompt(
            session_id=host_id,
            cwd=str(other),
            prompt="remember the pinned project decision",
        ),
        cwd=other,
        service_url=mock_server.url,
        env=env,
    )
    assert prompted.returncode == 0, prompted.stderr

    stopped = run_hook(
        suite,
        "store-to-session.py",
        "--stop",
        stdin=payloads.stop(
            session_id=host_id,
            assistant_message="the pinned project decision is stable",
            cwd=str(other),
        ),
        cwd=other,
        service_url=mock_server.url,
        env=env,
    )
    assert stopped.returncode == 0, stopped.stderr
    writes = [call for call in mock_server.calls if call["path"] == "/api/v1/remember/entry"]
    assert writes
    assert {call["json"]["dataset_name"] for call in writes} == {only[0]}

    synced = run_hook(
        suite,
        "sync-session-to-graph.py",
        stdin={"hook_event_name": "ManualSync", "session_id": host_id, "cwd": str(other)},
        cwd=other,
        service_url=mock_server.url,
        env=env,
    )
    assert synced.returncode == 0, synced.stderr
    improves = [call for call in mock_server.calls if call["path"] == "/api/v1/improve"]
    assert improves
    assert improves[-1]["json"]["dataset_name"] == only[0]
\`\`\`

The exit-worker \`COGNEE_SYNC_DATASET\` propagation is pinned separately in
\`test_exit_worker_receives_pinned_dataset\` from Task 3.

- [ ] **Step 2: Run and confirm RED**

Run: \`uv run pytest tests/e2e/test_project_dataset_pipeline.py -v\`

Expected: recall targets the later cwd/default instead of pinned runtime state.

- [ ] **Step 3: Pin at SessionStart**

In each \`_start\`, parse payload/cwd before loading configuration, then pin and read back the winning record:

\`\`\`python
payload = payload or {}
cwd = str(payload.get("cwd") or os.environ.get(_HOST_CWD_ENV) or os.getcwd())
config = load_config(cwd)
candidate_dataset = get_dataset(config)
candidate_source = str(config.get("_dataset_source") or "default")

session_id, conn_uuid = ensure_launch_record(
    session_key,
    cwd,
    dataset=candidate_dataset,
    dataset_source=candidate_source,
)
dataset, dataset_source = get_launch_dataset(session_key)
dataset = dataset or candidate_dataset
dataset_source = dataset_source or candidate_source
\`\`\`

Import \`get_launch_dataset\`, retain existing worker arguments, and include \`dataset\`/\`dataset_source\` in the \`session_resolved\` event.

- [ ] **Step 4: Make recall consume pinned runtime state**

Replace the session-ID-only helper with:

\`\`\`python
def _load_session(workspace: str = "") -> tuple[str, str]:
    resolved = load_resolved()
    session_id = str(resolved.get("session_id") or "")
    dataset = str(resolved.get("dataset") or "")
    if not session_id or not dataset:
        config = load_config(workspace or None)
        session_id = session_id or get_session_id(config, workspace or None)
        dataset = dataset or get_dataset(config)
    return session_id, dataset
\`\`\`

Pass the payload cwd into \`_run\`, use the returned \`dataset\` for every \`recall_via_http\` call, and leave session IDs unchanged.

- [ ] **Step 5: Make final sync prefer pinned state**

Keep \`COGNEE_SYNC_DATASET\` as the detached-worker override, then use \`data["dataset"]\` from \`load_resolved\` before any config fallback. Update the obsolete comment that says resolved state carries no dataset.

- [ ] **Step 6: Run pipeline and regression tests**

\`\`\`bash
uv run pytest tests/e2e/test_project_dataset_pipeline.py \
  tests/e2e/test_user_prompt_hooks.py \
  tests/integration/test_sync_strict.py -v
\`\`\`

Expected: all tests pass for both suites.

- [ ] **Step 7: Commit**

\`\`\`bash
git add integrations/claude-code/scripts/session-start.py \
  integrations/codex/plugins/cognee/scripts/session-start.py \
  integrations/claude-code/scripts/session-context-lookup.py \
  integrations/codex/plugins/cognee/scripts/session-context-lookup.py \
  integrations/claude-code/scripts/sync-session-to-graph.py \
  integrations/codex/plugins/cognee/scripts/sync-session-to-graph.py \
  integrations/tests/tests/e2e/test_project_dataset_pipeline.py
git commit -m "feat: keep hook pipelines on pinned datasets"
\`\`\`

### Task 5: Update status, doctor, and direct CLI surfaces

**Files:**
- Modify: both \`scripts/cognee_statusline_render.py\` copies
- Modify: both \`scripts/doctor.py\` copies
- Modify: both \`scripts/cognee-search.sh\` copies
- Modify: both \`scripts/cognee-remember.sh\` copies
- Modify: \`integrations/tests/tests/e2e/test_statusline_bar.py\`
- Modify: \`integrations/tests/tests/integration/test_doctor.py\`
- Create: \`integrations/tests/tests/integration/test_project_dataset_cli.py\`

- [ ] **Step 1: Write failing status and doctor tests**

\`\`\`python
def test_bar_prefers_pinned_dataset(suite, run_hook, temp_home):
    if suite.name == "claude-code":
        settings = temp_home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps({"enabledPlugins": {"cognee-memory@cognee": True}}),
            encoding="utf-8",
        )
    sessions = temp_home / ".cognee-plugin" / suite.state_subdir / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "host-one.json").write_text(
        json.dumps({"session_id": "sid", "dataset": "project_repo_111111111111"}),
        encoding="utf-8",
    )
    result = run_hook(
        suite,
        "cognee_statusline_render.py",
        stdin={"session_id": "host-one", "cwd": "/wrong/project"},
        service_url="https://api.example-cognee.ai",
        api_key=None,
    )
    assert "cognee: project_repo_111111111111" in result.stdout


def test_bar_derives_before_launch_record_exists(
    suite, run_hook, temp_home, project_dir
):
    if suite.name == "claude-code":
        settings = temp_home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps({"enabledPlugins": {"cognee-memory@cognee": True}}),
            encoding="utf-8",
        )
    subprocess.run(["git", "init"], cwd=project_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:Org/StatusRepo.git"],
        cwd=project_dir,
        check=True,
    )
    result = run_hook(
        suite,
        "cognee_statusline_render.py",
        stdin={"session_id": "new-host", "cwd": str(project_dir)},
        cwd=project_dir,
        service_url="https://api.example-cognee.ai",
        api_key=None,
        env={"COGNEE_DATASET_SCOPE": "project"},
    )
    assert "cognee: project_statusrepo_" in result.stdout
\`\`\`

Add \`dataset\` and \`dataset_source\` to \`_REPORT_KEYS\`, then add:

\`\`\`python
def test_report_includes_effective_project_dataset(
    doctor, project_dir, monkeypatch
):
    monkeypatch.chdir(project_dir)
    monkeypatch.setenv("COGNEE_DATASET_SCOPE", "project")
    report = doctor.collect_report()
    assert report["dataset"].startswith("project_project_")
    assert report["dataset_source"] == "project"
\`\`\`

- [ ] **Step 2: Run and confirm RED**

Run:

\`\`\`bash
uv run pytest tests/e2e/test_statusline_bar.py \
  tests/integration/test_doctor.py -v
\`\`\`

Expected: status shows \`agent_sessions\`; doctor lacks the new keys.

- [ ] **Step 3: Implement network-free status resolution**

In each renderer, define its suite-specific sessions directory and change the dataset helper signature:

\`\`\`python
def _active_dataset(host_id: str = "", cwd: str = "") -> str:
    if _path_safe(host_id):
        record = _read_json(_SESSIONS_MAP_DIR / f"{host_id}.json")
        pinned = str(record.get("dataset") or "").strip()
        if pinned:
            return pinned
    explicit = os.environ.get("COGNEE_PLUGIN_DATASET", "").strip()
    if explicit:
        return explicit
    if os.environ.get("COGNEE_DATASET_SCOPE", "").strip().lower() == "project" and cwd:
        try:
            from _project_dataset import derive_project_dataset

            derived = derive_project_dataset(cwd)
            if derived:
                return derived
        except Exception:
            pass
    return _DEFAULT_DATASET
\`\`\`

Parse \`session_id\` and cwd from stdin in both main functions. Pass the host ID through \`render_status_for_host\` in Codex so imported hook-context rendering uses the pinned map.

- [ ] **Step 4: Add doctor dataset diagnostics**

\`\`\`python
def _resolve_dataset() -> tuple[str, str]:
    from config import get_dataset, load_config

    cfg = load_config(os.getcwd())
    return get_dataset(cfg), str(cfg.get("_dataset_source") or "default")
\`\`\`

Add \`dataset\` and \`dataset_source\` to \`collect_report\` and display rows \`Dataset\` and \`Dataset Source\`.

- [ ] **Step 5: Route Bash wrappers through shared config**

Inside each wrapper's embedded Python, replace direct \`COGNEE_PLUGIN_DATASET\` reads with:

\`\`\`python
sys.path.insert(0, sys.argv[2])
from config import get_dataset, load_config

dataset = get_dataset(load_config(os.getcwd()))
\`\`\`

Keep \`cognee-remember.sh --dataset\` parsing after this resolution so the flag remains highest precedence. Do not add a new search flag.

- [ ] **Step 6: Test Bash wrappers against the mock server**

Use \`build_env\` and \`subprocess.run(["bash", wrapper, ...])\` on POSIX. Assert \`/api/v1/recall.datasets\` and \`/api/v1/remember.datasetName\` use the derived dataset, and \`--dataset explicit\` wins for remember. Mark the file \`pytest.mark.skipif(os.name == "nt", reason="Bash wrapper contract")\`.

- [ ] **Step 7: Run focused surface tests**

\`\`\`bash
uv run pytest tests/e2e/test_statusline_bar.py \
  tests/integration/test_doctor.py \
  tests/integration/test_project_dataset_cli.py -v
\`\`\`

Expected: all tests pass.

- [ ] **Step 8: Commit**

\`\`\`bash
git add integrations/claude-code/scripts/cognee_statusline_render.py \
  integrations/codex/plugins/cognee/scripts/cognee_statusline_render.py \
  integrations/claude-code/scripts/doctor.py \
  integrations/codex/plugins/cognee/scripts/doctor.py \
  integrations/claude-code/scripts/cognee-search.sh \
  integrations/codex/plugins/cognee/scripts/cognee-search.sh \
  integrations/claude-code/scripts/cognee-remember.sh \
  integrations/codex/plugins/cognee/scripts/cognee-remember.sh \
  integrations/tests/tests/e2e/test_statusline_bar.py \
  integrations/tests/tests/integration/test_doctor.py \
  integrations/tests/tests/integration/test_project_dataset_cli.py
git commit -m "feat: surface effective project datasets"
\`\`\`

### Task 6: Document and version the feature

**Files:**
- Modify: \`integrations/claude-code/README.md\`
- Modify: \`integrations/codex/README.md\`
- Modify: \`integrations/claude-code/CHANGELOG.md\`
- Modify: \`integrations/codex/plugins/cognee/CHANGELOG.md\`
- Modify: \`integrations/claude-code/.claude-plugin/plugin.json\`
- Modify: \`integrations/codex/plugins/cognee/.codex-plugin/plugin.json\`
- Modify: \`.claude-plugin/marketplace.json\`

- [ ] **Step 1: Add exact user documentation**

Document:

\`\`\`bash
# Remove/comment a global fixed dataset first:
# COGNEE_PLUGIN_DATASET="agent_sessions"

# Then enable automatic project isolation:
COGNEE_DATASET_SCOPE="project"
\`\`\`

State the precedence \`explicit env > #285 picker when present > derived project > agent_sessions\`, the \`project_<slug>_<hash12>\` format, stable Git remote/worktree behavior, per-conversation session IDs, no migration/deletion, and restart-on-next-session requirement. Update the launch-map example to show \`dataset\` and \`dataset_source\`.

- [ ] **Step 2: Bump plugin patch versions**

- Claude Code: \`1.3.3 -> 1.3.4\` in its manifest, marketplace entry, and changelog.
- Codex: \`1.4.3 -> 1.4.4\` in its manifest and changelog.

Add an \`Added\` changelog entry that links issue #356 and names the opt-in behavior.

- [ ] **Step 3: Validate manifests and pins**

\`\`\`bash
python -m json.tool integrations/claude-code/.claude-plugin/plugin.json >/dev/null
python -m json.tool integrations/codex/plugins/cognee/.codex-plugin/plugin.json >/dev/null
python -m json.tool .claude-plugin/marketplace.json >/dev/null
python scripts/check_version_pins.py
\`\`\`

Expected: all commands exit 0.

- [ ] **Step 4: Commit**

\`\`\`bash
git add integrations/claude-code/README.md integrations/codex/README.md \
  integrations/claude-code/CHANGELOG.md \
  integrations/codex/plugins/cognee/CHANGELOG.md \
  integrations/claude-code/.claude-plugin/plugin.json \
  integrations/codex/plugins/cognee/.codex-plugin/plugin.json \
  .claude-plugin/marketplace.json
git commit -m "docs: explain project dataset isolation"
\`\`\`

### Task 7: Full verification and branch review

**Files:**
- Verify all branch changes; no new production file is introduced in this task.

- [ ] **Step 1: Run formatting and lint**

\`\`\`bash
uvx ruff==0.16.0 format --check integrations/
uvx ruff==0.16.0 check integrations/
\`\`\`

Expected: both commands exit 0.

- [ ] **Step 2: Run the complete hermetic suite**

\`\`\`bash
cd integrations/tests
uv sync --dev
uv run pytest tests/ -v
\`\`\`

Expected: all non-live tests pass; only the repository's declared skips/deselections remain.

- [ ] **Step 3: Run repository integrity checks**

\`\`\`bash
cd ../..
git diff --check origin/main...HEAD
python scripts/check_version_pins.py
git status --short
\`\`\`

Expected: no whitespace errors, version pins agree, and the worktree is clean.

- [ ] **Step 4: Review every changed path**

\`\`\`bash
git diff --stat origin/main...HEAD
git diff --name-status origin/main...HEAD
git log --oneline origin/main..HEAD
\`\`\`

Confirm there are no changes outside the spec, no secrets/raw remote values in logs, both resolver copies are byte-identical, and \`agent_sessions\` remains the opt-out default.

### Task 8: Push and open the upstream PR

**Files:**
- No file modifications unless review finds a defect.

- [ ] **Step 1: Recheck upstream integration status**

\`\`\`bash
git fetch origin --prune
gh pr view 354 --repo topoteretes/cognee-integrations --json state,mergedAt,url
gh pr view 355 --repo topoteretes/cognee-integrations --json state,mergedAt,url
\`\`\`

Expected at plan time: both are \`OPEN\`. If either is \`MERGED\`, rebase on \`origin/main\` and add that integration's byte-identical resolver/config/session/status parity before pushing.

- [ ] **Step 2: Push the reviewed branch**

\`\`\`bash
git push -u fork feat/project-dataset-isolation-356
\`\`\`

- [ ] **Step 3: Open the PR**

Create \`/tmp/cognee-project-isolation-pr.md\` with \`apply_patch\` after the
verification run. Its Summary section states the implemented precedence and
session pinning; its Safety section states opt-in/default preservation,
empty-start behavior, and no migration/deletion; its Verification section
copies the complete pytest and Ruff result lines produced in Task 7; and its
Parity section records the current states and URLs of #354 and #355. End with
\`Closes #356\`.

\`\`\`bash
gh pr create \
  --repo topoteretes/cognee-integrations \
  --base main \
  --head netbrah:feat/project-dataset-isolation-356 \
  --title "feat: add opt-in per-project dataset isolation" \
  --body-file /tmp/cognee-project-isolation-pr.md
\`\`\`

- [ ] **Step 4: Verify hosted checks**

\`\`\`bash
gh pr checks --repo topoteretes/cognee-integrations --watch \
  "$(gh pr view --repo topoteretes/cognee-integrations --json url --jq .url)"
\`\`\`

Expected: Linux shared tests, Windows shared tests, Ruff, and repository checks pass.

### Task 9: Roll out locally without disrupting active sessions

**Files/state:**
- Back up and modify \`~/.cognee/.env\`.
- Back up installed Claude Code, Codex, Qwen, and Antigravity plugin roots only after resolving their active paths.
- Do not delete \`agent_sessions\` or restart/kill an active harness.

- [ ] **Step 1: Inventory active processes and loaded plugin roots**

\`\`\`bash
ps -axo pid=,lstart=,command= | rg 'claude|codex|qwen|antigravity|agy'
codex plugin list
claude plugin list
agy plugin list
\`\`\`

Resolve Qwen from its active extension/settings registration rather than \`~/.qwen/tmp\`, which contains session scratch state, not plugin source.

- [ ] **Step 2: Create timestamped, recoverable backups**

Use \`mktemp -d /tmp/cognee-project-scope-backup.XXXXXX\`, copy \`~/.cognee/.env\`, and copy each resolved plugin root with metadata preserved. Record the backup directory in the handoff.

- [ ] **Step 3: Build and verify the Qwen/Antigravity local parity overlays**

Fetch the exact open PR heads into dedicated worktrees:

\`\`\`bash
git fetch fork feat/qwen-cognee-351 feat/antigravity-cognee-352
git worktree add -b local/qwen-project-scope \
  /Users/palanisd/Projects/.worktrees/cognee-qwen-project-scope \
  fork/feat/qwen-cognee-351
git worktree add -b local/antigravity-project-scope \
  /Users/palanisd/Projects/.worktrees/cognee-antigravity-project-scope \
  fork/feat/antigravity-cognee-352
\`\`\`

Port the verified byte-identical resolver plus config selection, launch-record
pinning, host workspace extraction, status/hook output, and direct wrapper
behavior to each host shape. Qwen uses its PR's Qwen cwd/payload fields;
Antigravity uses the first \`workspacePaths\` entry before payload cwd. In each
worktree run \`cd integrations/tests && uv sync --dev && uv run pytest tests/ -v\`,
then run the repository Ruff checks from Task 7. Keep these parity commits local
until #354/#355 merge, then open focused follow-up PRs rather than expanding
their integration PR review scope.

- [ ] **Step 4: Install reviewed plugin files**

Install Claude Code and Codex from the verified #356 branch commit. Install Qwen
and Antigravity from the two verified local parity commits. Use the host's
supported plugin installer when it owns the registered root; if a host has no
installer, replace only its resolved Cognee plugin root after its active process
exits. Preserve credentials, state directories, and unrelated integrations.

- [ ] **Step 5: Change the shared env atomically**

Remove/comment every active \`COGNEE_PLUGIN_DATASET=agent_sessions\` assignment, add one \`COGNEE_DATASET_SCOPE="project"\`, and verify \`COGNEE_SESSION_ID\` is absent. Preserve all API keys and backend settings byte-for-byte.

- [ ] **Step 6: Restart one harness at a time**

Wait for each current session to finish naturally. Launch a fresh session in the same repository, verify its status/hook log shows \`project_<slug>_<hash12>\`, then proceed to the next harness. Never kill a process to accelerate rollout.

- [ ] **Step 7: Prove cross-harness project isolation**

For two harnesses in this repository, inspect their host-keyed launch records and assert:

\`\`\`text
dataset A == dataset B
session_id A != session_id B
\`\`\`

Launch one harness in another repository and assert its dataset differs. Confirm \`agent_sessions\` remains present and untouched for manual rollback/recall.

- [ ] **Step 8: Verify health signals**

- Claude Code: status line shows the derived dataset and healthy mode/glyph.
- Codex: \`UserPromptSubmit\` context shows the derived dataset and saved/recall counts.
- Qwen: its hook log records SessionStart and the derived dataset.
- Antigravity: \`~/.cognee-plugin/antigravity/hook.log\` exists for a new session and records the derived dataset; no permanent status bar is expected from the current host.
