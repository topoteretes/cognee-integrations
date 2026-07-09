"""Cross-integration parity test — issue #3549.

Asserts that the dataset-name sanitizer produces **identical output** across
all three Python integrations (hermes-agent, codex, claude-code) for every
canonical input from the issue.  The TypeScript openclaw sanitizer uses the
same algorithm; its parity is verified independently in:
    integrations/openclaw/__tests__/test_datasetName.ts

Run from the cognee-integrations repo root:
    python tests/test_cross_integration_parity.py
or:
    pytest tests/test_cross_integration_parity.py
"""

import importlib.util
import pathlib
import sys
import types

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent  # cognee-integrations/


def _load_module(label: str, file_path: pathlib.Path) -> types.ModuleType:
    """Load a Python file by absolute path under a unique module label.

    Using a unique label per integration avoids sys.modules key collisions
    between codex and claude-code, which both expose a module named
    `_plugin_common`.
    """
    spec = importlib.util.spec_from_file_location(label, file_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ── hermes-agent ─────────────────────────────────────────────────────────────
_hermes_root = REPO_ROOT / "integrations" / "hermes-agent"
sys.path.insert(0, str(_hermes_root))
try:
    from cognee_integration_hermes.provider import _sanitize_dataset_name as hermes_sanitize  # noqa: E402,I001
finally:
    sys.path.pop(0)

# ── codex ─────────────────────────────────────────────────────────────────────
_codex_pc = _load_module(
    "codex__plugin_common",
    REPO_ROOT / "integrations" / "codex" / "plugins" / "cognee" / "scripts" / "_plugin_common.py",
)
codex_sanitize = _codex_pc.sanitize_dataset_name  # type: ignore[attr-defined]

# ── claude-code ───────────────────────────────────────────────────────────────
_claude_pc = _load_module(
    "claude_code__plugin_common",
    REPO_ROOT / "integrations" / "claude-code" / "scripts" / "_plugin_common.py",
)
claude_sanitize = _claude_pc.sanitize_dataset_name  # type: ignore[attr-defined]

# ─────────────────────────────────────────────────────────────────────────────
# Canonical test cases — expected output is integration-agnostic.
# Use a sentinel string as the fallback so we can detect when a sanitizer
# correctly falls back (rather than accidentally matching "hermes" or
# "agent_sessions").
# ─────────────────────────────────────────────────────────────────────────────
FALLBACK = "DEFAULT"

CASES: list[tuple[str, str]] = [
    # valid names — pass through unchanged
    ("mydataset",    "mydataset"),
    ("project_01",   "project_01"),
    ("company-prod", "company-prod"),
    ("test.v1",      "test.v1"),
    # invalid names — must be normalised identically
    ("My Dataset",   "My_Dataset"),   # space → _
    ("My@Data",      "My_Data"),      # @ → _
    ("!!hello!!",    "hello"),        # leading/trailing _ stripped
    ("###",          FALLBACK),       # all _ after replace → stripped → empty → fallback
    ("........",     FALLBACK),       # all . stripped → empty → fallback
    ("___",          FALLBACK),       # all _ stripped → empty → fallback
    ("a" * 200,      "a" * 120),      # truncated to 120
    ("你好 Dataset",  "Dataset"),      # non-ASCII → _, then stripped
    # edge cases
    ("",             FALLBACK),       # empty → fallback
    ("  ",           FALLBACK),       # spaces only → __ → stripped → fallback
    ("-leading",     "-leading"),     # leading - is NOT stripped (only . and _ are)
    (".leading",     "leading"),      # leading . stripped
    ("_leading",     "leading"),      # leading _ stripped
    ("trailing.",    "trailing"),     # trailing . stripped
]

SANITIZERS: dict[str, object] = {
    "hermes-agent": hermes_sanitize,
    "codex":        codex_sanitize,
    "claude-code":  claude_sanitize,
}


def test_all_integrations_agree() -> None:
    """Every sanitizer must produce the exact same string for every canonical input."""
    for raw_input, expected in CASES:
        results = {name: fn(raw_input, FALLBACK) for name, fn in SANITIZERS.items()}  # type: ignore[operator]
        unique = set(results.values())
        assert len(unique) == 1, (
            f"Input {raw_input!r}: integrations disagree — {results}"
        )
        actual = unique.pop()
        assert actual == expected, (
            f"Input {raw_input!r}: all integrations agreed on {actual!r} but expected {expected!r}"
        )


def test_each_integration_individually() -> None:
    """Belt-and-suspenders: run the full canonical table per integration."""
    for int_name, fn in SANITIZERS.items():
        for raw_input, expected in CASES:
            result = fn(raw_input, FALLBACK)  # type: ignore[operator]
            assert result == expected, (
                f"[{int_name}] sanitize({raw_input!r}) = {result!r}, want {expected!r}"
            )


def test_fallback_only_when_empty() -> None:
    """Fallback must NOT fire when the sanitised name is non-empty."""
    for int_name, fn in SANITIZERS.items():
        assert fn("ok_name", "fb") == "ok_name", f"[{int_name}] fired fallback for valid name"  # type: ignore[operator]
        result = fn("###", "fb")  # type: ignore[operator]
        assert result == "fb", f"[{int_name}] did not fire fallback for invalid name"


def test_max_length_is_120() -> None:
    """All integrations must cap the sanitised name at 120 characters."""
    long_input = "x" * 200
    for int_name, fn in SANITIZERS.items():
        result = fn(long_input, FALLBACK)  # type: ignore[operator]
        assert len(result) == 120, (
            f"[{int_name}] expected length 120, got {len(result)}"
        )


if __name__ == "__main__":
    failures = 0
    for _test_name, _fn in sorted(globals().items()):
        if _test_name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print("PASS", _test_name)
            except AssertionError as exc:
                failures += 1
                print("FAIL", _test_name, "→", exc)
    raise SystemExit(1 if failures else 0)
