#!/bin/bash
# Map Docker-assigned addresses onto the interface NAMES in /etc/frr/frr.conf.
# Compose does not preserve attachment order (measured: name-sort, so companya's
# eth0 was 10.0.10.1 and FRR then put 10.0.10.1/30 on the wrong L2). Compose
# `priority` is rendered but does not assign ethN (measured on Compose v5.5.1:
# aaa-low at priority 100 became eth0; zzz-high at 1000 became eth1). We never
# renumber — we rename the iface that already has the config address.
#
# Pipelines under `set -o pipefail` that close early (`| head`, awk `exit`)
# become exit 141 (SIGPIPE). That is the CI failure: empty docker logs, a
# different router each run, PID 1 gone before `exec docker-start`. Every `ip`
# dump is read in full into a variable, then sliced in the shell.
set -euo pipefail

CONF="${FRR_CONF:-/etc/frr/frr.conf}"
echo "rename-ifaces: start host=$(hostname) conf=${CONF}"

if [ ! -r "$CONF" ]; then
  echo "rename-ifaces: no ${CONF}; starting FRR as-is" >&2
  exec /usr/lib/frr/docker-start
fi

die() {
  echo "rename-ifaces: FAILED: $*" >&2
  echo "rename-ifaces: links:" >&2
  ip -o link show >&2 || true
  echo "rename-ifaces: addrs:" >&2
  ip -4 -o addr show >&2 || true
  exit 1
}

ip_do() {
  echo "rename-ifaces: ip $*"
  ip "$@" || die "ip $*"
}

# Link names (no @if suffix), one per line. Full dump, then slice — no pipe.
link_names() {
  local dump line rest name
  dump=$(ip -o link show) || die "ip -o link show"
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    rest=${line#*: }
    name=${rest%%:*}
    name=${name%%@*}
    name=${name// /}
    [ -n "$name" ] && printf '%s\n' "$name"
  done <<< "$dump"
}

eth_names() {
  local n names=""
  while IFS= read -r n; do
    [[ "$n" =~ ^eth[0-9]+$ ]] || continue
    names="${names}${n}"$'\n'
  done <<< "$(link_names)"
  printf '%s' "$names"
}

first_tmp() {
  local n
  while IFS= read -r n; do
    if [[ "$n" =~ ^tmp[0-9]+$ ]]; then
      printf '%s\n' "$n"
      return 0
    fi
  done <<< "$(link_names)"
}

# Iface that currently holds IPv4 address $1 (exact " addr/" match).
iface_with_addr() {
  local addr=$1 dump line iface
  dump=$(ip -4 -o addr show) || die "ip -4 -o addr show"
  while IFS= read -r line; do
    case "$line" in
      *" ${addr}/"*)
        iface=${line#*: }
        iface=${iface%% *}
        iface=${iface%%@*}
        printf '%s\n' "$iface"
        return 0
        ;;
    esac
  done <<< "$dump"
}

echo "rename-ifaces: parking current eth* as tmpN"
n=0
eth_list=$(eth_names)
if [ -n "$eth_list" ]; then
  # sort reads the whole list; no early close.
  eth_list=$(printf '%s\n' "$eth_list" | sort)
fi
echo "rename-ifaces: eth list: $(printf '%s' "$eth_list" | tr '\n' ' ')"
while IFS= read -r iface; do
  [ -n "$iface" ] || continue
  ip_do link set "$iface" down
  ip_do link set "$iface" name "tmp${n}"
  ip_do link set "tmp${n}" up
  echo "rename-ifaces: ${iface} -> tmp${n}"
  n=$((n + 1))
done <<< "$eth_list"

wanted=$(awk '
  /^interface / { iface = $2; next }
  iface != "" && iface != "lo" && $1 == "ip" && $2 == "address" {
    split($3, a, "/")
    print iface, a[1]
  }
' "$CONF") || die "parse ${CONF}"

echo "rename-ifaces: mapping config addresses onto named ifaces"
while read -r want addr; do
  [ -n "${want:-}" ] || continue
  cur=$(iface_with_addr "$addr")
  if [ -z "${cur:-}" ]; then
    echo "rename-ifaces: no iface has ${addr} (wanted ${want})" >&2
    continue
  fi
  if [ "$cur" = "$want" ]; then
    echo "rename-ifaces: ${want} already has ${addr}"
    continue
  fi
  ip_do link set "$cur" down
  ip_do link set "$cur" name "$want"
  ip_do link set "$want" up
  echo "rename-ifaces: ${cur} (${addr}) -> ${want}"
done <<< "$wanted"

if ! ip link show eth0 >/dev/null 2>&1; then
  leftover=$(first_tmp)
  if [ -n "${leftover:-}" ]; then
    ip_do link set "$leftover" down
    ip_do link set "$leftover" name eth0
    ip_do link set eth0 up
    echo "rename-ifaces: ${leftover} (mgmt) -> eth0"
  else
    echo "rename-ifaces: no leftover tmp* to become eth0" >&2
  fi
fi

echo "rename-ifaces: done, exec docker-start"
exec /usr/lib/frr/docker-start
