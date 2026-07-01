# Releasing the Codex plugin

Checklist for cutting a new Codex Cognee plugin release. This is a **local**
Codex marketplace plugin (`registry: codex-marketplace`), so versions carry the
`-local` suffix.

1. **Bump the version.** Update `version` in
   [`plugins/cognee/.codex-plugin/plugin.json`](./plugins/cognee/.codex-plugin/plugin.json).
   Use [SemVer](https://semver.org/) with the `-local` pre-release suffix.

2. **Update the changelog.** Move the relevant notes from `## [Unreleased]` in
   [`CHANGELOG.md`](./CHANGELOG.md) into a new `## [x.y.z-local]` section, and add
   the matching compare/release links at the bottom.

3. **Sync the inventory.** Set the same version in the `codex` `current_version`
   in [`integrations/inventory.yml`](../inventory.yml).
   `scripts/check_version_consistency.py` (CI) fails if these drift. (The Codex
   `.agents/plugins/marketplace.json` does not carry a version field — nothing to
   sync there.)

4. **Commit + tag.** Commit the bump, then tag:
   ```sh
   git tag codex-v<version>
   git push origin codex-v<version>
   ```

5. **Verify.** Confirm CI is green and that the plugin loads from the local Codex
   marketplace at the new version.
