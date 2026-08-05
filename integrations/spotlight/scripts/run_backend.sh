#!/usr/bin/env bash
# Start the Cognee Spotlight backend. Reads .env from the integration root.
# COGNEE_MODE=fake (default) needs no keys; =local needs LLM_API_KEY (and the
# "local" extra); =cloud needs COGNEE_CLOUD_URL / COGNEE_CLOUD_API_KEY.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

# The app's onboarding writes this file (mode + credentials). It is sourced
# after .env so a choice made in the app wins over a developer's repo config.
# SPOTLIGHT_PROFILE=<name> selects an alternate profile: own config file, own
# data dir, own port — two users (or two accounts) side by side on one Mac.
PROFILE="${SPOTLIGHT_PROFILE:-default}"
if [ "$PROFILE" = "default" ]; then
  APP_CONFIG="$HOME/.cognee-spotlight/backend.env"
else
  APP_CONFIG="$HOME/.cognee-spotlight/profiles/$PROFILE/backend.env"
  export SPOTLIGHT_DATA_DIR="${SPOTLIGHT_DATA_DIR:-$HOME/.cognee-spotlight/profiles/$PROFILE}"
  export SPOTLIGHT_COGNEE_ROOT="${SPOTLIGHT_COGNEE_ROOT:-$HOME/.cognee-spotlight/profiles/$PROFILE/cognee}"
fi
if [ -f "$APP_CONFIG" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$APP_CONFIG"
  set +a
fi

MODE="${COGNEE_MODE:-fake}"
if [ "$MODE" = "local" ]; then
  # Force-isolate cognee's storage from any other cognee install on this
  # machine — sharing a database with a different cognee version fails at
  # migration time, and shells often export *_ROOT_DIRECTORY globally for a
  # dev checkout. Override deliberately with SPOTLIGHT_COGNEE_ROOT if needed.
  COGNEE_ROOT="${SPOTLIGHT_COGNEE_ROOT:-$HOME/.cognee-spotlight/cognee}"
  export DATA_ROOT_DIRECTORY="$COGNEE_ROOT/data"
  export SYSTEM_ROOT_DIRECTORY="$COGNEE_ROOT/system"
  export CACHE_ROOT_DIRECTORY="$COGNEE_ROOT/cache"
  # cognee's session memory rewrites every query through the LLM before the
  # vector search — right for a chat agent, wrong for search-as-you-type
  # (adds seconds of latency per keystroke). Forced off (shells often export
  # CACHING=true globally for dev); override with SPOTLIGHT_COGNEE_CACHING.
  export CACHING="${SPOTLIGHT_COGNEE_CACHING:-false}"
  # Single-user posture: with multi-tenant access control on (cognee's
  # default), every search opens and tears down a per-dataset DB worker
  # (~4s/query); single-user mode keeps one warm engine (~0.25s/query).
  # This backend is one person's index — teammates share via the hub.
  export ENABLE_BACKEND_ACCESS_CONTROL=false
  uv sync --extra local --extra docs
else
  uv sync
fi

exec uv run python -m spotlight_backend
