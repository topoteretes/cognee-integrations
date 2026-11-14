"""The GitHub push webhook: signature, what a push asks for, and how the corpus
is brought in line with it.

The ordering in ``apply_push`` is the part worth guarding: cognee deduplicates
by content, so an edit has to be uploaded and the old item deleted, in that
order, or a refused upload loses the page.
"""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from cognee_integration_web_widget.docs_drift import content_digest
from cognee_integration_web_widget.docs_ingest import render_for_ingest
from cognee_integration_web_widget.github_sync import (
    GitHubSyncSettings,
    PushChanges,
    apply_push,
    changes_from_push,
    verify_signature,
)

SECRET = "hook-secret"
DOCS_URL = "https://docs.example.com"


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def push(*commits, ref="refs/heads/main", repo="acme/docs", **extra):
    return {
        "ref": ref,
        "after": "abc1234def",
        "repository": {"full_name": repo, "default_branch": "main"},
        "commits": list(commits),
        **extra,
    }


def commit(added=(), modified=(), removed=()):
    return {"added": list(added), "modified": list(modified), "removed": list(removed)}


def stored_item(item_id, path, text, root="docs"):
    """A corpus item as cognee lists it, holding ``text`` ingested from ``path``."""
    return {
        "id": item_id,
        "name": path.rsplit(".", 1)[0].replace("/", "__"),
        "rawDataLocation": f"s3://bucket/text_{content_digest(text)}.txt",
        "externalMetadata": {"widget_path": path, "widget_root": root},
    }


# -- signature ------------------------------------------------------------------


def test_signature_accepts_githubs_hmac():
    assert verify_signature(SECRET, b"{}", sign(b"{}"))


@pytest.mark.parametrize(
    "header", [None, "", "sha1=abc", sign(b"{}", "other-secret"), sign(b"{ }")]
)
def test_signature_refuses_anything_else(header):
    assert not verify_signature(SECRET, b"{}", header)


# -- what a push asks for -------------------------------------------------------


def test_push_collects_added_modified_and_removed_docs():
    settings = GitHubSyncSettings(secret=SECRET)
    changes, _ = changes_from_push(
        push(commit(added=["guides/new.mdx"], modified=["index.md"], removed=["old.mdx"])), settings
    )
    assert changes.upserts == ["guides/new.mdx", "index.md"]
    assert changes.removals == ["old.mdx"]
    assert changes.sha == "abc1234def"
    # No docs folder: the root is the repository's own name, as a dashboard
    # ingest of a clone of it would record.
    assert changes.root == "docs"


def test_push_replays_commits_in_order():
    settings = GitHubSyncSettings(secret=SECRET)
    changes, _ = changes_from_push(
        push(
            commit(modified=["a.md", "b.md"]),
            commit(removed=["a.md", "b.md"]),
            commit(added=["b.md"]),
        ),
        settings,
    )
    assert changes.upserts == ["b.md"]
    assert changes.removals == ["a.md"]


def test_push_keeps_only_docs_under_the_docs_folder():
    settings = GitHubSyncSettings(secret=SECRET, docs_dir="site/docs")
    changes, _ = changes_from_push(
        push(
            commit(
                modified=[
                    "site/docs/guide.mdx",
                    "site/docs/api/ref.md",
                    "site/docs/logo.png",
                    "README.md",
                    "site/docs-old/x.md",
                ]
            )
        ),
        settings,
    )
    # Relative to the docs folder, as a dashboard ingest of that folder names them.
    assert changes.upserts == ["api/ref.md", "guide.mdx"]
    assert changes.root == "docs"


def test_push_extensions_are_configurable():
    settings = GitHubSyncSettings(secret=SECRET, extensions=(".txt",))
    changes, _ = changes_from_push(push(commit(modified=["a.md", "notes.TXT"])), settings)
    assert changes.upserts == ["notes.TXT"]


def test_push_refuses_paths_that_escape_the_docs_folder():
    settings = GitHubSyncSettings(secret=SECRET, docs_dir="docs")
    reason = changes_from_push(push(commit(modified=["docs/../secrets.md"])), settings)[1]
    assert reason == "no documentation files changed"


@pytest.mark.parametrize(
    "payload, settings, reason",
    [
        (push(commit(modified=["a.md"]), ref="refs/heads/feature"), {}, "not refs/heads/main"),
        (push(commit(modified=["a.md"])), {"branch": "release"}, "not refs/heads/release"),
        (push(commit(modified=["a.md"]), deleted=True), {}, "branch deleted"),
        (push(commit(modified=["a.md"])), {"repo": "acme/other"}, "is not acme/other"),
        (push(commit(modified=["src/app.py"])), {}, "no documentation files changed"),
    ],
)
def test_push_is_ignored_with_a_reason(payload, settings, reason):
    changes, why = changes_from_push(payload, GitHubSyncSettings(secret=SECRET, **settings))
    assert changes is None
    assert reason in why


# -- applying a push ------------------------------------------------------------


@pytest.fixture
def corpus():
    client = AsyncMock()
    client.ensure_dataset = AsyncMock(return_value="d1")
    client.dataset_data = AsyncMock(return_value=[])
    client.remember_background = AsyncMock(return_value=True)
    client.delete_data = AsyncMock(return_value=True)
    return client


async def run(client, changes, files):
    async def fetch(relative):
        return files.get(relative)

    return await apply_push(
        changes,
        client=client,
        dataset_name="web:demo:docs",
        fetch=fetch,
        node_set_for=lambda relative, root: f"{root}/{relative.rsplit('/', 1)[0]}",
        docs_url=DOCS_URL,
    )


async def test_an_edited_page_is_uploaded_then_its_old_item_deleted(corpus):
    old = render_for_ingest("# Old\n", "guides/a.md", DOCS_URL)
    corpus.dataset_data.return_value = [stored_item("old-1", "guides/a.md", old, root="mydocs")]
    calls = []
    corpus.remember_background.side_effect = lambda *a, **k: calls.append("upload") or True
    corpus.delete_data.side_effect = lambda **k: calls.append("delete") or True

    changes = PushChanges(repo="acme/docs", sha="s1", root="docs", upserts=["guides/a.md"])
    result = await run(corpus, changes, {"guides/a.md": "# New\n"})

    assert calls == ["upload", "delete"]
    assert result["updated"] == [{"path": "guides/a.md", "replaced": 1}]
    assert corpus.delete_data.await_args.kwargs == {"dataset_id": "d1", "data_id": "old-1"}
    args, kw = corpus.remember_background.await_args
    assert args[0] == render_for_ingest("# New\n", "guides/a.md", DOCS_URL).encode()
    assert kw["filename"] == "guides__a.md"
    # The existing item's root wins over the push default, so the page keeps
    # the node set it was recalled under.
    assert kw["node_set"] == ["mydocs/guides"]
    assert kw["external_metadata"]["widget_path"] == "guides/a.md"
    assert kw["external_metadata"]["github_sha"] == "s1"


async def test_a_refused_upload_keeps_the_old_version(corpus):
    old = render_for_ingest("# Old\n", "a.md", DOCS_URL)
    corpus.dataset_data.return_value = [stored_item("old-1", "a.md", old)]
    corpus.remember_background.return_value = False

    changes = PushChanges(repo="acme/docs", sha="s1", root="docs", upserts=["a.md"])
    result = await run(corpus, changes, {"a.md": "# New\n"})

    corpus.delete_data.assert_not_awaited()
    assert result["failed"][0]["path"] == "a.md"


async def test_an_unchanged_page_is_not_uploaded(corpus):
    text = render_for_ingest("# Same\n", "a.md", DOCS_URL)
    corpus.dataset_data.return_value = [stored_item("cur", "a.md", text)]

    changes = PushChanges(repo="acme/docs", sha="s1", root="docs", upserts=["a.md"])
    result = await run(corpus, changes, {"a.md": "# Same\n"})

    corpus.remember_background.assert_not_awaited()
    corpus.delete_data.assert_not_awaited()
    assert result["unchanged"] == [{"path": "a.md", "stale_removed": 0}]


async def test_an_unchanged_page_still_clears_older_duplicates(corpus):
    """The state an edit made before this webhook existed leaves behind."""
    text = render_for_ingest("# Same\n", "a.md", DOCS_URL)
    corpus.dataset_data.return_value = [
        stored_item("cur", "a.md", text),
        stored_item("older", "a.md", "something else"),
    ]
    changes = PushChanges(repo="acme/docs", sha="s1", root="docs", upserts=["a.md"])
    result = await run(corpus, changes, {"a.md": "# Same\n"})

    corpus.remember_background.assert_not_awaited()
    assert corpus.delete_data.await_args.kwargs["data_id"] == "older"
    assert result["unchanged"] == [{"path": "a.md", "stale_removed": 1}]


async def test_an_item_without_a_readable_digest_is_never_deleted(corpus):
    """cognee may have deduplicated the upload into it; deleting would lose the page."""
    item = stored_item("legacy", "a.md", "x")
    item["rawDataLocation"] = "s3://bucket/somewhere-else.txt"
    corpus.dataset_data.return_value = [item]

    changes = PushChanges(repo="acme/docs", sha="s1", root="docs", upserts=["a.md"])
    result = await run(corpus, changes, {"a.md": "# New\n"})

    corpus.remember_background.assert_awaited_once()
    corpus.delete_data.assert_not_awaited()
    assert result["updated"] == [{"path": "a.md", "replaced": 0, "left_in_place": ["legacy"]}]


async def test_a_new_page_is_added_under_the_push_root(corpus):
    changes = PushChanges(repo="acme/docs", sha="s1", root="docs", upserts=["guides/b.mdx"])
    result = await run(corpus, changes, {"guides/b.mdx": "---\ntitle: B\n---\nBody\n"})

    assert result["updated"] == [{"path": "guides/b.mdx", "replaced": 0}]
    assert corpus.remember_background.await_args.kwargs["node_set"] == ["docs/guides"]


async def test_items_from_before_paths_were_recorded_match_by_name(corpus):
    old = {
        "id": "legacy",
        "name": "guides__a",
        "rawDataLocation": "s3://b/text_" + "0" * 32 + ".txt",
    }
    other = stored_item("mdx", "guides/a.mdx", "x")  # same name, different recorded path
    corpus.dataset_data.return_value = [old, other]

    changes = PushChanges(repo="acme/docs", sha="s1", root="docs", upserts=["guides/a.md"])
    await run(corpus, changes, {"guides/a.md": "# New\n"})

    assert [c.kwargs["data_id"] for c in corpus.delete_data.await_args_list] == ["legacy"]


async def test_a_removed_page_is_deleted_from_the_corpus(corpus):
    corpus.dataset_data.return_value = [stored_item("gone", "old.md", "x")]
    changes = PushChanges(repo="acme/docs", sha="s1", root="docs", removals=["old.md", "never.md"])
    result = await run(corpus, changes, {})

    assert corpus.delete_data.await_args.kwargs["data_id"] == "gone"
    assert result["removed"] == [{"path": "old.md", "deleted": 1}]
    assert result["skipped"] == [{"path": "never.md", "why": "not in the corpus"}]


async def test_a_page_missing_at_the_commit_is_skipped_not_deleted(corpus):
    corpus.dataset_data.return_value = [stored_item("keep", "a.md", "x")]
    changes = PushChanges(repo="acme/docs", sha="s1", root="docs", upserts=["a.md"])
    result = await run(corpus, changes, {})

    corpus.delete_data.assert_not_awaited()
    assert result["skipped"][0]["path"] == "a.md"


async def test_a_fetch_failure_is_reported_per_page(corpus):
    async def fetch(relative):
        raise RuntimeError("rate limited")

    changes = PushChanges(repo="acme/docs", sha="s1", root="docs", upserts=["a.md"])
    result = await apply_push(
        changes,
        client=corpus,
        dataset_name="web:demo:docs",
        fetch=fetch,
        node_set_for=lambda r, root: root,
        docs_url=DOCS_URL,
    )
    assert "rate limited" in result["failed"][0]["why"]
    corpus.remember_background.assert_not_awaited()


# -- the route ------------------------------------------------------------------


@pytest.fixture
def hook_client(fake_client, monkeypatch):
    from cognee_integration_web_widget import server as server_mod
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server_mod, "GITHUB_SYNC", GitHubSyncSettings(secret=SECRET))
    fake_client.ensure_dataset = AsyncMock(return_value="d1")
    fake_client.dataset_data = AsyncMock(return_value=[])
    fake_client.remember_background = AsyncMock(return_value=True)
    fake_client.delete_data = AsyncMock(return_value=True)
    fetched = []

    async def fake_fetch(settings, repo, path, ref):
        fetched.append((repo, path, ref))
        return "# Page\n"

    monkeypatch.setattr(server_mod, "fetch_file", fake_fetch)
    server_mod.adapter.client = fake_client
    server_mod._github_last.clear()
    with TestClient(server_mod.app) as c:
        yield c, fake_client, fetched


def deliver(client, payload, event="push", secret=SECRET, form=False):
    if form:
        body = urlencode({"payload": json.dumps(payload)}).encode()
        content_type = "application/x-www-form-urlencoded"
    else:
        body = json.dumps(payload).encode()
        content_type = "application/json"
    return client.post(
        "/api/github/webhook",
        content=body,
        headers={
            "content-type": content_type,
            "x-github-event": event,
            "x-hub-signature-256": sign(body, secret),
        },
    )


def test_webhook_404s_when_no_secret_is_configured(hook_client, monkeypatch):
    from cognee_integration_web_widget import server as server_mod

    client, _, _ = hook_client
    monkeypatch.setattr(server_mod, "GITHUB_SYNC", None)
    assert deliver(client, push()).status_code == 404


def test_webhook_rejects_a_bad_signature(hook_client):
    client, fake, _ = hook_client
    r = deliver(client, push(commit(modified=["a.md"])), secret="wrong")
    assert r.status_code == 401
    fake.remember_background.assert_not_awaited()


def test_webhook_answers_ping(hook_client):
    client, _, _ = hook_client
    assert deliver(client, {"zen": "hi"}, event="ping").json() == {"pong": True}


def test_webhook_ignores_other_events(hook_client):
    client, fake, _ = hook_client
    assert deliver(client, {}, event="issues").json() == {"ignored": "issues event"}
    fake.ensure_dataset.assert_not_awaited()


def test_webhook_applies_a_push_and_reports_it_on_the_dashboard(hook_client, monkeypatch):
    from cognee_integration_web_widget import server as server_mod

    client, fake, fetched = hook_client
    r = deliver(client, push(commit(modified=["a.md"])))
    assert r.status_code == 202
    assert r.json() == {"sha": "abc1234def", "upserts": ["a.md"], "removals": []}
    # Read at the pushed commit, not the branch head.
    assert fetched == [("acme/docs", "a.md", "abc1234def")]
    assert fake.remember_background.await_args.kwargs["dataset_name"] == "web:demo:docs"
    assert server_mod._github_last["updated"] == [{"path": "a.md", "replaced": 0}]


def test_webhook_fetches_from_inside_the_docs_folder(hook_client, monkeypatch):
    from cognee_integration_web_widget import server as server_mod

    client, _, fetched = hook_client
    monkeypatch.setattr(
        server_mod, "GITHUB_SYNC", GitHubSyncSettings(secret=SECRET, docs_dir="docs")
    )
    deliver(client, push(commit(modified=["docs/a.md"])))
    assert fetched == [("acme/docs", "docs/a.md", "abc1234def")]


def test_webhook_accepts_the_form_encoded_content_type(hook_client):
    client, fake, _ = hook_client
    r = deliver(client, push(commit(added=["a.md"])), form=True)
    assert r.status_code == 202
    fake.remember_background.assert_awaited_once()


def test_webhook_says_why_a_push_was_ignored(hook_client):
    client, fake, _ = hook_client
    r = deliver(client, push(commit(modified=["a.md"]), ref="refs/heads/dev"))
    assert r.status_code == 200
    assert "not refs/heads/main" in r.json()["ignored"]
    fake.remember_background.assert_not_awaited()
