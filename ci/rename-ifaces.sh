#!/bin/bash
# Map Docker-assigned addresses onto the interface NAMES in /etc/frr/frr.conf.
# Compose does not preserve attachment order (measured: name-sort, so companya's
# eth0 was 10.0.10.1 and FRR then put 10.0.10.1/30 on the wrong L2). We never
# renumber — we rename the iface that already has the config address.
set -euo pipefail

CONF="${FRR_CONF:-/etc/frr/frr.conf}"
if [ ! -r "$CONF" ]; then
  echo "rename-ifaces: no ${CONF}; starting FRR as-is" >&2
  exec /usr/lib/frr/docker-start
fi

current_eth() {
  ip -o link show | awk -F': ' '{print $2}' | cut -d@ -f1 | grep -E '^eth[0-9]+$' | sort || true
}

n=0
while IFS= read -r iface; do
  [ -n "$iface" ] || continue
  ip link set "$iface" down
  ip link set "$iface" name "tmp${n}"
  ip link set "tmp${n}" up
  n=$((n + 1))
done < <(current_eth)

while read -r want addr; do
  [ -n "${want:-}" ] || continue
  cur=$(ip -4 -o addr show | awk -v a="$addr" '$4 ~ "^"a"/" { gsub(/@.*/, "", $2); print $2; exit }')
  if [ -z "${cur:-}" ]; then
    echo "rename-ifaces: no iface has ${addr} (wanted ${want})" >&2
    continue
  fi
  if [ "$cur" = "$want" ]; then
    echo "rename-ifaces: ${want} already has ${addr}"
    continue
  fi
  ip link set "$cur" down
  ip link set "$cur" name "$want"
  ip link set "$want" up
  echo "rename-ifaces: ${cur} (${addr}) -> ${want}"
done < <(awk '
  /^interface / { iface = $2; next }
  iface != "" && iface != "lo" && $1 == "ip" && $2 == "address" {
    split($3, a, "/")
    print iface, a[1]
  }
' "$CONF")

if ! ip link show eth0 >/dev/null 2>&1; then
  leftover=$(ip -o link show | awk -F': ' '{print $2}' | cut -d@ -f1 | grep -E '^tmp[0-9]+$' | head -n 1 || true)
  if [ -n "${leftover:-}" ]; then
    ip link set "$leftover" down
    ip link set "$leftover" name eth0
    ip link set eth0 up
    echo "rename-ifaces: ${leftover} (mgmt) -> eth0"
  fi
fi

exec /usr/lib/frr/docker-start
