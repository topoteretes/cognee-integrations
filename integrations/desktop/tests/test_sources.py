"""Sources: connectors stage files, the ordinary index pipeline does the rest."""

import json

from desktop_backend.sources import GoogleDriveSource, SlackSource, SourceManager


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b""):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content or json.dumps(self._payload).encode()
        self.text = self.content.decode(errors="ignore")

    def json(self):
        return self._payload


class ScriptedClient:
    """Returns queued responses in order and records requests."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def request(self, method, url, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)


async def test_slack_source_writes_daily_transcripts(tmp_path):
    client = ScriptedClient(
        [
            FakeResponse(
                200,
                {
                    "ok": True,
                    "messages": [
                        {"ts": "1754400000.0", "user": "vasilije", "text": "migrations first"},
                        {"ts": "1754400060.0", "user": "boris", "text": "got it"},
                    ],
                },
            )
        ]
    )
    source = SlackSource(tmp_path / "slack", "xoxb-token", ["C123"], client=client)
    detail = await source.sync()
    assert "1 transcript" in detail
    files = list((tmp_path / "slack").glob("slack-c123-*.md"))
    assert len(files) == 1
    body = files[0].read_text()
    assert "vasilije: migrations first" in body and "boris: got it" in body
    assert client.requests[0]["headers"]["Authorization"] == "Bearer xoxb-token"


async def test_gdrive_source_exports_google_docs_as_text(tmp_path):
    client = ScriptedClient(
        [
            FakeResponse(
                200,
                {
                    "files": [
                        {
                            "id": "f1",
                            "name": "Q3 Plan",
                            "mimeType": "application/vnd.google-apps.document",
                        }
                    ]
                },
            ),
            FakeResponse(200, content=b"the plan is StayFinder defense"),
        ]
    )
    source = GoogleDriveSource(tmp_path / "gdrive", "ya29.token", client=client)
    detail = await source.sync()
    assert "synced 1" in detail
    staged = tmp_path / "gdrive" / "gdrive-q3-plan.txt"
    assert staged.read_text() == "the plan is StayFinder defense"
    assert "export" in client.requests[1]["url"]


def test_manager_from_env_configures_sources(tmp_path, monkeypatch):
    class DummyIndexer:
        class _catalog:
            roots = []

    monkeypatch.setenv("SLACK_TOKEN", "t")
    monkeypatch.setenv("SLACK_CHANNELS", "C1, C2")
    monkeypatch.setenv("GDRIVE_ACCESS_TOKEN", "g")
    manager = SourceManager.from_env(DummyIndexer(), tmp_path)
    names = [s.name for s in manager.sources]
    assert names == ["folders", "slack", "gdrive"]
    assert manager.sources[1].channels == ["C1", "C2"]


async def test_mock_connectors_stage_demo_content(tmp_path, monkeypatch):
    from desktop_backend.sources import MockConnectorSource

    class DummyIndexer:
        class _catalog:
            roots = []

    monkeypatch.setenv("COGNEE_DESKTOP_MOCK_SOURCES", "slack, gdrive")
    manager = SourceManager.from_env(DummyIndexer(), tmp_path)
    names = [s.name for s in manager.sources]
    assert names == ["folders", "slack", "gdrive"]

    slack = manager.sources[1]
    assert isinstance(slack, MockConnectorSource)
    detail = await slack.sync()
    assert "connected (demo)" in detail
    staged = list((tmp_path / "sources" / "slack").glob("*.md"))
    assert staged
    # second sync: content unchanged, nothing rewritten
    assert "0 refreshed" in await slack.sync()


def test_mock_connectors_yield_to_real_credentials(tmp_path, monkeypatch):
    from desktop_backend.sources import SlackSource

    class DummyIndexer:
        class _catalog:
            roots = []

    monkeypatch.setenv("SLACK_TOKEN", "t")
    monkeypatch.setenv("SLACK_CHANNELS", "C1")
    monkeypatch.setenv("COGNEE_DESKTOP_MOCK_SOURCES", "slack")
    manager = SourceManager.from_env(DummyIndexer(), tmp_path)
    slack = [s for s in manager.sources if s.name == "slack"]
    assert len(slack) == 1 and isinstance(slack[0], SlackSource)


async def test_github_source_writes_issue_threads_per_repo(tmp_path):
    from desktop_backend.sources import GitHubSource

    issue_url = "https://api.github.com/repos/acme/rockets/issues/7"
    client = ScriptedClient(
        [
            FakeResponse(
                200,
                [
                    {
                        "number": 7,
                        "url": issue_url,
                        "title": "Fuel gauge reads empty on full tank",
                        "state": "closed",
                        "user": {"login": "wile"},
                        "labels": [{"name": "bug"}],
                        "body": "Gauge inverted after sensor swap.",
                    },
                    {
                        "number": 9,
                        "url": issue_url.replace("/7", "/9"),
                        "title": "Add parachute deploy retries",
                        "state": "open",
                        "user": {"login": "road"},
                        "labels": [],
                        "body": "One attempt is not enough.",
                        "pull_request": {"url": "..."},
                    },
                ],
            ),
            FakeResponse(
                200,
                [
                    {
                        "issue_url": issue_url,
                        "user": {"login": "road"},
                        "body": "Confirmed: polarity flipped in the harness.",
                    }
                ],
            ),
            FakeResponse(200, [{"tag_name": "v1.2.0", "name": "Retry era", "body": "Retries!"}]),
        ]
    )
    source = GitHubSource(tmp_path / "github", "ghp-token", ["acme/rockets"], client=client)
    detail = await source.sync()
    assert "3 file(s)" in detail and "1 repo(s)" in detail

    repo_dir = tmp_path / "github" / "acme-rockets"
    issue = (repo_dir / "issue-7-fuel-gauge-reads-empty-on-full-tank.md").read_text()
    assert "state: closed" in issue and "author: wile" in issue
    assert "road commented:" in issue and "polarity flipped" in issue
    pr = (repo_dir / "pr-9-add-parachute-deploy-retries.md").read_text()
    assert "pr #9" in pr
    assert "v1.2.0" in (repo_dir / "releases.md").read_text()
    # per-repo dataset mapping, and the token went out as a bearer header
    assert source.datasets == {str(repo_dir): "github-acme-rockets"}
    assert client.requests[0]["headers"]["Authorization"] == "Bearer ghp-token"

    # second sync with identical content rewrites nothing
    client.responses = [
        FakeResponse(200, []),
        FakeResponse(200, []),
        FakeResponse(200, []),
    ]
    assert "0 file(s)" in await source.sync()


def test_mock_github_repo_gets_its_own_dataset(tmp_path, monkeypatch):
    from desktop_backend.sources import MockConnectorSource

    class DummyIndexer:
        class _catalog:
            roots = []

    monkeypatch.setenv("COGNEE_DESKTOP_MOCK_SOURCES", "github")
    manager = SourceManager.from_env(DummyIndexer(), tmp_path)
    github = next(s for s in manager.sources if s.name == "github")
    assert isinstance(github, MockConnectorSource)
    assert github.label == "GitHub"
    repo_prefix = str(tmp_path / "sources" / "github" / "meridian-search-platform")
    assert github.datasets == {repo_prefix: "github-meridian-search-platform"}


def test_mock_mobile_device_connection(tmp_path, monkeypatch):
    """Demo-only sources (no real connector class) declare label/icon inline."""

    class DummyIndexer:
        class _catalog:
            roots = []

    monkeypatch.setenv("COGNEE_DESKTOP_MOCK_SOURCES", "mobile")
    manager = SourceManager.from_env(DummyIndexer(), tmp_path)
    mobile = next(s for s in manager.sources if s.name == "mobile")
    assert mobile.label == "Mobile" and mobile.icon == "smartphone"
    assert mobile.scope == ["Pixel 8 Pro — quick captures"]
