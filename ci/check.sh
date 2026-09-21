#!/usr/bin/env bash
# PASS/FAIL rows for the compose lab. Exit = FAIL count. At most 12 rows.
# A dead docker/vtysh/curl is a FAIL, never a PASS. Expected sessions and
# originated prefixes are read from configs/, not hardcoded.
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=lib.sh disable=SC1091
. ./lib.sh
cd "$ROOT"

fails=0
row() { # ok|fail  what  measured  rule
  local st
  case "$1" in
    ok)   st=PASS ;;
    fail) st=FAIL; fails=$((fails + 1)) ;;
    *)    st=$1 ;;
  esac
  printf '  %-6s %-70s %-52s %s\n' "$st" "$2" "$3" "$4"
}

printf '\n== simple-lab — four FRR routers, two companies multi-homed to two ISPs\n'
printf '  %-6s %-70s %-52s %s\n' STATUS WHAT MEASURED RULE

# 1. four routers running
nodes=()
while IFS= read -r n; do
  [ -n "$n" ] && nodes+=("$n")
done < <(lab_nodes)
want=${#nodes[@]}
ps_out=""
ps_rc=0
ps_out=$(compose ps --format '{{.Name}} {{.State}}' 2>&1) || ps_rc=$?
if [ "$ps_rc" -ne 0 ]; then
  row fail "four routers running" "docker failed: $(printf '%s' "$ps_out" | tr '\n' ' ' | head -c 60)" \
    "compose ps running=${want}"
else
  n=0
  missing=""
  for node in "${nodes[@]}"; do
    name=$(container_name "$node")
    if printf '%s\n' "$ps_out" | grep -Eq "^${name} running"; then
      n=$((n + 1))
    else
      missing="${missing}${name} "
    fi
  done
  if [ "$n" -eq "$want" ]; then
    row ok "four routers running" "running=${n}/${want}" \
      "container_name ${LAB_PREFIX}-<node> running"
  else
    row fail "four routers running" "running=${n}/${want} missing=${missing}" \
      "container_name ${LAB_PREFIX}-<node> running"
  fi
fi

# 2. every eBGP session Established (pairs from the configs)
expect_n=0
got_n=0
sess_err=""
while IFS=$'\t' read -r node ip _asn; do
  [ -n "$node" ] || continue
  expect_n=$((expect_n + 1))
  name=$(container_name "$node")
  raw=""
  rc=0
  raw=$(vtysh_json "$name" "show bgp summary json" 2>&1) || rc=$?
  if [ "$rc" -ne 0 ]; then
    sess_err="${sess_err}${name}:vtysh-rc=${rc} "
    continue
  fi
  measured=""
  prc=0
  measured=$(printf '%s' "$raw" | python3 "$BGP_PY" summary-require "$ip") || prc=$?
  if [ "$prc" -eq 0 ]; then
    got_n=$((got_n + 1))
  else
    sess_err="${sess_err}${name}:${measured} "
  fi
done < <(python3 "$BGP_PY" expected-peers --root "$ROOT")
if [ "$expect_n" -eq 0 ]; then
  row fail "eBGP sessions Established" "derived 0 neighbors from configs/" \
    "neighbor remote-as lines in configs/*/frr.conf"
elif [ -z "$sess_err" ] && [ "$got_n" -eq "$expect_n" ]; then
  row ok "eBGP sessions Established" "${got_n}/${expect_n} Established" \
    "every neighbor remote-as in configs/*/frr.conf"
else
  row fail "eBGP sessions Established" "${got_n}/${expect_n} ${sess_err}" \
    "every neighbor remote-as in configs/*/frr.conf"
fi

# 3. config addresses sit on the named ifaces (rename-ifaces.sh; never renumber)
iface_ok=1
iface_msg=""
while IFS=$'\t' read -r node iface addr; do
  [ -n "$node" ] || continue
  name=$(container_name "$node")
  raw=""
  rc=0
  raw=$(docker exec "$name" ip -4 -o addr show "$iface" 2>&1) || rc=$?
  if [ "$rc" -ne 0 ]; then
    iface_ok=0
    iface_msg="${iface_msg}${node}:${iface}:ip-rc=${rc} "
    continue
  fi
  if printf '%s' "$raw" | grep -Eq "[ :]${addr}/"; then
    iface_msg="${iface_msg}${node}:${iface}=${addr} "
  else
    iface_ok=0
    iface_msg="${iface_msg}${node}:${iface}:missing-${addr} "
  fi
done < <(python3 "$BGP_PY" ifaces --root "$ROOT")
if [ -z "$iface_msg" ]; then
  iface_ok=0
  iface_msg="derived 0 interface addresses from configs/"
fi
if [ "$iface_ok" -eq 1 ]; then
  row ok "config addresses on named ifaces" "$iface_msg" \
    "interface + ip address in configs/*/frr.conf"
else
  row fail "config addresses on named ifaces" "$iface_msg" \
    "interface + ip address in configs/*/frr.conf"
fi

# 4–5. originated prefixes (from the configs) present in the other company
# companya originates 10.1.1.0/24; companyb originates 192.168.1.0/24 — asserted
# only when the config actually has a `network` statement.
origin_rows=0
while IFS=$'\t' read -r origin pfx; do
  [ -n "$origin" ] || continue
  for node in "${nodes[@]}"; do
    [ "$node" != "$origin" ] || continue
    case "$node" in
      isp*) continue ;;
    esac
    origin_rows=$((origin_rows + 1))
    name=$(container_name "$node")
    raw=""
    rc=0
    raw=$(vtysh_json "$name" "show ip bgp json" 2>&1) || rc=$?
    if [ "$rc" -ne 0 ]; then
      row fail "${pfx} in ${node}" "vtysh rc=${rc}" \
        "${origin} network ${pfx} in configs/${origin}/frr.conf"
      continue
    fi
    measured=""
    prc=0
    measured=$(printf '%s' "$raw" | python3 "$BGP_PY" table-has "$pfx") || prc=$?
    if [ "$prc" -eq 0 ]; then
      row ok "${pfx} in ${node}" "$measured" \
        "${origin} network ${pfx} in configs/${origin}/frr.conf"
    else
      row fail "${pfx} in ${node}" "$measured" \
        "${origin} network ${pfx} in configs/${origin}/frr.conf"
    fi
  done
done < <(python3 "$BGP_PY" originated --root "$ROOT")
if [ "$origin_rows" -eq 0 ]; then
  row fail "originated prefixes in the other company" "configs have no network statements" \
    "network lines in configs/*/frr.conf"
fi

# 5. dashboard /api/state ready: true
dash_raw=""
dash_rc=0
dash_raw=$(curl -fsS --max-time 5 "${DASHBOARD_URL}/api/state" 2>&1) || dash_rc=$?
if [ "$dash_rc" -ne 0 ]; then
  row fail "dashboard /api/state ready" "curl rc=${dash_rc}" \
    "GET ${DASHBOARD_URL}/api/state ready=true"
else
  measured=""
  prc=0
  measured=$(printf '%s' "$dash_raw" | python3 "$BGP_PY" dashboard --expect-ready) || prc=$?
  if [ "$prc" -eq 0 ]; then
    row ok "dashboard /api/state ready" "$measured" \
      "GET ${DASHBOARD_URL}/api/state ready=true"
  else
    row fail "dashboard /api/state ready" "$measured" \
      "GET ${DASHBOARD_URL}/api/state ready=true"
  fi
fi

# 6. node list has the four routers with their ASNs (topology order, ASNs from configs)
want_nodes=""
want_asns=""
while IFS= read -r node; do
  [ -n "$node" ] || continue
  asn=$(python3 "$BGP_PY" asns --root "$ROOT" | awk -v n="$node" -F '\t' '$1==n {print $2}')
  want_nodes="${want_nodes}${want_nodes:+,}${node}"
  want_asns="${want_asns}${want_asns:+,}${asn}"
done < <(lab_nodes)
if [ "$dash_rc" -ne 0 ]; then
  row fail "dashboard nodes and ASNs" "curl rc=${dash_rc}" \
    "nodes=${want_nodes} asns=${want_asns}"
else
  measured=""
  prc=0
  measured=$(printf '%s' "$dash_raw" | python3 "$BGP_PY" dashboard \
    --expect-nodes "$want_nodes" --expect-asns "$want_asns") || prc=$?
  if [ "$prc" -eq 0 ]; then
    row ok "dashboard nodes and ASNs" "$measured" \
      "nodes=${want_nodes} asns=${want_asns}"
  else
    row fail "dashboard nodes and ASNs" "$measured" \
      "nodes=${want_nodes} asns=${want_asns}"
  fi
fi

# 7. the event history a reloading page catches up from. Two calls, because
# `since` is the whole point: one for the ring, one for the gap.
ev_raw=""
ev_rc=0
ev_raw=$(curl -fsS --max-time 5 "${DASHBOARD_URL}/api/events?since=0" 2>&1) || ev_rc=$?
if [ "$ev_rc" -ne 0 ]; then
  row fail "dashboard /api/events history" "curl rc=${ev_rc}" \
    "GET ${DASHBOARD_URL}/api/events?since=0 ready=true, ids increasing, RFC 3339 stamps"
else
  measured=""
  prc=0
  measured=$(printf '%s' "$ev_raw" | python3 "$BGP_PY" events --expect-ready --min-events 1) || prc=$?
  if [ "$prc" -eq 0 ]; then
    row ok "dashboard /api/events history" "$measured" \
      "GET ${DASHBOARD_URL}/api/events?since=0 ready=true, ids increasing, RFC 3339 stamps"
  else
    row fail "dashboard /api/events history" "$measured" \
      "GET ${DASHBOARD_URL}/api/events?since=0 ready=true, ids increasing, RFC 3339 stamps"
  fi

  last_id=$(printf '%s' "$ev_raw" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("lastId") or 0)')
  gap_from=$(( last_id > 0 ? last_id - 1 : 0 ))
  gap_raw=""
  gap_rc=0
  gap_raw=$(curl -fsS --max-time 5 "${DASHBOARD_URL}/api/events?since=${gap_from}" 2>&1) || gap_rc=$?
  if [ "$gap_rc" -ne 0 ]; then
    row fail "dashboard /api/events since" "curl rc=${gap_rc}" \
      "GET ?since=${gap_from} returns only id ${last_id}"
  else
    measured=""
    prc=0
    measured=$(printf '%s' "$gap_raw" | python3 "$BGP_PY" events --min-events 1 --expect-first-id "$last_id") || prc=$?
    if [ "$prc" -eq 0 ]; then
      row ok "dashboard /api/events since" "$measured" \
        "GET ?since=${gap_from} returns only id ${last_id}"
    else
      row fail "dashboard /api/events since" "$measured" \
        "GET ?since=${gap_from} returns only id ${last_id}"
    fi
  fi
fi

echo
echo "simple-lab check: $fails FAIL"
exit "$fails"
