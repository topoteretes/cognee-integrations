"""Keep the docs corpus in step with a GitHub repository.

GitHub sends a push webhook to ``POST /api/github/webhook``; this module turns
that push into corpus changes. A page added or edited on the tracked branch is
fetched at the pushed commit and replaces the item ingested from the same path;
a page deleted there is deleted from the corpus.

Replacing has to be done here. cognee deduplicates by content, so uploading an
edited page adds a second item next to the old one rather than updating it, and
both go on answering. The old item is found by the path ingest recorded on it
(``widget_path``) or, for items ingested before that, by its name.

The order is upload first, delete after. Deleting first would leave the page
missing from the corpus if the upload were refused; this way a failure leaves
the old version in place, which is stale but still answers.

The webhook is authenticated by GitHub's HMAC signature over the raw body, with
a secret shared between the webhook settings and ``WIDGET_GITHUB_WEBHOOK_SECRET``.
Unset, the route does not exist.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional
from urllib.parse import quote

from .docs_drift import content_digest, recorded_path, stored_digest
from .docs_ingest import item_name, render_for_ingest, safe_relative, upload_filename

DEFAULT_EXTENSIONS = (".md", ".mdx")

# Same bound as a dashboard ingest puts on one file.
MAX_FILE_CHARS = 2_000_000

# Fetches and uploads within one push go out together, as a dashboard ingest's do.
SYNC_CONCURRENCY = 4


@dataclass(frozen=True)
class GitHubSyncSettings:
    """What the webhook accepts and where it reads from. See ``from_env``."""

    secret: str
    token: Optional[str] = None
    # Folder inside the repository the corpus mirrors; "" is the repository root.
    # Corpus paths are relative to it, as they are to the folder a dashboard
    # ingest picks.
    docs_dir: str = ""
    # None follows the repository's default branch as each push reports it.
    branch: Optional[str] = None
    # "owner/name"; None accepts a push from any repository holding the secret.
    repo: Optional[str] = None
    extensions: tuple = DEFAULT_EXTENSIONS
    # Node-set root for pages the corpus does not hold yet. None uses the
    # folder's own name, which is what a dashboard ingest of it records.
    root: Optional[str] = None
    api_url: str = "https://api.github.com"

    @classmethod
    def from_env(cls) -> Optional["GitHubSyncSettings"]:
        """Settings from the environment, or ``None`` when the webhook is off."""
        secret = os.getenv("WIDGET_GITHUB_WEBHOOK_SECRET", "").strip()
        if not secret:
            return None
        extensions = tuple(
            e.strip().lower() if e.strip().startswith(".") else "." + e.strip().lower()
            for e in os.getenv("WIDGET_GITHUB_EXTENSIONS", ",".join(DEFAULT_EXTENSIONS)).split(",")
            if e.strip()
        )
        return cls(
            secret=secret,
            token=os.getenv("WIDGET_GITHUB_TOKEN", "").strip() or None,
            docs_dir=os.getenv("WIDGET_GITHUB_DOCS_DIR", "").strip().strip("/"),
            branch=os.getenv("WIDGET_GITHUB_BRANCH", "").strip() or None,
            repo=os.getenv("WIDGET_GITHUB_REPO", "").strip() or None,
            extensions=extensions or DEFAULT_EXTENSIONS,
            root=os.getenv("WIDGET_GITHUB_ROOT", "").strip().strip("/") or None,
            api_url=os.getenv("WIDGET_GITHUB_API_URL", "https://api.github.com")
            .strip()
            .rstrip("/"),
        )


def verify_signature(secret: str, body: bytes, header: Optional[str]) -> bool:
    """Whether ``header`` is GitHub's X-Hub-Signature-256 for ``body``."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[len("sha256=") :], expected)


@dataclass
class PushChanges:
    """What one push asks of the corpus. Paths are relative to the docs folder."""

    repo: str
    sha: str
    root: str
    upserts: list = field(default_factory=list)
    removals: list = field(default_factory=list)

    def repo_path(self, relative: str, docs_dir: str) -> str:
        return f"{docs_dir}/{relative}" if docs_dir else relative


def changes_from_push(
    payload: dict, settings: GitHubSyncSettings
) -> tuple[Optional[PushChanges], str]:
    """The corpus changes a push payload asks for, or ``None`` and why not.

    Commits are replayed in order, so a page edited and then deleted within one
    push ends up removed, and one deleted and then restored ends up re-fetched.
    A rename arrives as a removal of the old path and an addition of the new.
    """
    repository = payload.get("repository") or {}
    repo = str(repository.get("full_name") or "")
    if not repo:
        return None, "no repository in payload"
    if settings.repo and repo.lower() != settings.repo.lower():
        return None, f"repository {repo} is not {settings.repo}"

    branch = settings.branch or str(repository.get("default_branch") or "")
    if payload.get("ref") != f"refs/heads/{branch}":
        return None, f"push to {payload.get('ref')}, not refs/heads/{branch}"
    if payload.get("deleted"):
        return None, "branch deleted"

    sha = str(payload.get("after") or "")
    if not sha:
        return None, "no commit in payload"

    prefix = settings.docs_dir + "/" if settings.docs_dir else ""
    final: dict[str, str] = {}
    commits = payload.get("commits") or [payload.get("head_commit") or {}]
    for commit in commits:
        if not isinstance(commit, dict):
            continue
        for key, action in (("removed", "remove"), ("added", "upsert"), ("modified", "upsert")):
            for path in commit.get(key) or []:
                path = str(path)
                if not path.startswith(prefix) or not path.lower().endswith(settings.extensions):
                    continue
                relative = safe_relative(path[len(prefix) :])
                if relative:
                    final[relative] = action

    root = settings.root or (
        settings.docs_dir.rsplit("/", 1)[-1] if settings.docs_dir else repo.rsplit("/", 1)[-1]
    )
    changes = PushChanges(repo=repo, sha=sha, root=root)
    for relative, action in sorted(final.items()):
        (changes.upserts if action == "upsert" else changes.removals).append(relative)
    if not changes.upserts and not changes.removals:
        return None, "no documentation files changed"
    return changes, ""


async def fetch_file(settings: GitHubSyncSettings, repo: str, path: str, ref: str) -> Optional[str]:
    """A file's text at commit ``ref``, or ``None`` when it is not there.

    Read at the pushed commit rather than the branch head, so a later push
    landing first cannot hand this one newer content than it describes.
    """
    import httpx

    headers = {
        "Accept": "application/vnd.github.raw+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if settings.token:
        headers["Authorization"] = f"Bearer {settings.token}"
    url = f"{settings.api_url}/repos/{repo}/contents/{quote(path)}"
    async with httpx.AsyncClient(timeout=30.0) as http:
        response = await http.get(url, params={"ref": ref}, headers=headers)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.content.decode("utf-8", errors="replace")


def _items_for(items: list, relative: str) -> list:
    """The corpus items ingested from ``relative``.

    The recorded path is authoritative; the name is the fallback for items
    ingested before it was recorded, and only for those - otherwise ``a.md`` and
    ``a.mdx``, which share a name, would each claim the other's item.
    """
    name = item_name(relative)
    found = []
    for item in items:
        if not isinstance(item, dict):
            continue
        path = recorded_path(item)
        if path == relative or (path is None and str(item.get("name") or "") == name):
            found.append(item)
    return found


async def apply_push(
    changes: PushChanges,
    *,
    client,
    dataset_name: str,
    fetch: Callable[[str], Awaitable[Optional[str]]],
    node_set_for: Callable[[str, str], str],
    docs_url: Optional[str],
) -> dict:
    """Bring the corpus in line with one push and report what was done.

    ``fetch`` takes a path relative to the docs folder. Every outcome is
    reported per path, so a partial failure says which pages are stale.
    """
    dataset_id = await client.ensure_dataset(dataset_name)
    if not dataset_id:
        return {"sha": changes.sha, "error": f"cognee refused to create or find {dataset_name}"}
    items = await client.dataset_data(dataset_id)

    updated, unchanged, removed, skipped, failed = [], [], [], [], []
    limit = asyncio.Semaphore(SYNC_CONCURRENCY)

    async def delete(stale: list) -> list:
        """Delete ``stale``; the ids cognee refused."""
        refused = []
        for item in stale:
            if not await client.delete_data(dataset_id=dataset_id, data_id=str(item.get("id"))):
                refused.append(str(item.get("id")))
        return refused

    async def upsert(relative: str) -> None:
        async with limit:
            try:
                source = await fetch(relative)
            except Exception as error:  # noqa: BLE001 - reported per path
                failed.append({"path": relative, "why": f"could not fetch from GitHub: {error}"})
                return
            if source is None:
                skipped.append({"path": relative, "why": "not in the repository at this commit"})
                return
            if len(source) > MAX_FILE_CHARS or "\x00" in source or not source.strip():
                skipped.append({"path": relative, "why": "empty, binary or too large"})
                return

            text = render_for_ingest(source, relative, docs_url)
            digest = content_digest(text)
            existing = _items_for(items, relative)
            current = [i for i in existing if stored_digest(i) == digest]
            # An item with no readable digest may hold this very content, and
            # cognee would deduplicate the upload into it - deleting it would
            # then delete the new version. Left alone and reported instead.
            unverifiable = [i for i in existing if stored_digest(i) is None]
            stale = [i for i in existing if i not in current and i not in unverifiable]

            if current:
                refused = await delete(stale)
                unchanged.append({"path": relative, "stale_removed": len(stale) - len(refused)})
                return

            root = next(
                (
                    str((i.get("externalMetadata") or {}).get("widget_root"))
                    for i in existing
                    if isinstance(i.get("externalMetadata"), dict)
                    and (i.get("externalMetadata") or {}).get("widget_root")
                ),
                changes.root,
            )
            ok = await client.remember_background(
                text.encode("utf-8"),
                dataset_name=dataset_name,
                filename=upload_filename(relative),
                node_set=[node_set_for(relative, root)],
                external_metadata={
                    "widget_path": relative,
                    "widget_root": root,
                    "github_repo": changes.repo,
                    "github_sha": changes.sha,
                },
            )
            if not ok:
                failed.append(
                    {"path": relative, "why": "cognee refused the upload; old version kept"}
                )
                return
            refused = await delete(stale)
            entry = {"path": relative, "replaced": len(stale) - len(refused)}
            if refused:
                entry["not_deleted"] = refused
            if unverifiable:
                entry["left_in_place"] = [str(i.get("id")) for i in unverifiable]
            updated.append(entry)

    async def remove(relative: str) -> None:
        existing = _items_for(items, relative)
        if not existing:
            skipped.append({"path": relative, "why": "not in the corpus"})
            return
        refused = await delete(existing)
        if refused:
            failed.append({"path": relative, "why": "cognee refused the delete", "ids": refused})
        else:
            removed.append({"path": relative, "deleted": len(existing)})

    await asyncio.gather(*(upsert(r) for r in changes.upserts))
    for relative in changes.removals:
        await remove(relative)

    return {
        "repo": changes.repo,
        "sha": changes.sha,
        "updated": updated,
        "unchanged": unchanged,
        "removed": removed,
        "skipped": skipped,
        "failed": failed,
    }
