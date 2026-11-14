#!/bin/bash
# Isolates one `claude plugin eval` run; runs only with --scaffold (see ../../README.md).
# The harness gives each run a temporary HOME and does not pass COGNEE_* variables from
# your shell, so the plugin is configured through that HOME's env file instead: the
# plugin is turned off, so no hook or command reaches a server or starts one.
set -eu
env_file="$HOME/.cognee/.env"
if [ -e "$env_file" ]; then
  echo "isolate.sh: $env_file exists; refusing to replace a real Cognee config" >&2
  exit 1
fi
mkdir -p "$HOME/.cognee"
cat > "$env_file" <<'EOF'
COGNEE_PLUGIN_DISABLED=true
EOF
