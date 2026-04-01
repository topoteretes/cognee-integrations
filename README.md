# Cognee Integrations

Monorepo for Cognee-owned integration packages.

## Structure

Each integration lives under `integrations/<name>/` and remains independently versioned/publishable.

```text
integrations/
  dify/               -> cognee-dify-plugin
  google-adk/         -> cognee-integration-google-adk
  langgraph/          -> cognee-integration-langgraph
  openclaw/           -> @cognee/cognee-openclaw
  openclaw-skills/    -> cognee-openclaw-skills
```

## Integration Standard

Use these files when adding or migrating an integration:

- `integrations/TEMPLATE.md` - required package structure
- `docs/integration-checklist.md` - required docs/examples/tests + publish gate
- `.github/pull_request_template.md` - PR checklist that must be completed

CI enforces standard structure and inventory consistency via `scripts/check_integration_standards.py`.

## Development

Each integration is developed independently with its own toolchain.

```bash
# Python integrations
cd integrations/<name>
uv sync --locked --dev
uv run pytest tests/ -v
uv run ruff check .

# TypeScript integrations
cd integrations/<name>
npm install
npx tsc --noEmit --skipLibCheck
```

## Version Pinning Policy

Python integrations that depend on the Cognee SDK must pin `cognee` with a bounded range (for example, `cognee>=0.5.1,<0.5.4`). This is enforced by CI via `scripts/check_version_pins.py`.

TypeScript integrations that use Cognee over HTTP are exempt from Python package pinning and must document compatible Cognee API/server expectations.

## Publish Gate

A package is publish-ready only when all of the following are true:

1. Integration checklist is complete in the PR.
2. Standard-structure check passes in CI.
3. Pinning check passes (when the integration depends on the Python Cognee SDK).
4. Integration tests/typecheck pass for the changed package.
5. Inventory entry is present and up to date.
6. Integration is approved by cognee team.

See `docs/integration-checklist.md` for the full gate definition.

## CI

- `ci.yml`: selective tests by changed integration (pytest for Python, tsc for TypeScript)
- `lint.yml`: Ruff formatting/lint checks for Python code
- `check-pinning.yml`: bounded Cognee dependency enforcement for Python integrations
- `integration-standards.yml`: required integration structure + inventory coverage checks

## Migration Inventory

`integrations/inventory.yml` is the source of truth for:

- ownership classification (`cognee-owned` vs `partner-owned`)
- migration state (`done`, `pending`, `skipped`)
- monorepo package path/name
- source repository and archive/redirect tracking for migrated repos
