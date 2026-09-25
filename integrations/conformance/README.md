# Session id sanitization conformance

Every Cognee integration turns a native session id into a Cognee session id with
the same shape: `{agent}_{native_id}`. Before the native id is used, each
integration runs it through a small sanitizer. The rule is:

- Keep only the ASCII set `[A-Za-z0-9-_.]`.
- Replace every other character with `_`.
- Trim leading and trailing `.` and `_`.
- Cap the length at 120 characters.

`session_id_cases.json` is the single source of truth for that rule. It is a
list of `{ "input", "expected", "note" }` cases. Each test loads this file and
checks its sanitizer against it. The tests live with the suite that already
covers the tree they test, so no integration needs a test project of its own:

- claude-code and codex:
  `integrations/tests/tests/unit/test_session_id_conformance.py` — the shared
  hook-plugin suite, parametrized over both plugin trees.
- hermes-agent: `integrations/hermes-agent/tests/test_session_id_conformance.py`
- openclaw: `integrations/openclaw/__tests__/unit/test_session_id_conformance.ts`

Because every test reads the same file, any implementation that drifts from the
rule fails its test. CI runs all of them on every pull request through the
existing jobs in `.github/workflows/ci.yml`: the shared suite via `test-python`
(`detect-changes` maps a claude-code or codex change onto the `tests` cell) and
again under Python 3.9 via `test-hooks-python39`, hermes-agent via `test-python`
(it has its own `pyproject.toml`), and openclaw via `test-typescript`, which
runs `npm test` as well as the type check.

## Note on empty results

The table only holds inputs whose sanitized output is not empty. hermes-agent
returns `"session"` when the result would be empty, while the others return an
empty string. That one case is left out on purpose so the shared table stays
identical for all four integrations.

# Dataset name sanitization conformance

cognee rejects a dataset name that contains a space or a dot
(`check_dataset_name`, run on every write: remember, improve, memify and the
pipelines). Every integration therefore runs a configured or derived dataset
name through a small sanitizer before using it. The rule is deliberately narrow:

- Trim surrounding whitespace.
- Replace every run of spaces and dots with a single `_`.
- If nothing but underscores is left (and the input was not already that),
  use the integration's default dataset instead.

Nothing else changes. A name the server accepts today (`Foo+Bar`, `héllo`,
`_x_`) comes back unchanged, so no user is silently moved to a new, empty
dataset. A name that contains a space or a dot never took a write, so rewriting
it orphans nothing. The vscode extension's `sanitizeDatasetName` is stricter
(`[^A-Za-z0-9_-]` → `_`) because it only cleans names it derives itself.

`dataset_name_cases.json` holds `{ "input", "fallback", "expected", "note" }`
cases; each test passes `fallback` as the default, so one table serves
integrations with different defaults. The tests live next to the session-id ones:

- claude-code, codex and antigravity:
  `integrations/tests/tests/unit/test_dataset_name_conformance.py`
- hermes-agent: `integrations/hermes-agent/tests/test_dataset_name_conformance.py`
- openclaw: `integrations/openclaw/__tests__/unit/test_dataset_name_conformance.ts`

Where a name is typed explicitly (the dataset-switch commands), an invalid name
is refused with the sanitized form as a suggestion instead of being rewritten,
so a switch never lands in a dataset the user did not name.
