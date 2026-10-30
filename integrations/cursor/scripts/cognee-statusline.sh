#!/bin/sh
# Cognee status line entrypoint for the Cursor CLI.
#
# `~/.cursor/cli-config.json` -> statusLine.command points here (written by
# session-start.py). The CLI spawns it without a shell and pipes the statusline
# JSON context (including session_id) on stdin. We `exec` into the standalone
# Python renderer so it inherits that same stdin — the renderer is pure-local
# (reads only ~/.cognee-plugin JSON files), never imports the plugin runtime,
# and makes no network call, so it stays well inside the CLI's 2s timeout.
if command -v python3 >/dev/null 2>&1; then
    exec python3 "$(dirname "$0")/cognee_statusline_render.py"
fi
exec python "$(dirname "$0")/cognee_statusline_render.py"
