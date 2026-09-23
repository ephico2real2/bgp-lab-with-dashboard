#!/bin/sh
# router-start — the image's own CMD: the show-only agent beside FRR, then the
# image's entrypoint. containerlab runs this (it sets no `cmd`), compose does
# not (it runs ci/rename-ifaces.sh, which calls the same agent script).
#
# This file exists because the agent has to start on BOTH paths. It did not:
# the Containerfile installed it and set no CMD, so the image kept FRR's own
# `/usr/lib/frr/docker-start` and `clab deploy -t simple.clab.yml` produced four
# routers with no agent and a dashboard that could read none of them.
set -eu

/usr/local/bin/frr-agent-start

exec /usr/lib/frr/docker-start
