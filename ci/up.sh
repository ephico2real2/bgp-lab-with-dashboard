#!/usr/bin/env bash
# Bring the compose lab up and wait until every expected eBGP peer is Established.
# Deadline 90 s, poll every 2 s. Prints "converged after N s (M polls)" or the
# last summary and exits 1.
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=lib.sh disable=SC1091
. ./lib.sh
cd "$ROOT"

DEADLINE="${DEADLINE:-90}"
POLL="${POLL:-2}"

# A router can lose its start-up race: run 35533725780 (PR #19) had
# clab-simple-lab-isp1 exit 141 (SIGPIPE) while the other three came up
# healthy, and compose then refused the dashboard's dependency. One retry,
# with the evidence printed first, so a repeat is diagnosable instead of a
# bare "dependency failed to start".
up_once() { compose up -d --wait --wait-timeout 120; }
if ! up_once; then
  echo "--- the lab did not come up; state and logs of what failed ---" >&2
  compose ps -a >&2 || true
  for c in $(compose ps -a --format '{{.Name}} {{.State}}' 2>/dev/null | awk '$2 != "running" {print $1}'); do
    echo "--- $c ---" >&2
    docker inspect "$c" --format 'exit={{.State.ExitCode}} oom={{.State.OOMKilled}} err={{.State.Error}}' >&2 || true
    docker logs --tail 40 "$c" >&2 || true
  done
  echo "--- retrying once (recreate) ---" >&2
  compose up -d --force-recreate --wait --wait-timeout 120
  echo "the lab needed a retry: one or more containers failed their first start" >&2
fi


expect_n=$(python3 "$BGP_PY" expected-peers --root "$ROOT" | wc -l | tr -d ' ')
if [ "${expect_n:-0}" -eq 0 ]; then
  echo "derived 0 neighbors from configs/; nothing to wait for" >&2
  exit 1
fi

start=$(date +%s)
polls=0
last=""
while true; do
  now=$(date +%s)
  elapsed=$((now - start))
  polls=$((polls + 1))
  all_ok=1
  last=""
  while IFS=$'\t' read -r node _ip _asn; do
    [ -n "$node" ] || continue
    peers=()
    while IFS=$'\t' read -r n ip _a; do
      [ "$n" = "$node" ] || continue
      peers+=("$ip")
    done < <(python3 "$BGP_PY" expected-peers --root "$ROOT")
    name=$(container_name "$node")
    raw=""
    rc=0
    raw=$(vtysh_json "$name" "show bgp summary json" 2>&1) || rc=$?
    if [ "$rc" -ne 0 ]; then
      all_ok=0
      last="${last}${name}: vtysh rc=${rc} ${raw}"$'\n'
      continue
    fi
    measured=""
    prc=0
    measured=$(printf '%s' "$raw" | python3 "$BGP_PY" summary-require "${peers[@]}") || prc=$?
    last="${last}${name}: ${measured}"$'\n'
    if [ "$prc" -ne 0 ]; then
      all_ok=0
    fi
  done < <(python3 "$BGP_PY" asns --root "$ROOT")
  if [ "$all_ok" -eq 1 ]; then
    echo "converged after ${elapsed} s (${polls} polls)"
    printf '%s' "$last"
    exit 0
  fi
  if [ "$elapsed" -ge "$DEADLINE" ]; then
    echo "not converged after ${elapsed} s (${polls} polls)" >&2
    printf '%s' "$last" >&2
    exit 1
  fi
  sleep "$POLL"
done
