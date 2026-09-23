#!/bin/sh
# frr-agent-start — bring up the show-only agent beside FRR, or bring up
# nothing. One script, because there are two ways a router in this repo starts
# (compose runs ci/rename-ifaces.sh, containerlab runs the image's own CMD) and
# a security property that only one of them applies is not a property.
#
# It runs the agent as the `frr` user, not root. Measured on
# quay.io/frrouting/frr:10.7.1: there is no su-exec, /bin/setpriv is BusyBox
# and has no --reuid, and BusyBox chroot has no --userspec — but
# `su -s /bin/sh frr -c` works, and vtysh as that uid returns rc=0 with JSON
# because /var/run/frr is frr:frr and frr is in the frrvty group. There is no
# fallback to root: if the drop fails the agent does not start.
set -u

AGENT=/usr/local/bin/frr-agent

if [ -z "${FRR_AGENT_ADDR:-}" ]; then
  echo "frr-agent-start: FRR_AGENT_ADDR is unset; starting FRR with NO agent" >&2
  exit 0
fi
if [ ! -x "$AGENT" ]; then
  echo "frr-agent-start: ${AGENT} is missing; starting FRR with NO agent" >&2
  exit 0
fi
if [ -z "${FRR_AGENT_ALLOW:-}" ]; then
  echo "frr-agent-start: FRR_AGENT_ALLOW is unset; starting FRR with NO agent." >&2
  echo "frr-agent-start: binding to a management address is not a boundary —" >&2
  echo "frr-agent-start: a host on a transit link with a route to the management" >&2
  echo "frr-agent-start: subnet reaches it. Name the networks that may ask." >&2
  exit 0
fi

# Second layer, when the image has iptables: the kernel drops a packet for the
# agent's port that did not arrive on the management interface. The agent's own
# allow-list is the enforced boundary and works without this; this makes the
# refusal free and moves it off the router's user space.
AGENT_PORT=${FRR_AGENT_ADDR##*:}
MGMT_IF=${FRR_AGENT_MGMT_IF:-eth0}
if command -v iptables >/dev/null 2>&1; then
  if iptables -I INPUT 1 -p tcp --dport "${AGENT_PORT}" -i "${MGMT_IF}" -j ACCEPT 2>/dev/null \
     && iptables -A INPUT -p tcp --dport "${AGENT_PORT}" -j DROP 2>/dev/null; then
    echo "frr-agent-start: tcp/${AGENT_PORT} accepted on ${MGMT_IF} only"
  else
    echo "frr-agent-start: could not install the ${AGENT_PORT} rules (no NET_ADMIN?);" \
         "FRR_AGENT_ALLOW is the boundary" >&2
  fi
else
  echo "frr-agent-start: no iptables in this image; FRR_AGENT_ALLOW is the boundary"
fi

# Supervised. watchfrr supervises `mgmtd zebra bgpd staticd` and nothing else,
# the container healthcheck asks vtysh and nothing else, and the restart policy
# is `no` — so an agent that died left a container reporting healthy and a
# dashboard reporting the router unreachable, for ever. Measured: SIGSTOP on
# the agent, container health `healthy`, isp1 `unreachable` on /api/state.
(
  while true; do
    su -s /bin/sh frr -c "$AGENT"
    rc=$?
    echo "frr-agent-start: agent exited rc=${rc}; restarting in 2s" >&2
    sleep 2
  done
) &

echo "frr-agent-start: agent supervised on ${FRR_AGENT_ADDR}, from ${FRR_AGENT_ALLOW}"
