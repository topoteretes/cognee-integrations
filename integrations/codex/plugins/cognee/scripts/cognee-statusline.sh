#!/usr/bin/env bash
# Cognee status line entrypoint (Codex).
#
# The host pipes the statusline JSON context (including session_id) on stdin.
# We `exec` into the standalone Python renderer so it inherits that same stdin —
# the renderer is pure-local (reads only ~/.cognee-plugin/codex JSON files),
# never imports the plugin runtime, and makes no network call, so it stays
# instant.
exec python3 "$(dirname "$0")/cognee_statusline_render.py" "$@"
