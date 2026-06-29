# Releasing the Cognee Memory plugin for Claude Code

This checklist covers cutting a new release of the Claude Code integration.
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) and
tags use the `claude-code-v*` prefix (for example, `claude-code-v0.2.0`).

## Release checklist

1. **Bump the version.**
   - Update `version` in `integrations/claude-code/.claude-plugin/plugin.json`.

2. **Update the changelog.**
   - Move the relevant entries from `[Unreleased]` into a new version section in `CHANGELOG.md`.
   - Set the release date (`YYYY-MM-DD`) and keep the Added / Changed / Fixed grouping.
   - Update the comparison links at the bottom of the file.

3. **Sync the marketplace version.**
   - Update the `cognee-memory` plugin `version` in the root [`.claude-plugin/marketplace.json`](../../.claude-plugin/marketplace.json) so it matches `plugin.json`.

4. **Open a release PR.**
   - Commit the version bump, changelog, and marketplace sync together.
   - Merge once CI is green.

5. **Tag the release.**
   - After merging to `main`:
     ```bash
     git tag claude-code-v<version>
     git push origin claude-code-v<version>
     ```

6. **Verify.**
   - Confirm `plugin.json` and `marketplace.json` report the same version.
   - Install the plugin from the marketplace and confirm the new version loads.
