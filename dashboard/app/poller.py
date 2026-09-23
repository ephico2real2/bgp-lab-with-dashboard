import asyncio
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml


class LabPoller:
    def __init__(
        self,
        topology_path: Path,
        lab_prefix: str,
        broadcast: Callable[[dict], Awaitable[None]],
        interval: float = 2.0,
        exec_timeout: float = 5.0,
        events_ring: int = 500,
    ) -> None:
        self.topology_path = topology_path
        self.lab_prefix = lab_prefix
        self.broadcast = broadcast
        self.interval = interval
        # No Docker client. This used to be `docker.from_env()` and every read
        # was `docker exec <container> vtysh -c '<command>'`, which required
        # the host's socket inside a web app with no authentication — the whole
        # host, to read five `show` commands. Each router runs a show-only
        # agent on the management LAN instead, and this is an HTTP client.
        #
        # The timeout still matters for the same reason it did: one wedged
        # router must not stall every poll, because poll_all gathers them all.
        self.exec_timeout = exec_timeout
        # Before _load_nodes(), which asks for it: the inventory IS the agent
        # map now, so building it second meant asking an attribute that did not
        # exist yet. The container exited on startup with AttributeError.
        self._agent_urls: dict[str, str] | None = None
        self.nodes: list[dict[str, Any]] = self._load_nodes()
        self.last_state: dict[str, dict[str, Any]] = {}
        self.last_signature: Any = None
        # Events were broadcast and forgotten: a page that connected after the
        # `clear ip bgp *` — or simply reloaded — showed an empty Events pane
        # while the fabric it was watching had just reconverged. They are kept
        # here, oldest dropped first, and served over HTTP so a late page can
        # catch up on what it missed.
        self.events: deque[dict[str, Any]] = deque(maxlen=events_ring)
        self.last_event_id = 0
        # Names THIS process. Ids restart at 1 with it, and a page holding
        # ids from the previous one cannot always tell the two apart by
        # number: measured on this lab, the first poll of a fresh poller
        # issued 18 events, so a page that held 18 or fewer was told a lastId
        # that was not below its own and went on suppressing every new event
        # by an id it had marked rendered in the old process. Random rather
        # than clock-and-pid: a container restarts with the same pid.
        self.epoch = uuid.uuid4().hex

    def _load_nodes(self) -> list[dict[str, Any]]:
        """The routers to poll: what is RUNNING, ordered by the topology file.

        Two sources, each used for what it actually knows. Docker knows which
        containers exist right now, so a node added or removed after start-up
        no longer needs a dashboard restart. The topology file, when there is
        one, knows the order a reader expects to see them in — and nothing
        else: the ASN is read from the router itself on every poll now, not
        regex-parsed out of `configs/<node>/frr.conf`, where a `router bgp` in
        a VRF block or under different indentation was parsed as the router's
        own AS.
        """
        running = self._running_nodes()
        ordered = [n for n in self._topology_order() if n in running]
        ordered += sorted(running - set(ordered))
        # A lab whose containers are not up yet still draws its topology: fall
        # back to the file so the graph exists before the first one answers.
        if not ordered:
            ordered = self._topology_order()
        # Carry what each router has already told us about itself. This runs
        # every tick, and rebuilding the entries from scratch would blank the
        # measured AS and router-id on every poll — harmless for a router that
        # answers (the poll fills them straight back in) and wrong for one
        # that does not: its label would fall back to "?" the moment it went
        # unreachable, when the last thing it said is the honest thing to show.
        known = {n["name"]: n for n in getattr(self, "nodes", [])}
        return [known.get(name) or {"name": name, "asn": None} for name in ordered]

    def _running_nodes(self) -> set[str]:
        """The routers this dashboard has an address for.

        Live container discovery is gone with the Docker socket, and that is
        the trade this design makes: without the host's socket there is no way
        to enumerate what is running, so the routers are configuration. ROUTERS
        names them and where to reach them; the topology file supplies the
        names when it does not, and the address is then the node's name on the
        management network, which is what Compose and containerlab both give.
        """
        return set(self._agents())

    def _agents(self) -> dict[str, str]:
        """name -> base URL of that router's show-only agent."""
        if self._agent_urls is None:
            out: dict[str, str] = {}
            for part in (os.environ.get("ROUTERS") or "").split(","):
                part = part.strip()
                if not part:
                    continue
                name, _, url = part.partition("=")
                name, url = name.strip(), url.strip()
                if name and url:
                    out[name] = url.rstrip("/")
            self._agent_urls = out                 # env is fixed for the process
        if self._agent_urls:
            return self._agent_urls
        # No ROUTERS: the topology file names them and the address is the
        # node's name on the management network, which Compose and containerlab
        # both provide. Re-read every time rather than cached — the file is
        # mounted, so a router added to it still appears without a restart.
        # That is as much of the live inventory as survives losing the socket.
        port = os.environ.get("ROUTER_AGENT_PORT", "8080")
        return {name: f"http://{name}:{port}" for name in self._topology_order()}

    def _topology_order(self) -> list[str]:
        """The node order from the clab YAML, or nothing when there is no file.

        The file is OPTIONAL now. It is a presentation preference — which
        router a reader sees first — not the inventory.
        """
        try:
            topology = yaml.safe_load(self.topology_path.read_text())
        except (OSError, yaml.YAMLError) as exc:
            print(f"[poller] no topology file ({exc}); ordering by name")
            return []
        nodes = ((topology or {}).get("topology") or {}).get("nodes") or {}
        return [n for n in nodes if n != "dashboard"]

    async def run(self) -> None:
        while True:
            try:
                await self.poll_all()
            except Exception as exc:
                print(f"[poller] error: {exc}")
            await asyncio.sleep(self.interval)

    async def poll_all(self) -> None:
        # Re-read the inventory each tick. It is one Docker API call, and
        # without it a router added to the lab stayed invisible until someone
        # restarted the dashboard — which the README used to state as a
        # limitation rather than fix.
        nodes = await asyncio.to_thread(self._load_nodes)
        if nodes and nodes != self.nodes:
            self.nodes = nodes
        results = await asyncio.gather(
            *[asyncio.to_thread(self._poll_node_sync, n) for n in self.nodes],
            return_exceptions=True,
        )
        state: dict[str, Any] = {}
        for node, result in zip(self.nodes, results):
            if isinstance(result, BaseException):
                state[node["name"]] = {"error": repr(result)}
            else:
                state[node["name"]] = result

        # What each router said about ITSELF this poll. `/api/state` publishes
        # the node list, and a consumer of it — the page before its first
        # frame, ci/check.sh, anything else — should get the AS the router is
        # running, not a number parsed out of a file it was written from. The
        # router-id comes with it: it is the identity BGP uses, and the only
        # one that is unique per router.
        for node in self.nodes:
            ipv4 = ((state.get(node["name"]) or {}).get("summary") or {}).get("ipv4Unicast") or {}
            if ipv4.get("as") is not None:
                node["asn"] = ipv4["as"]
            if ipv4.get("routerId"):
                node["routerId"] = ipv4["routerId"]

        # Compare what the page RENDERS, not the raw poll. The summary carries
        # FRR's per-peer counters, and peerUptime advances with the clock, so
        # `state == self.last_state` can never be true: measured at this
        # interval against an idle fabric, it fired on 0 of 5 comparisons while
        # msgRcvd, msgSent, peerUptime and peerUptimeMsec moved every tick. The
        # counters still travel in the payload; they just no longer decide
        # whether anything changed.
        signal = self._signal(self.last_state, state)
        signature = self._signature(state)
        if signature == self.last_signature:
            self.last_state = state
            # The signal goes out on EVERY tick, change or not. It is what the
            # page animates from, and a heartbeat that only beats when the
            # topology changes is not a heartbeat: on a healthy fabric the
            # signature never moves and the page would sit frozen.
            await self.broadcast({"type": "signal", "data": signal})
            return

        try:
            events = self._diff_events(self.last_state, state)
        except Exception as exc:
            print(f"[poller] diff failed: {exc}")
            events = []
        self.last_state = state
        self.last_signature = signature
        await self.broadcast({"type": "state", "data": state})
        await self.broadcast({"type": "signal", "data": signal})
        for ev in events:
            await self.broadcast({"type": "event", "data": self.record_event(ev)})

    def record_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Give an event a monotonic id and keep it.

        The id is what lets a page ask for only what it has not seen
        (`/api/events?since=`) and de-duplicate an event that arrives twice —
        once in the catch-up fetch, once on the socket — without comparing
        whole objects or trusting two events in the same millisecond to differ.
        """
        self.last_event_id += 1
        event = dict(event, id=self.last_event_id)
        self.events.append(event)
        return event

    def events_since(self, since: int = 0) -> list[dict[str, Any]]:
        return [e for e in self.events if e["id"] > since]

    def _poll_node_sync(self, node: dict[str, Any]) -> dict[str, Any]:
        base = self._agents().get(node["name"])
        if not base:
            return {"error": f"no agent address for {node['name']}"}

        summary = self._get_json(base, "bgp-summary")
        # the `detail` view: the trimmed `show ip bgp json` returns paths with
        # no community or large-community fields
        bgp = self._get_json(base, "bgp-detail")
        # The neighbours view carries FRR's own timers. It is fetched
        # NON-FATALLY: a router that answered the first two is up even if this
        # one fails, and the page then shows the session without a heartbeat
        # rather than showing the router as down.
        try:
            neighbors = self._get_json(base, "bgp-neighbors")
        except Exception:
            neighbors = None
        return {"summary": summary, "bgp": bgp, "neighbors": neighbors}

    def _get_json(self, base: str, view: str) -> Any:
        """One named view from one agent. `view` is ours, never a caller's."""
        url = f"{base}/show/{view}"
        try:
            with urllib.request.urlopen(url, timeout=self.exec_timeout) as r:
                text = r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:200]
            raise RuntimeError(f"agent {url} answered {exc.code}: {body}") from exc
        except Exception as exc:                       # unreachable, timeout, DNS
            raise RuntimeError(f"agent {url} unreachable: {exc}") from exc
        # vtysh prints warnings before its JSON sometimes; the agent passes its
        # output through untouched, so the trim stays here.
        idx = text.find("{")
        if idx == -1:
            return None
        try:
            return json.loads(text[idx:])
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"bad json from {url}: {exc}; output={text[:200]}")

    @classmethod
    def _signal(cls, prev: dict, curr: dict) -> dict:
        """What each session DID between the last two polls.

        An Established line says the peers agreed to talk; these say whether
        anything is being said. Every number here is a measurement, and the two
        flags exist so the page cannot animate one that was not taken:
        `hasDelta` is false when there is no previous sample to subtract, and
        `hasTimers` is false when FRR's timers cannot be trusted for this peer.
        """
        out: dict[str, Any] = {}
        for node, ndata in curr.items():
            ndata = ndata or {}
            peers = cls._peers(ndata.get("summary") or {})
            before = cls._peers(((prev or {}).get(node) or {}).get("summary") or {})
            neighbors = ndata.get("neighbors") or {}
            per: dict[str, Any] = {}
            for ip, info in peers.items():
                entry = {
                    "state": info.get("state"),
                    "msgRcvd": info.get("msgRcvd", 0),
                    "msgSent": info.get("msgSent", 0),
                    "inq": info.get("inq", 0),
                    "outq": info.get("outq", 0),
                    "pfxRcd": info.get("pfxRcd", 0),
                    "pfxSnt": info.get("pfxSnt", 0),
                    # FRR's own flap count, which survives between polls and so
                    # catches a drop the 2s interval never saw.
                    "flaps": info.get("connectionsDropped", 0),
                    # A peer that arrived through `bgp listen range` rather than
                    # a `neighbor` line: on this fabric, a server.
                    "dynamic": bool(info.get("dynamicPeer")),
                    "hasDelta": False,
                    "dRcvd": 0, "dSent": 0, "dPfxRcd": 0, "dPfxSnt": 0,
                    "hasTimers": False,
                    "quietMsec": 0, "holdMsec": 0, "keepaliveMsec": 0,
                }
                was = before.get(ip)
                if was is not None:
                    # A counter that went DOWN means the session reset and
                    # restarted its counters. There is no interval to measure
                    # then: reporting a negative pulse, or clamping it to zero,
                    # would both be inventions. The flap is still visible in
                    # `flaps`, which survives the reset.
                    if (entry["msgRcvd"] >= was.get("msgRcvd", 0)
                            and entry["msgSent"] >= was.get("msgSent", 0)):
                        entry["hasDelta"] = True
                        entry["dRcvd"] = entry["msgRcvd"] - was.get("msgRcvd", 0)
                        entry["dSent"] = entry["msgSent"] - was.get("msgSent", 0)
                        # SIGNED: a withdrawal lowers pfxRcd while msgRcvd
                        # rises, so -1 here is a real withdrawal on a session
                        # that never reset. Render it as a signed change.
                        entry["dPfxRcd"] = entry["pfxRcd"] - was.get("pfxRcd", 0)
                        entry["dPfxSnt"] = entry["pfxSnt"] - was.get("pfxSnt", 0)
                nbr = neighbors.get(ip) or {}
                # FRR emits bgpTimerLastRead for EVERY peer, and for one that is
                # not Established it is the peer's AGE, not a heartbeat —
                # measured on 10.5.3 with a neighbour that never came up:
                # bgpState "Active", bgpTimerLastRead 14000 against a holdMsec
                # of 9000. It is also truncated to whole seconds and wraps every
                # 24h, so a peer down for exactly a day reads 0. FRR's own
                # bgpState is what says whether the reading means anything.
                if nbr.get("bgpState") == "Established" and info.get("state") == "Established":
                    entry["hasTimers"] = True
                    entry["quietMsec"] = nbr.get("bgpTimerLastRead", 0)
                    entry["holdMsec"] = nbr.get("bgpTimerHoldTimeMsecs", 0)
                    entry["keepaliveMsec"] = nbr.get("bgpTimerKeepAliveIntervalMsecs", 0)
                per[ip] = entry
            out[node] = per
        return out

    @classmethod
    def _signature(cls, state: dict) -> tuple:
        """What the page draws: the sessions and the chosen paths, nothing else.

        Sorted at every level because FRR's JSON objects are dicts and Python
        preserves insertion order, so an unsorted signature would change on
        iteration order alone. The routes are included, not just the peers: a
        withdrawal changes no peer state, and leaving them out would let the
        RIB keep showing a prefix the events pane had just reported gone.
        """
        out = []
        for node in sorted(state):
            ndata = state.get(node) or {}
            peers = cls._peers(ndata.get("summary") or {})
            sessions = tuple(
                (ip, (peers[ip] or {}).get("state"), (peers[ip] or {}).get("remoteAs"))
                for ip in sorted(peers)
            )
            best = cls._best_paths(ndata.get("bgp") or {})
            paths = tuple((prefix, best[prefix]) for prefix in sorted(best))
            out.append((node, ndata.get("error"), sessions, paths))
        return tuple(out)

    @staticmethod
    def event_ts(now: float | None = None) -> str:
        """RFC 3339, UTC, milliseconds.

        `%H:%M:%S` in the container's local time was three problems in one
        string: it carries no date, so it repeats every 24 hours and cannot be
        put beside a router log; it is a different instant to a reader in
        another zone, unlabelled; and at whole-second resolution the events of
        one reconvergence — which happens inside a second — arrive with
        identical stamps and no way to order them.
        """
        now = time.time() if now is None else now
        whole = int(now)
        ms = int(round((now - whole) * 1000))
        if ms == 1000:          # rounding up at .9996 must carry into the second
            whole, ms = whole + 1, 0
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(whole)) + f".{ms:03d}Z"

    def _diff_events(self, prev: dict, curr: dict) -> list[dict]:
        events: list[dict] = []
        # One stamp for the whole diff: these events were all read from the
        # same poll, and giving them separate times would imply an ordering the
        # measurement does not have. The ids order them.
        ts = self.event_ts()
        for node, ndata in curr.items():
            ndata = ndata or {}
            pnode = prev.get(node)
            psum = (pnode or {}).get("summary") or {}
            csum = ndata.get("summary") or {}
            ppeers = self._peers(psum)
            cpeers = self._peers(csum)
            # The same rule, applied backwards. A router we could not read LAST
            # time has an empty peers map for the same reason a dead one does,
            # so every session it reports now looks new: measured on this lab's
            # isp1, one timed-out poll cost four `appeared` events and two
            # `added` ones, describing nothing that happened. `pnode is None` is
            # the other case — a router never read at all — and that first sight
            # IS worth naming.
            saw_peers = pnode is None or self._answered(pnode, "summary")
            for ip, info in cpeers.items():
                pinfo = ppeers.get(ip)
                if pinfo is None:
                    if not saw_peers:
                        continue
                    # A new peer was already emitted as a bare state change,
                    # which reads as a transition out of nothing. Name it.
                    events.append({
                        "ts": ts,
                        "kind": "session",
                        "change": "appeared",
                        "node": node,
                        "peer": ip,
                        "remoteAs": info.get("remoteAs"),
                        "state": info.get("state"),
                    })
                elif pinfo.get("state") != info.get("state"):
                    events.append({
                        "ts": ts,
                        "kind": "session",
                        "change": "state",
                        "node": node,
                        "peer": ip,
                        "remoteAs": info.get("remoteAs"),
                        "state": info.get("state"),
                        "was": pinfo.get("state"),
                    })
            # A peer that VANISHES from the table produces nothing above: the
            # loop only walks what is still there. The session disappeared from
            # the router — a dynamic neighbour that left, a `no neighbor`, a
            # bgpd that died — and the page went on drawing its edge green,
            # because with no peer data there is no new colour to paint.
            #
            # Only when the router ANSWERED, though. A poll that failed has an
            # empty peers map for the same reason a dead router does, and
            # "isp1 is unreachable" is not "isp1's four sessions are gone":
            # one unreachable router would otherwise spray a vanished event
            # per session and, when it answered again, an appeared event per
            # session. We claim something is missing only after looking.
            if not self._answered(ndata, "summary"):
                continue
            for ip, pinfo in ppeers.items():
                if ip in cpeers:
                    continue
                events.append({
                    "ts": ts,
                    "kind": "session",
                    "change": "vanished",
                    "node": node,
                    "peer": ip,
                    "remoteAs": pinfo.get("remoteAs"),
                    "was": pinfo.get("state"),
                })
        # path-best changes
        for node, ndata in curr.items():
            ndata = ndata or {}
            pnode = prev.get(node)
            pbgp = (pnode or {}).get("bgp") or {}
            cbgp = ndata.get("bgp") or {}
            pbest = self._best_paths(pbgp)
            cbest = self._best_paths(cbgp)
            saw_table = pnode is None or self._answered(pnode, "bgp")
            for prefix, nh in cbest.items():
                if prefix not in pbest:
                    if not saw_table:
                        continue
                    # A prefix that ARRIVES. `prefix in pbest` excluded exactly
                    # this: the lab could learn a route and say nothing.
                    events.append({
                        "ts": ts,
                        "kind": "route",
                        "node": node,
                        "prefix": prefix,
                        "change": "added",
                        "to": nh,
                    })
                elif pbest.get(prefix) != nh:
                    events.append({
                        "ts": ts,
                        "kind": "bestpath",
                        "node": node,
                        "prefix": prefix,
                        "from": pbest.get(prefix),
                        "to": nh,
                    })
            if not self._answered(ndata, "bgp"):
                continue
            for prefix, nh in pbest.items():
                if prefix not in cbest:
                    # A withdrawal. The RIB pane would otherwise keep showing a
                    # prefix the events pane never mentioned losing. Same rule
                    # as above: a failed poll has an empty table, and a router
                    # we could not read has not withdrawn anything.
                    events.append({
                        "ts": ts,
                        "kind": "route",
                        "node": node,
                        "prefix": prefix,
                        "change": "withdrawn",
                        "from": nh,
                    })
        return events

    @staticmethod
    def _answered(ndata: dict, key: str) -> bool:
        """True when this poll actually read `key` from the router.

        An errored node and a router with nothing in its table look identical
        downstream — both are an empty dict — so the difference has to be read
        here, from whether the poll produced the view at all.
        """
        return not ndata.get("error") and isinstance(ndata.get(key), dict)

    @staticmethod
    def _peers(summary: dict) -> dict:
        ipv4 = (summary or {}).get("ipv4Unicast") or {}
        return ipv4.get("peers") or {}

    @staticmethod
    def _best_paths(bgp: dict) -> dict:
        out = {}
        routes = (bgp or {}).get("routes") or {}
        for prefix, paths in routes.items():
            if not isinstance(paths, list):
                continue
            for p in paths:
                bp = p.get("bestpath")
                is_best = bp is True or (isinstance(bp, dict) and bp.get("overall"))
                if is_best:
                    nh_list = p.get("nexthops", [])
                    nh = nh_list[0].get("ip") if nh_list else None
                    out[prefix] = nh
                    break
        return out
