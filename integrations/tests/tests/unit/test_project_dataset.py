from __future__ import annotations

import re
import subprocess
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


@pytest.mark.parametrize(
    "remote",
    [
        "git@[2001:db8::1]:Org/Repo.git",
        "ssh://git@[2001:db8::1]/Org/Repo.git",
    ],
)
def test_ipv6_scp_and_url_remotes_normalize_identically(suite, isolated_modules, remote):
    resolver = isolated_modules(suite, "_project_dataset")
    assert resolver.normalize_git_remote(remote) == "git:[2001:db8::1]/Org/Repo"


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
    ["/tmp/local/repo.git", "file:///tmp/local/repo.git", "not a remote", "C:repo"],
)
def test_unsupported_or_malformed_remote_returns_none(suite, isolated_modules, remote):
    resolver = isolated_modules(suite, "_project_dataset")
    assert resolver.normalize_git_remote(remote) is None


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
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
    assert resolver.derive_project_dataset(str(repo)) == resolver.derive_project_dataset(
        str(linked)
    )


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
    assert resolver.derive_project_dataset(str(repo)) == resolver.derive_project_dataset(
        str(linked)
    )


def test_git_failure_falls_back_to_workspace(suite, isolated_modules, tmp_path, monkeypatch):
    resolver = isolated_modules(suite, "_project_dataset")
    workspace = tmp_path / "plain"
    workspace.mkdir()
    monkeypatch.setattr(
        resolver.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError())
    )
    assert resolver.derive_project_dataset(str(workspace)).startswith("project_plain_")


def test_git_timeout_falls_back_to_workspace(suite, isolated_modules, tmp_path, monkeypatch):
    resolver = isolated_modules(suite, "_project_dataset")
    workspace = tmp_path / "plain"
    workspace.mkdir()
    monkeypatch.setattr(
        resolver.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd=["git"], timeout=1)),
    )
    assert resolver.derive_project_dataset(str(workspace)).startswith("project_plain_")


def test_git_decoding_failure_falls_back_without_replacement_text(
    suite, isolated_modules, tmp_path, monkeypatch
):
    resolver = isolated_modules(suite, "_project_dataset")
    workspace = tmp_path / "plain"
    workspace.mkdir()
    invocation = {}

    def decoding_failure(*args, **kwargs):
        invocation.update(kwargs)
        raise UnicodeDecodeError("utf-8", b"\xffsecret", 0, 1, "invalid start byte")

    monkeypatch.setattr(resolver.subprocess, "run", decoding_failure)

    assert resolver.derive_project_dataset(str(workspace)).startswith("project_plain_")
    assert invocation["errors"] == "strict"


def test_invalid_workspace_returns_none(suite, isolated_modules, tmp_path):
    resolver = isolated_modules(suite, "_project_dataset")
    assert resolver.derive_project_dataset(str(tmp_path / "missing")) is None


def test_resolver_copies_are_byte_identical():
    root = Path(__file__).resolve().parents[3]
    claude = root / "claude-code" / "scripts" / "_project_dataset.py"
    codex = root / "codex" / "plugins" / "cognee" / "scripts" / "_project_dataset.py"
    assert claude.read_bytes() == codex.read_bytes()
