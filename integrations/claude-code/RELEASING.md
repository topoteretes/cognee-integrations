# Releasing claude-code

1. Bump the version in `plugin.json`.
2. Move all `[Unreleased]` entries into a new `[vX.Y.Z] - YYYY-MM-DD` section in `CHANGELOG.md`.
3. Commit both files:
   ```
   git commit -m "release: vX.Y.Z"
   ```
4. Tag the release:
   ```
   git tag claude-code-vX.Y.Z
   ```
5. Push the tag:
   ```
   git push origin claude-code-vX.Y.Z
   ```
6. Sync the marketplace version if applicable.
