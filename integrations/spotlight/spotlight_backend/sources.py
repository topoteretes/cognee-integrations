"""Data sources: memory that fills itself.

A source is anything that can be synced into the index on a schedule. Three
ship here:

- :class:`LocalFolderSource` — the indexed roots re-sync automatically, so a
  file saved into a watched folder becomes searchable without touching the
  app (the indexer is incremental: unchanged files cost nothing).
- :class:`SlackSource` — pulls channel history via the Slack Web API into
  markdown transcripts (one file per channel per sync window), which then
  index like any document. Enabled by ``SLACK_TOKEN`` + ``SLACK_CHANNELS``.
- :class:`GoogleDriveSource` — pulls text-exportable Drive files via REST
  (``GDRIVE_ACCESS_TOKEN``, optional ``GDRIVE_QUERY``). Google Docs export as
  plain text; binary formats download as-is for cognee's loaders.

Connectors write into ``<data_dir>/sources/<name>/`` and let the ordinary
indexing pipeline do the rest, so every source inherits incrementality,
catalog mapping, and openable results for free. The sync loop runs inside
the backend; ``/sources`` reports status.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import traceback
from pathlib import Path
from typing import Any, Optional


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-") or "item"


class LocalFolderSource:
    """The watched-folder source: whatever roots the catalog knows re-sync."""

    name = "folders"
    label = "Folders"
    icon = "folder"  # SF Symbol; the app renders whatever the backend reports

    def __init__(self, indexer: Any) -> None:
        self._indexer = indexer

    async def sync(self) -> str:
        if not self._indexer._catalog.roots:
            return "no folders indexed yet"
        started = self._indexer.start([])
        return "reindex started" if started else "indexer busy"


class SlackSource:
    """Channel history -> markdown transcripts -> the index.

    v1 pulls the most recent window per configured channel each sync; the
    file per (channel, day) is rewritten and the incremental indexer picks it
    up only when its content actually changed.
    """

    name = "slack"
    label = "Slack"
    icon = "bubble.left.and.bubble.right"

    def __init__(
        self, staging: Path, token: str, channels: list[str], *, client: Any = None
    ) -> None:
        self.staging = staging
        self.token = token
        self.channels = channels
        self._client = client  # injectable for tests

    async def sync(self) -> str:
        written = 0
        for channel in self.channels:
            messages = await self._history(channel)
            if not messages:
                continue
            by_day: dict[str, list[str]] = {}
            for message in messages:
                stamp = float(message.get("ts", 0) or 0)
                day = time.strftime("%Y-%m-%d", time.localtime(stamp))
                who = message.get("user") or message.get("username") or "someone"
                text = str(message.get("text", "")).strip()
                if text:
                    by_day.setdefault(day, []).append(f"- {who}: {text}")
            for day, lines in by_day.items():
                path = self.staging / f"slack-{_slug(channel)}-{day}.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"# Slack #{channel} — {day}\n\n" + "\n".join(lines) + "\n")
                written += 1
        return f"wrote {written} transcript file(s)"

    async def _history(self, channel: str) -> list[dict[str, Any]]:
        response = await self._get(
            "https://slack.com/api/conversations.history",
            params={"channel": channel, "limit": 200},
        )
        if response.status_code >= 400:
            return []
        data = response.json()
        return data.get("messages", []) if data.get("ok") else []

    async def _get(self, url: str, params: dict) -> Any:
        import httpx

        headers = {"Authorization": f"Bearer {self.token}"}
        if self._client is not None:
            return await self._client.request("GET", url, headers=headers, params=params)
        async with httpx.AsyncClient(timeout=60.0) as client:
            return await client.get(url, headers=headers, params=params)


class GoogleDriveSource:
    """Drive files -> local staging -> the index.

    Uses the plain REST API with a bearer token (OAuth or service account —
    however the token was minted). Google-native docs export as text; other
    files download raw for cognee's loaders.
    """

    name = "gdrive"
    label = "Drive"
    icon = "externaldrive"

    def __init__(self, staging: Path, token: str, query: str = "", *, client: Any = None) -> None:
        self.staging = staging
        self.token = token
        self.query = query or "trashed = false"
        self._client = client

    async def sync(self) -> str:
        listing = await self._get(
            "https://www.googleapis.com/drive/v3/files",
            params={"q": self.query, "pageSize": 50, "fields": "files(id,name,mimeType)"},
        )
        if listing.status_code >= 400:
            return f"listing failed (HTTP {listing.status_code})"
        files = listing.json().get("files", [])
        written = 0
        for f in files:
            file_id, name, mime = (
                f.get("id"),
                _slug(str(f.get("name", ""))),
                str(f.get("mimeType", "")),
            )
            if mime.startswith("application/vnd.google-apps"):
                content = await self._get(
                    f"https://www.googleapis.com/drive/v3/files/{file_id}/export",
                    params={"mimeType": "text/plain"},
                )
                suffix = ".txt"
            else:
                content = await self._get(
                    f"https://www.googleapis.com/drive/v3/files/{file_id}",
                    params={"alt": "media"},
                )
                suffix = Path(str(f.get("name", ""))).suffix or ".bin"
            if content.status_code >= 400:
                continue
            path = self.staging / f"gdrive-{name}{suffix}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(
                content.content if hasattr(content, "content") else content.text.encode()
            )
            written += 1
        return f"synced {written} file(s)"

    async def _get(self, url: str, params: dict) -> Any:
        import httpx

        headers = {"Authorization": f"Bearer {self.token}"}
        if self._client is not None:
            return await self._client.request("GET", url, headers=headers, params=params)
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            return await client.get(url, headers=headers, params=params)


class GitHubSource:
    """Repository knowledge -> markdown -> one dataset per repo.

    Ingests the discussion layer, not the code: issues (with recent
    comments), pull-request threads, and release notes — the "why" that
    never shows up in a working tree. Works with any ``owner/repo``;
    public repos need no token. Each repo lands in its own
    ``github-<owner>-<repo>`` dataset, so it appears as its own memory
    layer in the graph and in answer attribution.
    """

    name = "github"
    label = "GitHub"
    icon = "chevron.left.forwardslash.chevron.right"

    def __init__(self, staging: Path, token: str, repos: list[str], *, client: Any = None) -> None:
        self.staging = staging
        self.token = token
        self.repos = repos
        self._client = client  # injectable for tests

    @property
    def datasets(self) -> dict[str, str]:
        """Staging-path prefix -> dataset, one per repository."""
        return {str(self.staging / _slug(repo)): f"github-{_slug(repo)}" for repo in self.repos}

    async def sync(self) -> str:
        written = 0
        for repo in self.repos:
            written += await self._sync_repo(repo)
        return f"synced {written} file(s) from {len(self.repos)} repo(s)"

    async def _sync_repo(self, repo: str) -> int:
        base = f"https://api.github.com/repos/{repo}"
        dest = self.staging / _slug(repo)
        # three list calls per repo: issues+PRs, recent comments, releases
        issues = await self._get_json(
            f"{base}/issues",
            {"state": "all", "per_page": 50, "sort": "updated", "direction": "desc"},
        )
        comments = await self._get_json(
            f"{base}/issues/comments", {"per_page": 100, "sort": "updated", "direction": "desc"}
        )
        releases = await self._get_json(f"{base}/releases", {"per_page": 20})

        by_issue: dict[str, list[dict]] = {}
        for comment in comments:
            by_issue.setdefault(str(comment.get("issue_url", "")), []).append(comment)

        written = 0
        for issue in issues:
            number = issue.get("number", "")
            kind = "pr" if issue.get("pull_request") else "issue"
            lines = [f"# {repo} {kind} #{number}: {issue.get('title', '')}", ""]
            lines.append(f"- state: {issue.get('state', '')}")
            if author := (issue.get("user") or {}).get("login"):
                lines.append(f"- author: {author}")
            if labels := [
                lab.get("name", "") for lab in issue.get("labels", []) if isinstance(lab, dict)
            ]:
                lines.append(f"- labels: {', '.join(labels)}")
            lines += ["", str(issue.get("body") or "").strip()]
            for comment in by_issue.get(str(issue.get("url", "")), []):
                who = (comment.get("user") or {}).get("login", "someone")
                lines += ["", "---", f"{who} commented:", str(comment.get("body") or "").strip()]
            title_slug = _slug(str(issue.get("title", "")))[:60]
            written += self._write(dest / f"{kind}-{number}-{title_slug}.md", "\n".join(lines))
        if releases:
            lines = [f"# {repo} releases", ""]
            for release in releases:
                lines += [
                    f"## {release.get('tag_name', '')} — {release.get('name', '')}",
                    str(release.get("body") or "").strip(),
                    "",
                ]
            written += self._write(dest / "releases.md", "\n".join(lines))
        return written

    @staticmethod
    def _write(path: Path, content: str) -> int:
        content = content.rstrip() + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_text() == content:
            return 0
        path.write_text(content)
        return 1

    async def _get_json(self, url: str, params: dict) -> list[dict]:
        import httpx

        headers = {"Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self._client is not None:
            response = await self._client.request("GET", url, headers=headers, params=params)
        else:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                response = await client.get(url, headers=headers, params=params)
        if response.status_code >= 400:
            return []
        data = response.json()
        return data if isinstance(data, list) else []


class MockConnectorSource:
    """A demo stand-in for a real connector: stages canned fictional content
    into ``<data_dir>/sources/<name>/`` so the app shows a live connection
    (and its documents index and search) without any external credentials.

    Enabled with ``SPOTLIGHT_MOCK_SOURCES=slack,gdrive``. Content is written
    once per file; the incremental indexer ignores unchanged files after that.
    """

    def __init__(
        self,
        name: str,
        staging: Path,
        files: dict[str, str],
        label: str = "",
        icon: str = "",
        datasets: Optional[dict[str, str]] = None,
    ) -> None:
        self.name = name
        self.staging = staging
        self.files = files  # keys may contain subdirs, e.g. "repo/issue-1.md"
        self.label = label or name.title()
        self.icon = icon or "puzzlepiece.extension"
        # relative subdir -> dataset, resolved against staging like the real
        # connector's mapping (so mock GitHub lands in github-<repo> too)
        self.datasets = {str(staging / sub): ds for sub, ds in (datasets or {}).items()}

    async def sync(self) -> str:
        written = 0
        for filename, content in self.files.items():
            path = self.staging / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists() or path.read_text() != content:
                path.write_text(content)
                written += 1
        return f"connected (demo) — {len(self.files)} document(s), {written} refreshed"


# Fictional Meridian Travel Group content for the mock connectors — same
# invented company as the demo corpus, so nothing real can leak.
MOCK_SLACK_FILES = {
    "slack-product-2026-08-04.md": (
        "# Slack #product — 2026-08-04\n\n"
        "- alex.chen: unified search beta is live for 5% of traffic — "
        "meaning-based queries convert 12% better than keyword so far\n"
        "- sam.rivera: RoomRover cut prices again in the São Paulo market, "
        "sales wants the counter-offer playbook updated by Friday\n"
        "- alex.chen: supplier API v3 rate limits bite above 40 rps — "
        "batching fix is in review, ships with Thursday's deploy\n"
        "- jamie.park: StayFinder's new loyalty tiers are getting press; "
        "our v2 needs the referral hook to stay competitive\n"
    ),
    "slack-eng-incidents-2026-08-03.md": (
        "# Slack #eng-incidents — 2026-08-03\n\n"
        "- sam.rivera: search latency spiked to 4s after the cache change — "
        "rolled back, root cause was per-query session rewrites\n"
        "- alex.chen: postmortem action: keep response caching off for "
        "search-as-you-type paths, warm the engine at boot instead\n"
        "- jamie.park: added the regression check to the deploy runbook\n"
    ),
}

# One fictional repository for the GitHub mock — issues, a PR review thread,
# and release notes for Meridian's (invented) search platform.
MOCK_GITHUB_REPO = "meridian/search-platform"
MOCK_GITHUB_FILES = {
    "meridian-search-platform/issue-42-search-latency-spikes.md": (
        "# meridian/search-platform issue #42: Search latency spikes to 4s "
        "when session cache rewrites queries\n\n"
        "- state: closed\n- author: sam-rivera\n- labels: bug, performance\n\n"
        "Type-ahead searches jumped from 250ms to 4s after enabling the "
        "session cache. Every keystroke goes through an LLM query rewrite "
        "before the vector search.\n\n---\n"
        "alex-chen commented:\nRoot cause confirmed: the cache is built for "
        "chat agents, not search-as-you-type. Fix is to disable response "
        "caching on the type-ahead path and warm the engine at boot instead.\n"
        "\n---\n"
        "sam-rivera commented:\nShipped in v2.2.1 — latency back to 250ms. "
        "Added a regression check to the deploy runbook.\n"
    ),
    "meridian-search-platform/pr-57-batch-supplier-api-calls.md": (
        "# meridian/search-platform pr #57: Batch supplier API v3 calls to "
        "stay under rate limits\n\n"
        "- state: closed\n- author: alex-chen\n- labels: supplier-api\n\n"
        "Supplier API v3 throttles above 40 rps. This batches availability "
        "lookups into windows of 25 so peak-season traffic stays under the "
        "limit.\n\n---\n"
        "jamie-park commented:\nReview: ship it behind the flag until the "
        "peak-season load test passes — same rollout playbook as unified "
        "search.\n"
    ),
    "meridian-search-platform/issue-61-loyalty-referral-hook.md": (
        "# meridian/search-platform issue #61: Loyalty v2 referral hook must "
        "land before September\n\n"
        "- state: open\n- author: jamie-park\n- labels: loyalty\n\n"
        "StayFinder's loyalty tiers are getting press. Our v2 needs the "
        "referral hook in the September release or the board update slips.\n"
    ),
    "meridian-search-platform/releases.md": (
        "# meridian/search-platform releases\n\n"
        "## v2.3.0 — Unified search beta\n"
        "Meaning-based search behind a flag for 5% of traffic. Early data: "
        "12% better conversion than keyword.\n\n"
        "## v2.2.1 — Search latency hotfix\n"
        "Disabled per-query cache rewrites on type-ahead; engine warms at "
        "boot. Latency restored to 250ms.\n"
    ),
}

MOCK_GDRIVE_FILES = {
    "gdrive-board-update-q3-draft.txt": (
        "Meridian Travel Group — Q3 Board Update (draft)\n\n"
        "Growth: bookings up 18% QoQ, driven by the unified search beta.\n"
        "Competition: RoomRover discounting aggressively in Brazil; "
        "Wanderly retreating to premium segments.\n"
        "Risks: supplier API v3 migration must land before peak season; "
        "loyalty v2 slips if referral hook misses September.\n"
    ),
    "gdrive-partner-pricing-tiers.txt": (
        "Meridian partner pricing tiers (internal)\n\n"
        "Tier 1 (chains, >500 properties): 8% commission, dedicated support.\n"
        "Tier 2 (regional groups): 11% commission, quarterly reviews.\n"
        "Tier 3 (independents): 14% commission, self-serve onboarding.\n"
        "Price-match escalations go through the sales playbook, not ad hoc.\n"
    ),
}


class SourceManager:
    """Runs every configured source on an interval and indexes what lands."""

    def __init__(self, indexer: Any, data_dir: Path, interval: float = 60.0) -> None:
        self.indexer = indexer
        self.data_dir = data_dir
        self.interval = interval
        self.sources: list[Any] = []
        self.status: dict[str, dict[str, Any]] = {}
        self._task: Optional[asyncio.Task] = None

    @classmethod
    def from_env(cls, indexer: Any, data_dir: Path) -> "SourceManager":
        manager = cls(indexer, data_dir, interval=float(os.getenv("SPOTLIGHT_SYNC_INTERVAL", "60")))
        if os.getenv("SPOTLIGHT_WATCH_FOLDERS", "true").lower() != "false":
            manager.sources.append(LocalFolderSource(indexer))
        if token := os.getenv("SLACK_TOKEN"):
            channels = [c.strip() for c in os.getenv("SLACK_CHANNELS", "").split(",") if c.strip()]
            if channels:
                manager.sources.append(SlackSource(data_dir / "sources" / "slack", token, channels))
        if token := os.getenv("GDRIVE_ACCESS_TOKEN"):
            manager.sources.append(
                GoogleDriveSource(
                    data_dir / "sources" / "gdrive", token, os.getenv("GDRIVE_QUERY", "")
                )
            )
        if repos_env := os.getenv("GITHUB_REPOS"):
            repos = [r.strip() for r in repos_env.split(",") if r.strip()]
            if repos:
                manager.sources.append(
                    GitHubSource(
                        data_dir / "sources" / "github", os.getenv("GITHUB_TOKEN", ""), repos
                    )
                )
        # demo connections: mock connectors for sources with no credentials,
        # skipped for any name already covered by a real connector above.
        # Each mock borrows the real connector's label/icon (and, for GitHub,
        # its dataset-per-repo layout) so it renders identically in the app.
        mocks = {
            "slack": (MOCK_SLACK_FILES, SlackSource, {}),
            "gdrive": (MOCK_GDRIVE_FILES, GoogleDriveSource, {}),
            "github": (
                MOCK_GITHUB_FILES,
                GitHubSource,
                {_slug(MOCK_GITHUB_REPO): f"github-{_slug(MOCK_GITHUB_REPO)}"},
            ),
        }
        configured = {s.name for s in manager.sources}
        for name in (x.strip().lower() for x in os.getenv("SPOTLIGHT_MOCK_SOURCES", "").split(",")):
            if name in mocks and name not in configured:
                files, real, datasets = mocks[name]
                manager.sources.append(
                    MockConnectorSource(
                        name,
                        data_dir / "sources" / name,
                        files,
                        label=real.label,
                        icon=real.icon,
                        datasets=datasets,
                    )
                )
        return manager

    def start(self) -> None:
        if self.sources and self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def _loop(self) -> None:
        # connectors first, so their staged files are on disk before the
        # folder source triggers the (single-flight) indexer
        while True:
            for source in self.sources:
                if isinstance(source, LocalFolderSource):
                    continue
                await self._run(source)
            staged = [
                str(s.staging) for s in self.sources if hasattr(s, "staging") and s.staging.exists()
            ]
            if staged:
                # register per-source dataset routing (e.g. github-<repo>)
                # before the run so staged files land in the right dataset
                for source in self.sources:
                    if datasets := getattr(source, "datasets", None):
                        self.indexer.dataset_overrides.update(datasets)
                self.indexer.start(staged)
            for source in self.sources:
                if isinstance(source, LocalFolderSource):
                    await self._run(source)
            await asyncio.sleep(self.interval)

    async def _run(self, source: Any) -> None:
        try:
            detail = await source.sync()
            self.status[source.name] = {"ok": True, "detail": detail, "at": time.time()}
        except Exception as exc:
            traceback.print_exc()
            self.status[source.name] = {
                "ok": False,
                "detail": f"{type(exc).__name__}: {exc}",
                "at": time.time(),
            }
