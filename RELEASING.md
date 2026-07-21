# Releasing Cognee Integrations

Use this short checklist for each integration release.

## Before tagging

1. Bump the integration version in the plugin manifest.
2. Update `integrations/inventory.yml` so `current_version` matches the manifest.
3. Add a note to `CHANGELOG.md` under `Unreleased`.
4. Run the relevant integration tests and any release checks.

## Tagging

1. Create the integration tag that matches the released package or plugin.
2. For Claude Code, use tags like `claude-code-v*`.
3. For other integrations, follow the package-specific tag pattern already used in that integration's release flow.

## After tagging

1. Sync the marketplace or package registry metadata if the integration publishes there.
2. Confirm the published version matches the manifest.
3. Update the changelog with the released version and date.

## Notes

- Keep release notes short and user-facing.
- If a release changes a manifest version, update the inventory check in the same PR.
- If a release adds a new integration, add it to `integrations/inventory.yml` and the changelog together.

