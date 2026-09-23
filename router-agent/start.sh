#!/bin/sh
# router-start — run the show-only agent beside FRR, then hand off to the
# image's own entrypoint.
#
# The agent runs as the `frr` user, not root. Measured on
# quay.io/frrouting/frr:10.7.1: there is no su-exec, /bin/setpriv is BusyBox
# and has no --reuid, and BusyBox chroot has no --userspec — but
# `su -s /bin/sh frr -c` works, and vtysh as that uid returns rc=0 with JSON
# because /var/run/frr is frr:frr and frr is in the frrvty group. There is no
# fallback to root: if the drop fails the agent does not start.
set -eu

if [ -z "${FRR_AGENT_ADDR:-}" ]; then
  echo "router-start: FRR_AGENT_ADDR is required (the management address)" >&2
  exit 1
fi

su -s /bin/sh frr -c '/usr/local/bin/frr-agent' &

exec /sbin/tini -- /usr/lib/frr/docker-start
