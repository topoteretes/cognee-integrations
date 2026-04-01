# Integration Checklist

This checklist defines the required publish gate for all integration packages in this monorepo.

## Required Structure

- [ ] Package is under `integrations/<name>/`.
- [ ] Package has `README.md`.
- [ ] Package has `examples/` with usage documentation.
- [ ] Package has tests (`tests/` or `__tests__/`).
- [ ] Package has a package manifest (`pyproject.toml` or `package.json`).
- [ ] `README.md` includes async-first guidance (timeouts/retries/non-blocking behavior).

## Dependency Policy

- [ ] If Python package depends on `cognee`, dependency is bounded (`>=...` and `<...`).
- [ ] If package uses Cognee HTTP API, compatibility expectations are documented.

## Migration / Inventory

- [ ] `integrations/inventory.yml` entry exists (or is updated) for this integration.
- [ ] Ownership is classified (`cognee-owned` or `partner-owned`).
- [ ] Migration state is current (`done`, `pending`, `skipped`).
- [ ] For migrated repos with legacy source repos, archive/redirect status and link are tracked.
- [ ] Installation continuity is preserved or replacement instructions are documented.

## Publish Gate (Definition)

A package is eligible to publish only when:

1. This checklist is completed in the PR.
2. `integration-standards.yml` passes.
3. `check-pinning.yml` passes when applicable.
4. Integration tests/typecheck pass for the changed package.
5. Inventory metadata is current.

