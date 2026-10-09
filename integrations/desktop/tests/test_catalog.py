from desktop_backend.catalog import Catalog, _name_score


def make_catalog(tmp_path):
    return Catalog(tmp_path / "catalog.json")


def test_name_scoring_order():
    assert _name_score("plan.md", "plan") == 100.0
    assert _name_score("planning.md", "plan") == 80.0
    assert _name_score("project-plan.md", "proj plan") == 70.0
    assert _name_score("my-plan-notes.md", "plan") == 70.0  # word-start match
    assert _name_score("airplane.md", "plan") == 60.0
    assert _name_score("p-l-a-n.md", "plan") == 40.0
    assert _name_score("unrelated.md", "plan") == 0.0


def test_match_names_ranks_exact_over_substring(tmp_path):
    catalog = make_catalog(tmp_path)
    catalog.upsert("/docs/plan.md", 1.0, 10)
    catalog.upsert("/docs/my-plan-notes.md", 2.0, 10)
    hits = catalog.match_names("plan")
    assert [h["path"] for h in hits] == ["/docs/plan.md", "/docs/my-plan-notes.md"]


def test_persistence_roundtrip(tmp_path):
    catalog = make_catalog(tmp_path)
    catalog.upsert("/docs/a.md", 1.0, 10)
    catalog.add_roots(["/docs"])
    catalog.save()

    reloaded = make_catalog(tmp_path)
    assert len(reloaded) == 1
    assert reloaded.roots == ["/docs"]
    assert not reloaded.needs_index("/docs/a.md", 1.0)
    assert reloaded.needs_index("/docs/a.md", 2.0)


def test_find_by_basename(tmp_path):
    catalog = make_catalog(tmp_path)
    catalog.upsert("/docs/Notes.md", 1.0, 10)
    assert catalog.find_by_basename("notes.md") == "/docs/Notes.md"
    assert catalog.find_by_basename("notes") == "/docs/Notes.md"
    assert catalog.find_by_basename("other.md") is None
