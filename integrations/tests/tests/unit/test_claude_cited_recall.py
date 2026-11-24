"""Cited recall in the Claude Code plugin (SDK-1099).

A server that lists ``recall_cited_v1`` returns the regular ``only_context``
prompt with a tag line, ``[mem:<kind>/<dataset_id>/<record_id>@<version>]``,
before every passage, entity and fact, and the tags in ``metadata["records"]``.
The hook asks for it only when the cached ``/health`` lists the capability, keeps
every tag with its record through trimming, the budget and dedup, names the
record in the receipt, and ``cognee-search.sh --expand`` reads tags back.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

KEY = "cited-session"
PROMPT = "how does pinning a dataset work?"
DS = "5f0c0000-0000-4000-8000-000000000001"


def tag(kind: str, n: int, version: str = "a") -> str:
    return f"[mem:{kind}/{DS}/00000000-0000-4000-8000-{n:012d}@{version * 32}]"


LEARNING, TRANSCRIPT, QUOTING = tag("passage", 1), tag("passage", 2), tag("passage", 3)
TENANT, EMPTY = tag("entity", 4), tag("entity", 5)
PINNING, SWITCH = tag("fact", 6), tag("fact", 7)
#: A tag inside a captured answer's own text: not one the server listed.
FOREIGN = tag("passage", 99, "f")
RECORDS = [LEARNING, TRANSCRIPT, QUOTING, TENANT, EMPTY, PINNING, SWITCH]

PASSAGES = [
    (LEARNING, "# Session learning (session claude_1fb0)\n\nPinning keeps recall on one dataset."),
    (TRANSCRIPT, "Session ID: claude_other\nQuestion: is cognee working?\nAnswer: yes"),
    (QUOTING, f"Notes on citing\nAnswer: quoted\n---\n{FOREIGN}\nrest of the note"),
]
ENTITIES = [
    (TENANT, "### tenant server (software)\n- The tenant server runs cognee."),
    (EMPTY, "### empty node (concept)\n- empty node is a concept."),
]
FACTS = [
    (PINNING, "- Pinning keeps recall on the active dataset."),
    (SWITCH, "- Switching datasets moves later saves to the new dataset."),
]
CONFLICTS = "## Fact conflicts\n- The tenant runs 1.6.9 and 1.6.8."


def context(tagged: bool) -> str:
    def body(tag_, text):
        return f"{tag_}\n{text}" if tagged else text

    return "\n\n".join(
        [
            "## Relevant passages\n" + "\n---\n".join(body(t, p) for t, p in PASSAGES),
            "## Relevant entities\n" + "\n\n".join(body(t, e) for t, e in ENTITIES),
            "## Related facts\n" + "\n".join(body(t, f) for t, f in FACTS),
            CONFLICTS,
        ]
    )


def item(tagged: bool) -> dict:
    head = (
        f"The question is: `{PROMPT}`\n"
        "Answer using this sectioned context. Keep the answer brief and do not use "
        "information outside the context.\n\nContext:\n`"
    )
    value = {"source": "graph", "text": head + context(tagged) + "`", "dataset_name": "memory"}
    if tagged:
        value["metadata"] = {"records": [{"tag": t} for t in RECORDS]}
    return value


@pytest.fixture
def lookup(suite, hook_module, monkeypatch):
    if suite.name != "claude-code":
        pytest.skip("Claude Code cited recall")
    module = hook_module(suite, "session-context-lookup.py")
    monkeypatch.setattr(module, "get_session_key", lambda: KEY)
    return module


@pytest.fixture
def modules(suite, isolated_modules):
    if suite.name != "claude-code":
        pytest.skip("Claude Code cited recall")
    return lambda name: isolated_modules(suite, name)


def injected(run) -> str:
    return run.output["hookSpecificOutput"]["additionalContext"]


def cited(lookup, monkeypatch, recall=None):
    from utils.recall import drive_recall

    monkeypatch.setattr(lookup, "cited_supported", lambda url: True)
    return drive_recall(
        lookup, monkeypatch, recall=recall or {"graph": [item(True)]}, prompt=PROMPT
    )


def test_a_server_without_the_capability_gets_todays_single_request(lookup, monkeypatch):
    from utils.recall import drive_recall

    plain = drive_recall(lookup, monkeypatch, recall={"graph": [item(False)]}, prompt=PROMPT)
    assert plain.calls.count("graph") == 1
    assert "result_mode" not in plain.kwargs["graph"]
    assert not any(t in injected(plain) for t in RECORDS)
    monkeypatch.setattr(lookup, "cited_supported", lambda url: True)
    capable = drive_recall(lookup, monkeypatch, recall={"graph": [item(False)]}, prompt=PROMPT)
    assert capable.kwargs["graph"]["result_mode"] == "cited"


def test_untagged_output_is_unchanged_by_the_capability(suite, hook_module, monkeypatch, tmp_path):
    if suite.name != "claude-code":
        pytest.skip("Claude Code cited recall")
    from utils.recall import drive_recall

    outputs = []
    for supported in (False, True):
        module = hook_module(suite, "session-context-lookup.py")
        monkeypatch.setattr(module, "get_session_key", lambda: f"plain-{supported}")
        monkeypatch.setattr(module, "cited_supported", lambda url, s=supported: s)
        run = drive_recall(module, monkeypatch, recall={"graph": [item(False)]}, prompt=PROMPT)
        outputs.append(injected(run))
    assert outputs[0] == outputs[1]


def test_every_record_keeps_its_tag_and_no_tag_is_left_alone(lookup, monkeypatch):
    run = cited(lookup, monkeypatch)
    assert run.kwargs["graph"]["result_mode"] == "cited"
    assert all("result_mode" not in kw for scope, kw in run.kwargs.items() if scope != "graph")
    text = injected(run)
    for tag_, body in PASSAGES + ENTITIES[:1] + FACTS:
        assert f"{tag_}\n{body.splitlines()[0]}" in text, tag_
    # The entity pruned to nothing takes its tag with it.
    assert EMPTY not in text
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() in RECORDS:
            assert i + 1 < len(lines) and lines[i + 1].strip(), line
            assert lines[i + 1].strip() not in RECORDS


def test_a_tag_quoted_inside_a_passage_does_not_split_it(lookup, monkeypatch):
    text = injected(cited(lookup, monkeypatch))
    assert f"Answer: quoted\n---\n{FOREIGN}\nrest of the note" in text


def test_transcripts_still_go_to_their_own_section(lookup, monkeypatch):
    text = injected(cited(lookup, monkeypatch))
    heading = text.index("## Passages from other sessions")
    assert text.index(TRANSCRIPT) > heading > text.index(LEARNING)


def test_the_budget_drops_a_tag_and_its_record_together(lookup, monkeypatch):
    monkeypatch.setenv("COGNEE_INJECT_MAX_CHARS", "3000")
    big = [(tag("passage", 10 + i), f"passage {i}: " + "x" * 400) for i in range(6)]
    value = item(True)
    passages = "## Relevant passages\n" + "\n---\n".join(f"{t}\n{p}" for t, p in big)
    value["text"] = value["text"].replace(
        value["text"][
            value["text"].index("## Relevant passages") : value["text"].index(
                "\n\n## Relevant entities"
            )
        ],
        passages,
    )
    value["metadata"]["records"] += [{"tag": t} for t, _ in big]
    text = injected(cited(lookup, monkeypatch, {"graph": [value]}))
    kept = [t for t, _ in big if t in text]
    assert 0 < len(kept) < len(big)
    for t, body in big:
        assert (t in text) == (body in text), t


def test_a_refused_cited_recall_falls_back_to_the_regular_one(lookup, monkeypatch):
    def recall(_prompt, **kw):
        if kw["scope"] != ["graph"]:
            return []
        if kw.get("result_mode") == "cited":
            body = b'{"detail": "dataset has no passages [CitedRequiresHybridError]"}'
            raise urllib.error.HTTPError("u", 422, "Unprocessable", {}, io.BytesIO(body))
        return [item(False)]

    run = cited(lookup, monkeypatch, recall)
    assert run.calls.count("graph") == 2
    assert run.fired("recall_cited_refused")
    text = injected(run)
    assert "Pinning keeps recall on the active dataset." in text
    assert not any(t in text for t in RECORDS)


def test_another_422_is_not_retried(lookup, monkeypatch):
    def recall(_prompt, **kw):
        if kw["scope"] != ["graph"]:
            return []
        raise urllib.error.HTTPError("u", 422, "x", {}, io.BytesIO(b'{"detail": "bad query"}'))

    run = cited(lookup, monkeypatch, recall)
    assert run.calls.count("graph") == 1 and not run.fired("recall_cited_refused")


def test_receipts_name_each_cited_record_and_dedup_ignores_the_tag(lookup, modules, monkeypatch):
    evidence = modules("_evidence")
    cited(lookup, monkeypatch)
    items = evidence.last_receipt(KEY)["items"]
    references = {i["reference"]["tag"] for i in items if i["addressable"]}
    assert references == set(RECORDS) - {EMPTY}
    tenant = next(i for i in items if (i["reference"] or {}).get("tag") == TENANT)
    assert tenant["reference"]["kind"] == "entity"
    assert tenant["reference"]["record_id"].endswith("000000000004")
    assert tenant["reason"] == "injected"
    # The conflict note is no record, so it has no reference.
    assert any(not i["addressable"] for i in items)
    cited(lookup, monkeypatch)
    again = evidence.last_receipt(KEY)["items"]
    assert {i["reason"] for i in again if i["addressable"]} == {"already_in_context"}

    ledger = modules("_injection_ledger")
    assert ledger.entry_hash(f"{TENANT}\n### a\n- b") == ledger.entry_hash("### a\n- b")


def test_explain_prints_each_records_tag(lookup, modules, suite, hook_module, monkeypatch):
    cited(lookup, monkeypatch)
    cli = hook_module(suite, "cognee-evidence.py")
    receipt = cli.run(type("Args", (), {"session_key": KEY})())
    citations = {i.get("citation") for i in receipt["items"]}
    assert TENANT in citations and EMPTY not in citations


def test_the_capability_cache_belongs_to_one_server(modules, monkeypatch):
    cited_recall = modules("_cited_recall")
    url = modules("_cloud_worker").target()["base_url"]
    assert not cited_recall.supported(url)
    cited_recall.record_capabilities({"version": "1.7.0", "capabilities": ["recall_cited_v1"]})
    assert cited_recall.supported(url) and cited_recall.supported(url + "/")
    assert not cited_recall.supported("https://other.example")
    cited_recall.record_capabilities({"version": "1.6.5", "capabilities": ["session_summary_v1"]})
    assert not cited_recall.supported(url)


def test_recall_text_keeps_tags_with_records(modules):
    rt = modules("_recall_text")
    tags = frozenset(RECORDS)
    entities = "\n\n".join(f"{t}\n{e}" for t, e in ENTITIES)
    pruned, _ = rt.prune_entities(entities, tags)
    assert pruned == f"{TENANT}\n### tenant server (software)\n- The tenant server runs cognee."
    # Without tags, an untagged block prunes exactly as before.
    plain = "\n\n".join(e for _, e in ENTITIES)
    assert rt.prune_entities(plain) == rt.prune_entities(plain, frozenset())
    block = "\n---\n".join(f"{t}\n{p}" for t, p in PASSAGES)
    assert rt.split_passages(block, tags) == [f"{t}\n{p}" for t, p in PASSAGES]
    assert rt.is_session_transcript(f"{TRANSCRIPT}\nSession ID: x\nQuestion: q")
    assert rt.passage_session_id(f"{TRANSCRIPT}\nSession ID: claude_x\nQuestion: q") == "claude_x"
    assert rt.cited_tags(item(True)) == tags
    assert rt.cited_tags(item(False)) == frozenset()
    assert rt.cited_tags({"metadata": {"records": [{"tag": "[mem:bad]"}]}}) == frozenset()


class _Server:
    def __init__(self, capabilities, rows=()):
        self.capabilities, self.rows, self.posts = capabilities, list(rows), []

    def __call__(self, rt, path, payload=None):
        if path == "/health":
            return {"capabilities": self.capabilities}
        self.posts.append(payload)
        return self.rows


@pytest.fixture
def search(suite, hook_module, monkeypatch):
    if suite.name != "claude-code":
        pytest.skip("Claude Code cited recall")
    module = hook_module(suite, "cognee_search.py")
    monkeypatch.setattr(
        module,
        "_runtime",
        lambda: {
            "service_url": "https://cloud.example",
            "api_key": "k",
            "session_id": "",
            "dataset": "memory",
            "dataset_ids": "",
            "recall_auto": "",
        },
    )
    return module


def test_expand_prints_bodies_and_lists_missing_and_forbidden(search, monkeypatch, capsys):
    changed = tag("passage", 1, "c")
    server = _Server(
        ["recall_cited_v1"],
        [
            {
                "text": "Pinning keeps recall on one dataset.",
                "metadata": {"status": "available", "tag": changed},
            },
            {"text": "", "metadata": {"status": "missing"}},
            {"text": "", "metadata": {"status": "forbidden"}},
        ],
    )
    monkeypatch.setattr(search, "_http_json", server)
    code = search.main([PROMPT, "--expand", f"{LEARNING},{TENANT},{PINNING},{LEARNING}"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert server.posts == [{"query": PROMPT, "expand_items": [LEARNING, TENANT, PINNING]}]
    assert out["records"][0]["text"] == "Pinning keeps recall on one dataset."
    assert out["records"][0]["changed"] is True and out["records"][0]["current_tag"] == changed
    assert out["missing"] == [TENANT] and out["forbidden"] == [PINNING]


def test_expand_refuses_bad_tags_and_old_servers(search, monkeypatch, capsys):
    server = _Server(["session_summary_v1"])
    monkeypatch.setattr(search, "_http_json", server)
    assert search.main(["--expand", "[mem:bad]"]) == 2
    assert (
        search.main(["--expand", ",".join([TENANT] * 1 + [tag("fact", n) for n in range(30)])]) == 2
    )
    assert search.main(["--expand", TENANT]) == 1
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["status"] == "unsupported"
    assert server.posts == []
