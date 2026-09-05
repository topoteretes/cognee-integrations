---
name: cognee-manage-access
description: Inspect plugin memory permissions, grant or revoke access, choose graph read datasets, reconnect an existing plugin identity, or switch the write dataset.
---

# Manage Cognee memory access

Use the installed plugin's `scripts/memory-access.py`. Resolve that path relative
to this skill's plugin root. Run `python3 <script> status` first. Use dataset and
principal UUIDs returned by the API; names can collide across owners. Status
includes readable datasets as well as the separately verified write choices.
New identity creation needs the safe provisioning contract in Cognee SDK
PR #4942; Cognee 1.5.4 supports the existing-principal access commands.

- Grant or revoke a specific permission with `grant` / `revoke --principal-id UUID
  --dataset-id UUID --permission read|write`. Repeated `--dataset-id` selects
  several datasets. These commands authenticate as the user principal, and the
  backend must authorize sharing. Grant only the datasets and permissions the
  user requested; do not infer team-wide sharing from a request to recall memory.
- Select graph reads with `read --session-key HOST_ID --dataset-id UUID ...`.
  No dataset arguments restores recall of the active write dataset. This changes
  recall selection, not permissions. The selection is bound to the current
  identity, backend, and host launch; account changes require re-selection.
- Switch writes with `write UUID --session-key HOST_ID`. It syncs first, then
  registers a fresh session and changes the launch record. A failed sync aborts.
  Never bypass that failure automatically. Reads and writes are independent.
- Reconnect a supplied existing plugin key with `connect --key-env ENV_VAR_NAME`.
  Never put key values in command arguments or print them. It verifies the parent
  and plugin identity, imports the key without rotation, and requires a fresh
  host session. New identity creation remains an explicit SessionStart opt-in.

Existing data is not moved or copied by these commands. Promotion of selected
persisted memory from agent to user to team belongs to the SDK `cognee.promote`
API and requires separate authorization and an explicit destination.

Validate changes with status and actual authorized recall/write checks. Explain
any permission denial; never retry using a more privileged data-plane key.
