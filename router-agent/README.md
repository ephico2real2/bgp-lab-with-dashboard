# router-agent

The pinned FRR image plus a read-only HTTP agent, so the dashboard can read a
router without holding the host's Docker socket.

## Why

The dashboard used to run `docker exec <container> vtysh -c '<command>'`. That
required `/var/run/docker.sock` inside a web app with no authentication — and
that socket is the entire host: every container, every image, every volume,
and a shell in any of them.

## What it exposes

| path | runs |
|---|---|
| `GET /healthz` | nothing — liveness |
| `GET /show` | nothing — lists the view names below |
| `GET /show/bgp-summary` | `show ip bgp summary json` |
| `GET /show/bgp-detail` | `show ip bgp detail json` |
| `GET /show/bgp-neighbors` | `show bgp neighbors json` |
| `GET /show/ip-route` | `show ip route json` |
| `GET /show/interface` | `show interface brief json` |

Anything else is a 404, and every write verb is a 405. There is no
`configure`, no `clear`, no `write`.

## The property that matters

Nothing from the request reaches a command line. The URL names a **key** of a
fixed dictionary and the dictionary's **value** is what runs, so there is no
string to escape and no path for `/show/summary; reload` to become anything
but a dictionary miss.

## Environment

| var | required | what |
|---|---|---|
| `FRR_AGENT_ADDR` | yes | `host:port` to bind. Required, not defaulted — a default would put an agent on the data plane the first time someone forgot it. |
| `FRR_AGENT_TIMEOUT` | no (5) | seconds before a wedged `vtysh` is killed; the dashboard polls every 2 s and must not queue behind one. |

The agent runs as the `frr` user. There is no authentication: the management
network is the boundary, as it was before — but what is behind it is now five
read-only commands rather than the host.
