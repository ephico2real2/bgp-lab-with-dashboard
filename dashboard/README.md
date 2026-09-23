# BGP Lab Dashboard

A live web dashboard for the simple-lab. Polls every FRR router every 2 seconds via `docker exec ... vtysh -c "show ip bgp ... json"` and pushes diffs to your browser over a WebSocket.

## What you see

- **Topology graph** — one node per router, one edge per BGP session. Edge color = session state (green Established, amber transitioning, red down).
- **Sidebar** — click any node to see its BGP summary, neighbors, and full BGP table including communities.
- **Event log** — streaming list of session up/down and best-path changes.

![alt text](bgp-lab-dashboard.png)


## How it deploys with the lab

The dashboard is wired into `simple.clab.yml` as a regular clab node:

```yaml
nodes:
  companya: {...}
  isp1:     {...}
  isp2:     {...}
  companyb: {...}
  dashboard:
    kind: linux
    image: bgp-dashboard:latest
    ports:
      - 8088:8080                                    # host:container
    env:
      LAB_TOPOLOGY: /lab/topology.yml
      LAB_PREFIX: clab-simple-lab
    binds:
      - simple.clab.yml:/lab/topology.yml:ro         # the router names
      - configs:/lab/configs:ro                      # the configs, for reference
```

So `sudo clab deploy -t simple.clab.yml` brings up all 5 containers (4 routers + dashboard) in one shot. `sudo clab destroy -t simple.clab.yml` tears all 5 down.

The only thing clab does **not** do is build the dashboard image — clab assumes images already exist. So the first-time workflow is:

```bash
# 1. Build the image. Only needed once, or whenever you change dashboard code.
cd dashboard/
sudo docker build -t bgp-dashboard:latest .

# 2. Deploy lab + dashboard together.
cd ..
sudo clab deploy -t simple.clab.yml

# 3. Open the UI
#    http://<host>:8088
```

The image is ~150 MB (Python slim + FastAPI + docker SDK). After the first build, redeploys are fast since the image is cached.

## Restarting just the dashboard (without disturbing the lab)

clab is all-or-nothing — it has no per-node redeploy. If you change dashboard code and want to see it live without tearing down BGP state, bypass clab and use `docker run` directly:

```bash
sudo docker rm -f clab-simple-lab-dashboard
sudo docker run -d --name clab-simple-lab-dashboard \
  --network clab \
  -p 8088:8080 \
  -e LAB_PREFIX=clab-simple-lab \
  -e ROUTERS="companya=http://172.22.20.11:8080,companyb=http://172.22.20.12:8080,isp1=http://172.22.20.13:8080,isp2=http://172.22.20.14:8080" \
  -v simple.clab.yml:/lab/topology.yml:ro \
  -v /configs:/lab/configs:ro \
  bgp-dashboard:latest
```

(Adjust the host paths to wherever your config lives.) The next `clab destroy + deploy` will then pick up the dashboard normally again.

## Running against a different lab

The dashboard isn't hard-coded to the simple-lab. To point it at the original 6-node lab (or any other), set `LAB_PREFIX` to that lab's container prefix and bind its topology YAML:

```bash
sudo docker run -d --name bgp-dashboard \
  --network clab \
  -p 8088:8080 \
  -e LAB_PREFIX=clab-bgp-lab \
  -v /home/pc/bgp/bgp-lab.clab.yml:/lab/topology.yml:ro \
  -v /home/pc/bgp/configs:/lab/configs:ro \
  bgp-dashboard:latest
```

The dashboard holds **no Docker socket**. Each router runs a show-only agent (`router-agent/`) on the management LAN and the dashboard reads it over HTTP: five `show` commands behind a fixed allow-list, where it used to have `docker exec` into any container on the host.

`ROUTERS` names the routers and where their agents answer. Without it the topology file supplies the names and the address is the node's name on the management network — re-read every poll, so a router added to the file still appears without a restart. Losing the socket costs live *container* discovery: nothing can enumerate what is running without it, so the routers are configuration. The AS and the router-id are still read from each router's own `show ip bgp summary json`.

## How it works

```
Browser ◄── WebSocket ── FastAPI ──HTTP──► frr-agent (management LAN)
   ▲                      │
   └── HTTP on load ──────┤
       /api/state         └── 2 s polling loop ───┐
       /api/events                                ▼
                          show ip bgp summary json
                          show ip bgp detail json      (detail: communities)
                          show bgp neighbors json      (timers; non-fatal)
```

The polling loop runs as an asyncio task and sends three kinds of frame:

| frame | when | why |
|---|---|---|
| `snapshot` | on connect | the graph the page draws first |
| `state` | when the SIGNATURE changes — the sessions or the best paths | re-sending 11 KB and re-laying out a steady fabric every 2 s is churn |
| `signal` | **every tick** | what each session DID between two polls; a heartbeat that only beats on change is not a heartbeat |
| `event` | per change | `session` (appeared / state / vanished), `route` (added / withdrawn), `bestpath` |

Because `state` is sent only on a change, anything the page owes the reader
*later* — the 30 s hold on a vanished edge — is a timer in the page, not a
wait for the next frame.

The page loads over HTTP first (`/api/state`, then `/api/events?since=0`) and
only then opens the socket, so a slow or blocked WebSocket shows a drawn page
rather than an empty one. Events carry monotonic ids and the process's
`epoch`, which is what lets a reconnecting page ask for just the gap and draw
a twice-delivered event once.

## Files

| Path | Role |
|---|---|
| `Dockerfile` | python:3.12-slim base + FastAPI + docker SDK |
| `requirements.txt` | pinned deps |
| `app/main.py` | FastAPI app: `/`, `/api/state`, `/api/events`, `/api/version`, `/ws` |
| `app/poller.py` | async polling, state diff, event generation |
| `app/static/index.html` | shell layout |
| `app/static/dashboard.js` | Cytoscape graph + WebSocket client |
| `app/static/styles.css` | basic styling |
| `tests/` | the poller's diff, the ring and the endpoint (pytest); the page's decisions, run in a `vm` under node |

## Extending

`/api/version` answers which build is serving the page — the same commit the
image carries as `org.opencontainers.image.revision`, because both come from
one build argument. The header shows the short form and links to the commit;
an image not built by CI says `local build` rather than inventing a number.

Environment:

| var | default | what it does |
|---|---|---|
| `ROUTERS` | (unset) | `name=url,…` — where each router's agent answers |
| `ROUTER_AGENT_PORT` | `8080` | the port, when the names come from the topology file |
| `DASHBOARD_REVISION` | `unknown` | baked in at build time; shown in the header |
| `DASHBOARD_BUILT` | `unknown` | baked in at build time; the tooltip's build date |
| `LAB_TOPOLOGY` | `/lab/topology.yml` | the containerlab YAML the node list is read from |
| `LAB_PREFIX` | `clab-simple-lab` | container names are `<prefix>-<node>` |
| `POLL_INTERVAL` | `2` | seconds between polls |
| `EVENTS_RING` | `500` | events kept for a page that arrives late |

- **Different lab**: set `LAB_PREFIX` (e.g. `LAB_PREFIX=clab-bgp-lab` for the original 6-node lab) and bind that lab's topology YAML to `/lab/topology.yml`.
- **More polling fields**: add to `poller._poll_node_sync()` (e.g. `show ip route json`).
- **Highlight best-path edges**: in `dashboard.js`, walk the BGP table for the selected node and add a class to the edges that match.

The layout does NOT re-run on a diff — `updateGraph` patches data only, so a
node you drag stays where you put it. "Reset layout" in the graph tools is
what re-runs `cose` deliberately.

## Known limits (MVP)

- "Best path" highlighting is in the table only, not on the graph yet.
- No authentication. Run on a trusted network.
- No authentication on the agents either: the management network is the boundary. What is behind it is five read-only commands, not the host.
