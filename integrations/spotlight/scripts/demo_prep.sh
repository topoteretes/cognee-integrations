#!/usr/bin/env bash
# One-shot demo prep: both profiles' backends up, app running, health checklist.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ok() { printf "  ✅ %s\n" "$1"; }
bad() { printf "  ❌ %s\n" "$1"; }

start_backend() { # $1 profile, $2 port
  if ! curl -s -m 2 "localhost:$2/health" > /dev/null 2>&1; then
    SPOTLIGHT_PROFILE="$1" SPOTLIGHT_SEARCH_CACHE_TTL=7200 nohup ./scripts/run_backend.sh \
      > "/tmp/spotlight-backend-$1.log" 2>&1 &
    for _ in $(seq 1 45); do
      sleep 2
      curl -s -m 2 "localhost:$2/health" > /dev/null 2>&1 && break
    done
  fi
}

echo "Cognee Spotlight demo prep"
echo "──────────────────────────"

start_backend default 8765
start_backend boris 8766

H1=$(curl -s -m 5 localhost:8765/health || true)
H2=$(curl -s -m 5 localhost:8766/health || true)
echo "$H1" | grep -q '"mode":"cloud"' && ok "vasilije backend (8765, cloud)" || bad "vasilije backend: $H1"
echo "$H2" | grep -q '"user":"boris"' && ok "boris backend (8766, cloud)" || bad "boris backend: $H2"

# tenant reachable with the configured key
set -a; source "$HOME/.cognee-spotlight/backend.env" 2>/dev/null; set +a
CODE=$(curl -s -m 15 -o /dev/null -w '%{http_code}' -L "$COGNEE_CLOUD_URL/health" 2>/dev/null || echo 000)
[ "$CODE" = "200" ] && ok "tenant reachable ($COGNEE_CLOUD_URL)" || bad "tenant health HTTP $CODE"

# semantic search sanity — run twice: the first query after a backend start
# is a cold start on the tenant and often exceeds the panel's budget
curl -s -m 30 'localhost:8765/search?q=where%20do%20competitors%20threaten%20us' > /dev/null || true
R=$(curl -s -m 30 'localhost:8765/search?q=where%20do%20competitors%20threaten%20us' || true)
echo "$R" | grep -q 'competitor_landscape' && ok "semantic search returns file results (warm)" \
  || bad "semantic search empty — tenant may still be cognifying; retry in a few minutes"
# pre-warm every scripted demo query into the response cache (2h TTL), so
# the live demo never waits on the tenant
curl -s -m 120 'localhost:8765/search?q=where%20do%20our%20competitors%20threaten%20us%20most' > /dev/null 2>&1 || true
curl -s -m 180 "localhost:8765/search?q=who%20are%20Meridian%27s%20main%20competitors%20and%20where%20are%20we%20exposed%3F&mode=answer" > /dev/null 2>&1 || true
curl -s -m 180 'localhost:8765/search?q=what%20did%20we%20change%20to%20make%20search%20fast&mode=answer' > /dev/null 2>&1 || true
curl -s -m 180 'localhost:8766/search?q=why%20is%20spotlight%20search%20fast%20now&mode=answer' > /dev/null 2>&1 || true
curl -s -m 30 'localhost:8766/search?q=spotlight' > /dev/null 2>&1 || true
curl -s -m 60 'localhost:8765/graph?max_nodes=300' > /dev/null 2>&1 && ok "knowledge graph page renders" || bad "graph page failed"

# anonymization guard: scripted answer queries must never surface real brands
A=$(curl -s -m 90 'localhost:8765/search?q=who%20are%20our%20main%20competitors&mode=answer' || true)
echo "$A" | grep -qiE 'trivago|booking|expedia|kayak|bayer' \
  && bad "REAL BRAND LEAKED into answers — do not demo; ping the assistant" \
  || ok "anonymization check: answers are fictional-only"

# the app itself
if ! pgrep -x CogneeSpotlight > /dev/null; then
  open "$ROOT/macos/dist/Cognee Spotlight.app"
  sleep 3
fi
pgrep -x CogneeSpotlight > /dev/null && ok "app running (⌥Space)" || bad "app not running"

echo
echo "Rehearse with DEMO.md. Remember: mark both inboxes read before the client walks in."
