import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import docker
import yaml


class LabPoller:
    def __init__(
        self,
        topology_path: Path,
        lab_prefix: str,
        broadcast: Callable[[dict], Awaitable[None]],
        interval: float = 2.0,
        exec_timeout: float = 5.0,
    ) -> None:
        self.topology_path = topology_path
        self.lab_prefix = lab_prefix
        self.broadcast = broadcast
        self.interval = interval
        # A vtysh that never returns otherwise blocks its worker thread for
        # ever: exec_run is two HTTP calls to the daemon, and without a timeout
        # the read on exec_start has no deadline. One wedged router then stalls
        # every poll, because poll_all gathers all of them.
        self.client = docker.from_env(timeout=exec_timeout)
        self.exec_timeout = exec_timeout
        self.nodes: list[dict[str, Any]] = self._load_nodes()
        self.last_state: dict[str, dict[str, Any]] = {}
        self.last_signature: Any = None

    def _load_nodes(self) -> list[dict[str, Any]]:
        topology = yaml.safe_load(self.topology_path.read_text())
        node_names = list(topology["topology"]["nodes"].keys())
        result = []
        for name in node_names:
            if name == "dashboard":
                continue
            asn = self._guess_asn(name)
            result.append({"name": name, "asn": asn})
        return result

    def _guess_asn(self, node_name: str) -> int | None:
        # Look in configs/<node>/frr.conf relative to topology
        candidate = self.topology_path.parent / "configs" / node_name / "frr.conf"
        if not candidate.exists():
            return None
        match = re.search(r"^\s*router bgp (\d+)", candidate.read_text(), re.MULTILINE)
        return int(match.group(1)) if match else None

    async def run(self) -> None:
        while True:
            try:
                await self.poll_all()
            except Exception as exc:
                print(f"[poller] error: {exc}")
            await asyncio.sleep(self.interval)

    async def poll_all(self) -> None:
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

        # Compare what the page RENDERS, not the raw poll. The summary carries
        # FRR's per-peer counters, and peerUptime advances with the clock, so
        # `state == self.last_state` can never be true: measured at this
        # interval against an idle fabric, it fired on 0 of 5 comparisons while
        # msgRcvd, msgSent, peerUptime and peerUptimeMsec moved every tick. The
        # counters still travel in the payload; they just no longer decide
        # whether anything changed.
        signature = self._signature(state)
        if signature == self.last_signature:
            self.last_state = state
            return

        try:
            events = self._diff_events(self.last_state, state)
        except Exception as exc:
            print(f"[poller] diff failed: {exc}")
            events = []
        self.last_state = state
        self.last_signature = signature
        await self.broadcast({"type": "state", "data": state})
        for ev in events:
            await self.broadcast({"type": "event", "data": ev})

    def _poll_node_sync(self, node: dict[str, Any]) -> dict[str, Any]:
        container_name = f"{self.lab_prefix}-{node['name']}"
        try:
            container = self.client.containers.get(container_name)
        except docker.errors.NotFound:
            return {"error": f"container {container_name} not found"}

        summary = self._exec_json(container, "show ip bgp summary json")
        # `detail` variant is required for community / large-community fields —
        # the bulk `show ip bgp json` returns a trimmed path object without them.
        bgp = self._exec_json(container, "show ip bgp detail json")
        return {"summary": summary, "bgp": bgp}

    def _exec_json(self, container, command: str) -> Any:
        result = container.exec_run(["vtysh", "-c", command])
        if result.exit_code != 0:
            raise RuntimeError(f"vtysh failed for {command}: {result.output[:200]}")
        text = result.output.decode("utf-8", errors="replace")
        # vtysh sometimes prints warnings before JSON; trim to first '{'
        idx = text.find("{")
        if idx == -1:
            return None
        try:
            return json.loads(text[idx:])
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"bad json from {command}: {exc}; output={text[:200]}")

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

    def _diff_events(self, prev: dict, curr: dict) -> list[dict]:
        events: list[dict] = []
        ts = time.strftime("%H:%M:%S")
        for node, ndata in curr.items():
            psum = (prev.get(node) or {}).get("summary") or {}
            csum = (ndata or {}).get("summary") or {}
            ppeers = self._peers(psum)
            cpeers = self._peers(csum)
            for ip, info in cpeers.items():
                pinfo = ppeers.get(ip)
                if not pinfo or pinfo.get("state") != info.get("state"):
                    events.append({
                        "ts": ts,
                        "kind": "session",
                        "node": node,
                        "peer": ip,
                        "remoteAs": info.get("remoteAs"),
                        "state": info.get("state"),
                    })
        # path-best changes
        for node, ndata in curr.items():
            pbgp = (prev.get(node) or {}).get("bgp") or {}
            cbgp = (ndata or {}).get("bgp") or {}
            pbest = self._best_paths(pbgp)
            cbest = self._best_paths(cbgp)
            for prefix, nh in cbest.items():
                if pbest.get(prefix) != nh and prefix in pbest:
                    events.append({
                        "ts": ts,
                        "kind": "bestpath",
                        "node": node,
                        "prefix": prefix,
                        "from": pbest.get(prefix),
                        "to": nh,
                    })
        return events

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
