# Session id sanitization conformance

Every Cognee integration turns a native session id into a Cognee session id with
the same shape: `{agent}_{native_id}`. Before the native id is used, each
integration runs it through a small sanitizer. The rule is:

- Keep only the ASCII set `[A-Za-z0-9-_.]`.
- Replace every other character with `_`.
- Trim leading and trailing `.` and `_`.
- Cap the length at 120 characters.

`session_id_cases.json` is the single source of truth for that rule. It is a
list of `{ "input", "expected", "note" }` cases. Each integration has a small
test that loads this file and checks its own sanitizer against it:

- claude-code: `integrations/claude-code/tests/test_session_id_conformance.py`
- codex: `integrations/codex/tests/test_session_id_conformance.py`
- hermes-agent: `integrations/hermes-agent/tests/test_session_id_conformance.py`
- openclaw: `integrations/openclaw/__tests__/test_session_id_conformance.ts`

Because every test reads the same file, any implementation that drifts from the
rule fails its test. CI runs all four on every pull request: hermes-agent via
the `test-python` job (it has a `pyproject.toml`), and claude-code, codex and
openclaw via the dedicated `test-conformance` job in
`.github/workflows/ci.yml`.

## Note on empty results

The table only holds inputs whose sanitized output is not empty. hermes-agent
returns `"session"` when the result would be empty, while the others return an
empty string. That one case is left out on purpose so the shared table stays
identical for all four integrations.
