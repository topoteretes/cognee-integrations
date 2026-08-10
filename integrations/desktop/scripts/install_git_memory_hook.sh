#!/usr/bin/env bash
# Give a git repository memory: every commit becomes a captured note in
# cognee, so "why did we change the retry logic?" is answerable months later.
#   ./install_git_memory_hook.sh /path/to/repo
set -euo pipefail

REPO="${1:?usage: install_git_memory_hook.sh /path/to/repo}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$REPO/.git/hooks/post-commit"

cat > "$HOOK" <<HOOK
#!/usr/bin/env bash
# cognee memory hook — captures each commit (message + stats) into memory.
# Fails silently: committing must never depend on the memory backend.
(
  MSG=\$(git log -1 --pretty=%B)
  STATS=\$(git log -1 --stat --pretty=format:)
  NAME=\$(basename "\$(git rev-parse --show-toplevel)")
  HASH=\$(git rev-parse --short HEAD)
  printf 'Commit %s in %s\n\n%s\n\n%s\n' "\$HASH" "\$NAME" "\$MSG" "\$STATS" \
    | "$ROOT/scripts/cognee-send" --title "commit: \$(git log -1 --pretty=%s)" \
        --source "git:\$NAME" >/dev/null 2>&1 || true
) &
HOOK
chmod +x "$HOOK" "$ROOT/scripts/cognee-send"
echo "Installed post-commit memory hook in $REPO"
