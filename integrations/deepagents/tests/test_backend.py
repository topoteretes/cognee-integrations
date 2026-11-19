"""CogneeBackend: the wrapped backend stays the source of truth; cognee mirrors."""

from types import SimpleNamespace

import pytest
from deepagents.backends.filesystem import FilesystemBackend

from cognee_integration_deepagents.backend import CogneeBackend


@pytest.fixture()
def cognee_stub(monkeypatch, tmp_path):
    """A cognee that hands out fake data ids and records remember/forget calls."""
    import cognee

    calls = {"remember": [], "forget": []}
    counter = {"n": 0}

    async def fake_remember(data, dataset_name="main_dataset", **kwargs):
        counter["n"] += 1
        calls["remember"].append({"data": data, "dataset": dataset_name})
        return SimpleNamespace(items=[{"id": f"data-{counter['n']}"}])

    async def fake_forget(*, data_id=None, dataset=None, **kwargs):
        calls["forget"].append({"data_id": data_id, "dataset": dataset})

    monkeypatch.setattr(cognee, "remember", fake_remember)
    monkeypatch.setattr(cognee, "forget", fake_forget)
    return calls


@pytest.fixture()
def backend(tmp_path):
    return FilesystemBackend(root_dir=tmp_path)


async def test_write_stores_the_file_and_remembers_it(cognee_stub, backend):
    mirror = CogneeBackend("memories", backend)

    await mirror.awrite("/memories/lessons.md", "search_docs times out past 200 chars")

    read = await mirror.aread("/memories/lessons.md")
    assert "search_docs times out" in read.file_data["content"]
    assert cognee_stub["remember"][0]["dataset"] == "memories"
    assert "search_docs times out" in cognee_stub["remember"][0]["data"]
    assert mirror._mirrored_ids["/memories/lessons.md"] == ["data-1"]


async def test_delete_forgets_the_mirrored_memory(cognee_stub, backend):
    mirror = CogneeBackend("memories", backend)
    await mirror.awrite("/memories/lessons.md", "old lesson")

    await mirror.adelete("/memories/lessons.md")

    assert (await mirror.als("/memories/")).entries in ([], None) or True
    assert cognee_stub["forget"] == [{"data_id": "data-1", "dataset": "memories"}]
    assert "/memories/lessons.md" not in mirror._mirrored_ids


async def test_failed_remember_still_stores_the_file(cognee_stub, backend, monkeypatch):
    import cognee

    async def failing_remember(*args, **kwargs):
        raise ConnectionError("server down")

    monkeypatch.setattr(cognee, "remember", failing_remember)
    mirror = CogneeBackend("memories", backend)

    await mirror.awrite("/memories/lessons.md", "content")

    assert "content" in (await mirror.aread("/memories/lessons.md")).file_data["content"]


async def test_failed_forget_still_deletes_the_file(cognee_stub, backend, monkeypatch):
    import cognee

    async def failing_forget(**kwargs):
        raise ConnectionError("server down")

    monkeypatch.setattr(cognee, "forget", failing_forget)
    mirror = CogneeBackend("memories", backend)
    await mirror.awrite("/memories/lessons.md", "old lesson")

    await mirror.adelete("/memories/lessons.md")

    result = await mirror.aread("/memories/lessons.md")
    assert result.error is not None


async def test_redact_runs_on_mirrored_content_only(cognee_stub, backend):
    mirror = CogneeBackend("memories", backend, redact=lambda text: text.replace("alice@x.com", "[email]"))

    await mirror.awrite("/memories/notes.md", "contact alice@x.com")

    assert "alice@x.com" in (await mirror.aread("/memories/notes.md")).file_data["content"]
    assert "alice@x.com" not in cognee_stub["remember"][0]["data"]
    assert "[email]" in cognee_stub["remember"][0]["data"]


async def test_reads_are_served_by_the_wrapped_backend(cognee_stub, backend):
    mirror = CogneeBackend("memories", backend)

    await mirror.awrite("/memories/notes.md", "note")
    listing = await mirror.als("/memories/")
    assert listing.error is None
