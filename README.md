# Cognee Integrations

Monorepo for all Cognee-owned integration packages.

## Structure

Each integration lives under `integrations/<name>/` and is an independently publishable package.

```
integrations/
  openclaw/           -> @openclaw/memory-cognee (npm)
  claude-code/        -> Cognee plugin for Claude Code
  codex/              -> Cognee plugin marketplace for Codex
```

## Adding a New Integration

### Python integrations

_(Template coming soon. For now, follow the TypeScript pattern below and adapt for Python with `pyproject.toml`.)_

### TypeScript/Node integrations (e.g., OpenClaw plugins)
1. Create `integrations/<name>/` with `package.json`, entry file, and plugin manifest
2. Follow the target platform's plugin conventions
3. Add an entry to `integrations/inventory.yml`

CI auto-detects new integrations by language (Python via `pyproject.toml`, TypeScript via `package.json`) — no workflow edits needed.

## Development

Each integration is developed independently with its own toolchain:

```bash
# Python integrations
cd integrations/<name>
uv sync --dev
uv run pytest tests/ -v
uv run ruff check .

# TypeScript integrations
cd integrations/<name>
npm install
npx tsc --noEmit
```

## Version Pinning Policy

Python integrations must pin the `cognee` dependency with a bounded range (e.g., `cognee>=0.5.1,<0.6.0`). This is enforced by CI via `scripts/check_version_pins.py`. TypeScript integrations that talk to Cognee via HTTP API are exempt from package pinning but should document compatible Cognee server versions.

When a new `cognee` version is released:
1. Update the bounds in affected integrations
2. Run tests to verify compatibility
3. Bump the integration version
4. Publish the updated package

## Publishing

Each integration is published independently via tag-per-package:

```bash
# TypeScript: publishes to npm
git tag openclaw-v2026.2.4 && git push --tags

# Python (when added): publishes to PyPI
# git tag <name>-v<version> && git push --tags
```

The `publish.yml` workflow parses the tag, runs tests, and publishes to the appropriate registry.

## CI

- **Lint**: Ruff on every PR across all Python integrations
- **Tests**: Auto-detects changed integrations and runs the right test suite (pytest for Python, tsc for TypeScript)
- **Pin check**: Validates bounded `cognee` dependencies in Python integrations
- **Publish**: Tag-triggered per-package publishing to PyPI or npm

## Inventory

`integrations/inventory.yml` tracks all known integrations with ownership, migration status, package names, and version info. Update it when adding or migrating integrations.
