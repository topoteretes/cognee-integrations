# Releasing the Cognee Codex plugin

This checklist covers cutting a new release of the Codex integration.
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) and
tags use the `codex-v*` prefix (for example, `codex-v1.0.3-local`).

## Release checklist

1. **Bump the version.**
   - Update `version` in `integrations/codex/plugins/cognee/.codex-plugin/plugin.json`.

2. **Update the changelog.**
   - Move the relevant entries from `[Unreleased]` into a new version section in `CHANGELOG.md`.
   - Set the release date (`YYYY-MM-DD`) and keep the Added / Changed / Fixed grouping.
   - Update the comparison links at the bottom of the file.

3. **Sync the marketplace version.**
   - Confirm the plugin entry in [`.agents/plugins/marketplace.json`](.agents/plugins/marketplace.json) points at the released plugin. If the marketplace ever pins an explicit version, update it to match `plugin.json`.

4. **Open a release PR.**
   - Commit the version bump, changelog, and any marketplace changes together.
   - Merge once CI is green.

5. **Tag the release.**
   - After merging to `main`:
     ```bash
     git tag codex-v<version>
     git push origin codex-v<version>
     ```

6. **Verify.**
   - Confirm `plugin.json` reports the new version.
   - Install the plugin from the Codex marketplace and confirm the new version loads.
