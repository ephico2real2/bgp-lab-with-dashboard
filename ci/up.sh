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

compose up -d --wait --wait-timeout 120

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
