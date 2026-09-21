# CI evidence

Four FRR routers — two companies multi-homed to two ISPs — plus the live
dashboard. The topology is the one in the README: companyA and companyB each
peer with ISP1 and ISP2; the ISPs peer with each other and transit
`10.1.1.0/24` (companyA) and `192.168.1.0/24` (companyB).

## Bring-up (no containerlab)

```bash
docker compose -f compose/docker-compose.yml up -d --wait
# or: ci/up.sh
```

Dashboard: http://127.0.0.1:8089

Routers: `quay.io/frrouting/frr:10.7.1` — the pin in
`compose/docker-compose.yml`, which is what this run brought up.

## Screenshots

### 01-steady

The dashboard at rest: four routers, eBGP sessions Established.

![01-steady](https://raw.githubusercontent.com/ephico2real2/bgp-lab-with-dashboard/ci-captures/2026-09-21T0727Z_lab-ci_run35572869913-1/01-steady.png)

### 02-sessions-down

After an administrative shutdown of isp1's sessions. The graph shows the drop
and the events pane reads `Idle (Admin)`.

This picture is deliberately NOT `clear bgp *`: a clear drops and
re-establishes inside about 2 s, and a 2 s poller cannot be relied on to
photograph a 2 s transient — one run caught it and the next polled straight
past it. A shutdown holds the sessions down until they are released, so the
picture is of a known state. The blog's own `clear bgp *` still runs, after
the recovery, for the events pane.

![02-sessions-down](https://raw.githubusercontent.com/ephico2real2/bgp-lab-with-dashboard/ci-captures/2026-09-21T0727Z_lab-ci_run35572869913-1/02-sessions-down.png)

### 03-recovered

Sessions Established again after isp1's peers come back.

![03-recovered](https://raw.githubusercontent.com/ephico2real2/bgp-lab-with-dashboard/ci-captures/2026-09-21T0727Z_lab-ci_run35572869913-1/03-recovered.png)

## Check

```

== simple-lab — four FRR routers, two companies multi-homed to two ISPs
  STATUS WHAT                                                                   MEASURED                                             RULE
  PASS   four routers running                                                   running=4/4                                          container_name clab-simple-lab-<node> running
  PASS   eBGP sessions Established                                              10/10 Established                                    every neighbor remote-as in configs/*/frr.conf
  PASS   config addresses on named ifaces                                       companya:eth1=10.0.10.1 companya:eth2=10.0.11.1 companyb:eth1=10.0.30.1 companyb:eth2=10.0.31.1 isp1:eth1=10.0.10.2 isp1:eth2=10.0.20.1 isp1:eth3=10.0.30.2 isp2:eth1=10.0.11.2 isp2:eth2=10.0.20.2 isp2:eth3=10.0.31.2  interface + ip address in configs/*/frr.conf
  PASS   10.1.1.0/24 in companyb                                                prefix=10.1.1.0/24 paths=2                           companya network 10.1.1.0/24 in configs/companya/frr.conf
  PASS   192.168.1.0/24 in companya                                             prefix=192.168.1.0/24 paths=2                        companyb network 192.168.1.0/24 in configs/companyb/frr.conf
  PASS   dashboard /api/state ready                                             ready=True nodes=companya,isp1,isp2,companyb asns=65001,65100,65200,65002 GET http://127.0.0.1:8089/api/state ready=true
  PASS   dashboard nodes and ASNs                                               ready=True nodes=companya,isp1,isp2,companyb asns=65001,65100,65200,65002 nodes=companya,isp1,isp2,companyb asns=65001,65100,65200,65002

simple-lab check: 0 FAIL
```

## Timings

```
DOM connected+#graph canvas: 0.1 s
graph four nodes (/api/state): 0.0 s
wrote ci/out/screenshots/01-steady.png
administrative shutdown of clab-simple-lab-isp1's 3 sessions
dashboard shows a non-Established session: 1.0 s
wrote ci/out/screenshots/02-sessions-down.png
releasing clab-simple-lab-isp1's sessions
dashboard sessions recovered: 2.0 s
clear bgp * on clab-simple-lab-isp1 (the blog's demo; events only)
wrote ci/out/screenshots/03-recovered.png
```

## How this was produced

- run: https://github.com/ephico2real2/bgp-lab-with-dashboard/actions/runs/35572869913
- commit: `43a409adb1a1a6d13172d97b65b4c0e5a8e9c355`
- dashboard image digest: `sha256:f14331040255f646d287370ddaa3ff6176043700dc898643310eaa193b386714`
- rendered by `ci/mkdoc.sh` from that run's check table, timings and screenshot URLs
