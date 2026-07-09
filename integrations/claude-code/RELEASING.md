# Releasing the Claude Code plugin

Checklist for cutting a new `cognee-memory` (Claude Code) release.

1. **Bump the version.** Update `version` in
   [`.claude-plugin/plugin.json`](./.claude-plugin/plugin.json). Use
   [SemVer](https://semver.org/).

2. **Update the changelog.** Move the relevant notes from `## [Unreleased]` in
   [`CHANGELOG.md`](./CHANGELOG.md) into a new `## [x.y.z]` section, and add the
   matching compare/release links at the bottom.

3. **Sync the marketplace + inventory.** Set the same version in:
   - the `cognee-memory` entry of the top-level
     [`.claude-plugin/marketplace.json`](../../.claude-plugin/marketplace.json)
   - the `claude-code` `current_version` in
     [`integrations/inventory.yml`](../inventory.yml)

   `scripts/check_version_consistency.py` (CI) fails if these drift.

4. **Commit + tag.** Commit the bump, then tag:
   ```sh
   git tag claude-code-v<version>
   git push origin claude-code-v<version>
   ```

5. **Verify.** Confirm CI is green (import smoke tests + version consistency) and
   that the plugin installs from the marketplace at the new version.
