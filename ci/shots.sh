#!/usr/bin/env bash
# Headless Chrome screenshots of the dashboard: steady, after `clear bgp *`
# on isp1, then recovered. Waits for /api/state (not a fixed sleep). If
# Chrome is absent, says so and exits 0 — CI uses Playwright (ci/walk.js).
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=lib.sh disable=SC1091
. ./lib.sh
cd "$ROOT"

OUT="${SHOTS_DIR:-$ROOT/ci/out/screenshots}"
LOG="${SHOTS_LOG:-$ROOT/ci/out/shots.log}"
mkdir -p "$OUT" "$(dirname "$LOG")"

os=$(uname -s)
if [ "$os" = Darwin ]; then
  CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
else
  CHROME="${CHROME:-google-chrome}"
fi

if [ ! -x "$CHROME" ] && ! command -v "$CHROME" >/dev/null 2>&1; then
  echo "Chrome not found at ${CHROME}; skipping screenshots (CI uses Playwright via ci/walk.js)"
  exit 0
fi

deadline_wait() { # seconds  python-filter-on-api-state  label
  local limit=$1
  local py=$2
  local label=$3
  local start now elapsed raw rc
  start=$(date +%s)
  while true; do
    now=$(date +%s)
    elapsed=$((now - start))
    raw=""
    rc=0
    raw=$(curl -fsS --max-time 3 "${DASHBOARD_URL}/api/state" 2>&1) || rc=$?
    if [ "$rc" -eq 0 ]; then
      if printf '%s' "$raw" | python3 -c "$py"; then
        echo "${label}: ${elapsed} s" | tee -a "$LOG"
        return 0
      fi
    fi
    if [ "$elapsed" -ge "$limit" ]; then
      echo "${label}: timed out after ${elapsed} s (curl rc=${rc})" | tee -a "$LOG" >&2
      printf '%s\n' "${raw:-no body}" | tee -a "$LOG" >&2
      return 1
    fi
    sleep 1
  done
}

shot() {
  local dest=$1
  "$CHROME" --headless=new --disable-gpu --no-first-run --hide-scrollbars \
    --window-size=1400,900 \
    --screenshot="$dest" \
    "${DASHBOARD_URL}/" >/dev/null 2>&1
  echo "wrote ${dest} ($(wc -c < "$dest" | tr -d ' ') bytes)" | tee -a "$LOG"
}

: > "$LOG"

ready_py='
import json, sys
d = json.load(sys.stdin)
nodes = d.get("nodes") or []
data = d.get("data") or {}
if d.get("ready") is not True: raise SystemExit(1)
if len(nodes) < 4: raise SystemExit(1)
if len(data) < 4: raise SystemExit(1)
'

down_py='
import json, sys
sys.path.insert(0, "'"$ROOT"'/ci")
from bgp import peers_from, state_of
d = json.load(sys.stdin)
blob = d.get("data") or {}
other = 0
for nd in blob.values():
    if not isinstance(nd, dict):
        continue
    for peer in peers_from(nd.get("summary") or {}).values():
        if state_of(peer) != "Established":
            other += 1
if other == 0:
    raise SystemExit(1)
'

up_py='
import json, sys
sys.path.insert(0, "'"$ROOT"'/ci")
from bgp import peers_from, state_of
d = json.load(sys.stdin)
blob = d.get("data") or {}
est = other = 0
for nd in blob.values():
    if not isinstance(nd, dict):
        continue
    for peer in peers_from(nd.get("summary") or {}).values():
        if state_of(peer) == "Established":
            est += 1
        else:
            other += 1
if other != 0 or est == 0:
    raise SystemExit(1)
'

deadline_wait 60 "$ready_py" "dashboard ready (4 nodes)"
sleep 2
shot "$OUT/01-steady.png"

isp1=$(container_name isp1)
echo "clear bgp * on ${isp1}" | tee -a "$LOG"
docker exec "$isp1" vtysh -c "clear bgp *"

deadline_wait 60 "$down_py" "dashboard shows a non-Established session"
shot "$OUT/02-clear-bgp.png"

deadline_wait 90 "$up_py" "dashboard sessions recovered"
sleep 2
shot "$OUT/03-recovered.png"
