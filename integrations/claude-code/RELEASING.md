# Releasing

This document describes how to release a new version of the Cognee Claude Code plugin.

## Release checklist

1. **Create a release branch**
   ```bash
   git checkout -b release/claude-code-v0.x.x
   ```

2. **Update the version**
   - `integrations/claude-code/.claude-plugin/plugin.json` — bump `version`
   - `integrations/inventory.yml` — bump `current_version` for `slug: claude-code`
   - `.claude-plugin/marketplace.json` — bump `version` for plugin `cognee-memory`

3. **Update the changelog**
   - Move items from "Unreleased" to a new dated section in `CHANGELOG.md`
   - Follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format

4. **Run version consistency check**
   ```bash
   python3 scripts/check_version_consistency.py
   ```
   Must exit 0.

5. **Run tests**
   ```bash
   cd integrations/claude-code
   uv sync --locked --dev
   uv run pytest tests/ -v
   ```

6. **Commit and tag**
   ```bash
   git add -A
   git commit -s -m "chore(claude-code): release v0.x.x"
   git tag claude-code-v0.x.x
   git push origin release/claude-code-v0.x.x --tags
   ```

7. **Open a pull request**
   - Open a PR against `main`
   - Title: `chore(claude-code): release v0.x.x`
   - Include changelog summary in the PR description

8. **After merge, sync the marketplace version**
   - The marketplace source (`marketplace.json`) is consumed by Claude Code's plugin system.
   - Once the PR merges, run inside Claude Code to pick up the update:
     ```
     /plugin uninstall cognee-memory@cognee
     /plugin install cognee-memory@cognee
     ```
   - Or for a full refresh:
     ```
     /plugin uninstall cognee-memory@cognee
     /plugin marketplace remove topoteretes/cognee-integrations
     /plugin marketplace add topoteretes/cognee-integrations
     /plugin install cognee-memory@cognee
     ```
