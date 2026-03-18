# Integration Package Template

Use this template when creating or migrating an integration into this monorepo.

## Required Layout

```text
integrations/<integration-name>/
  README.md
  examples/
    README.md
  tests/ or __tests__/
  pyproject.toml or package.json
```

Keep integration boundaries strict: each package owns its own dependencies, tests, docs, and publish versioning.

## Required Documentation

`README.md` must include:

1. Install instructions.
2. Configuration reference.
3. Example usage (also link to `examples/`).
4. Async-first guidance for long-running operations (timeouts, retries, non-blocking behavior).
5. Compatibility notes (Cognee API/SDK or platform version expectations).

## Required Examples

`examples/` must include at least one runnable or copy/paste-ready usage path and a short README explaining prerequisites.

## Required Tests

Every integration must include a baseline smoke test:

- Python: `tests/` with at least one `pytest` test file.
- TypeScript: `__tests__/` with at least one `jest` test file.

## Dependency / Pinning Policy

- Python integrations that depend on the Cognee SDK must use bounded version ranges (`>=` lower bound and `<`/`<=` upper bound).
- TypeScript integrations that communicate over HTTP should document supported server/API versions.

## Inventory Requirement

Each integration must be listed in `integrations/inventory.yml` with:

- `slug`
- `ownership`
- `migration_status`
- `package_name`
- `monorepo_path` (for `migration_status: done`)
- source repo + archive/redirect tracking (when applicable)

