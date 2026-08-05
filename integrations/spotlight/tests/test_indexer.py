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
