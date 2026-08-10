from pathlib import Path

from spotlight_backend.config import Settings
from spotlight_backend.indexer import discover_files


def test_discover_filters(tmp_path):
    (tmp_path / "keep.md").write_text("hello")
    (tmp_path / "keep.py").write_text("print('hi')")
    (tmp_path / "skip.bin").write_text("nope")
    (tmp_path / ".hidden.md").write_text("nope")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.md").write_text("nope")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.txt").write_text("yes")
    big = tmp_path / "big.md"
    big.write_text("x" * 100)

    settings = Settings()
    settings.max_file_size = 50
    found = {p.name for p in discover_files([str(tmp_path)], settings)}
    assert found == {"keep.md", "keep.py", "nested.txt"}


def test_discover_accepts_single_file_and_dedupes(tmp_path):
    f = tmp_path / "note.md"
    f.write_text("hi")
    settings = Settings()
    found = discover_files([str(f), str(tmp_path)], settings)
    assert found == [Path(f)]


async def test_dataset_overrides_route_files_and_cognify(tmp_path):
    import asyncio

    from spotlight_backend.adapters import FakeAdapter
    from spotlight_backend.catalog import Catalog
    from spotlight_backend.indexer import Indexer

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "note.md").write_text("a plain note")
    repo = tmp_path / "sources" / "github" / "acme-rockets"
    repo.mkdir(parents=True)
    (repo / "issue-1.md").write_text("a repo issue")

    settings = Settings()
    settings.data_dir = tmp_path / "state"
    adapter = FakeAdapter(dataset="spotlight")
    indexer = Indexer(adapter, Catalog(settings.data_dir / "catalog.json"), settings)
    indexer.dataset_overrides[str(repo)] = "github-acme-rockets"

    async def run():
        indexer.start([str(docs), str(tmp_path / "sources" / "github")])
        for _ in range(200):
            if indexer.status["state"] in ("idle", "error"):
                break
            await asyncio.sleep(0.01)
        assert indexer.status["state"] == "idle", indexer.status

    await run()

    assert adapter.dataset_of[str(docs / "note.md")] == "spotlight"
    assert adapter.dataset_of[str(repo / "issue-1.md")] == "github-acme-rockets"
    # both datasets got their graphs built
    assert set(adapter.cognified_datasets) == {"spotlight", "github-acme-rockets"}


def test_walk_skips_symlinks_escaping_the_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("not yours to index")
    root = tmp_path / "watched"
    root.mkdir()
    (root / "mine.md").write_text("fine")
    (root / "escape").symlink_to(outside)
    (root / "escape.md").symlink_to(outside / "secret.md")
    # a symlink pointing back inside the tree is still fine
    (root / "alias.md").symlink_to(root / "mine.md")

    settings = Settings()
    found = {p.name for p in discover_files([str(root)], settings)}
    assert "mine.md" in found and "alias.md" in found
    assert "secret.md" not in found and "escape.md" not in found
