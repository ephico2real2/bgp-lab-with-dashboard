#!/usr/bin/env python3
"""Read the lab's FRR configs and FRR/dashboard JSON. State is an exact
field match (`state` / `peerState` / `bgpState` == "Established"), never
a substring of the blob. Invalid JSON → exit 2.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def configs_dir(root: Path | None = None) -> Path:
    return (root or repo_root()) / "configs"


def node_names(root: Path | None = None) -> list[str]:
    d = configs_dir(root)
    return sorted(
        p.name
        for p in d.iterdir()
        if p.is_dir() and (p / "frr.conf").is_file()
    )


def read_conf(node: str, root: Path | None = None) -> str:
    return (configs_dir(root) / node / "frr.conf").read_text()


def asn_of(node: str, root: Path | None = None) -> int | None:
    m = re.search(r"^\s*router bgp (\d+)", read_conf(node, root), re.MULTILINE)
    return int(m.group(1)) if m else None


def neighbors_of(node: str, root: Path | None = None) -> list[tuple[str, int]]:
    text = read_conf(node, root)
    out: list[tuple[str, int]] = []
    seen: set[str] = set()
    for ip, asn in re.findall(
        r"^\s*neighbor\s+(\S+)\s+remote-as\s+(\d+)", text, re.MULTILINE
    ):
        if ip in seen:
            continue
        seen.add(ip)
        out.append((ip, int(asn)))
    return out


def ifaces_of(node: str, root: Path | None = None) -> list[tuple[str, str]]:
    """(ethN, address) from `interface` / `ip address` in frr.conf; skip lo."""
    out: list[tuple[str, str]] = []
    iface = ""
    for line in read_conf(node, root).splitlines():
        m = re.match(r"^interface\s+(\S+)", line)
        if m:
            iface = m.group(1)
            continue
        if iface and iface != "lo":
            m = re.match(r"^\s*ip address\s+(\d+\.\d+\.\d+\.\d+)", line)
            if m:
                out.append((iface, m.group(1)))
    return out


def originated_of(node: str, root: Path | None = None) -> list[str]:
    text = read_conf(node, root)
    out: list[str] = []
    seen: set[str] = set()
    for pfx in re.findall(r"^\s*network\s+(\S+)", text, re.MULTILINE):
        if pfx in seen:
            continue
        seen.add(pfx)
        out.append(pfx)
    return out


def topology_nodes(path: Path) -> list[str]:
    # Minimal YAML walk so we do not depend on PyYAML on the host.
    text = path.read_text()
    in_nodes = False
    names: list[str] = []
    for line in text.splitlines():
        if re.match(r"^  nodes:\s*$", line):
            in_nodes = True
            continue
        if in_nodes:
            if re.match(r"^  [A-Za-z]", line) and not line.startswith("    "):
                break
            m = re.match(r"^    ([A-Za-z0-9_-]+):\s*$", line)
            if m:
                name = m.group(1)
                if name != "dashboard":
                    names.append(name)
    return names


def load_json(raw: str):
    raw = raw.strip()
    idx = raw.find("{")
    if idx == -1:
        raise json.JSONDecodeError("no JSON object", raw, 0)
    return json.loads(raw[idx:])


def peers_from(data) -> dict:
    if not isinstance(data, dict):
        return {}
    if "peers" in data and isinstance(data["peers"], dict):
        return data["peers"]
    out = {}
    for v in data.values():
        if isinstance(v, dict):
            out.update(peers_from(v))
    return out


def state_of(peer) -> str:
    if not isinstance(peer, dict):
        return ""
    for k in ("state", "peerState", "bgpState"):
        val = peer.get(k)
        if isinstance(val, str) and val:
            return val
    return ""


def routes_from(data) -> dict:
    if not isinstance(data, dict):
        return {}
    if "routes" in data and isinstance(data["routes"], dict):
        return data["routes"]
    out = {}
    for v in data.values():
        if isinstance(v, dict):
            out.update(routes_from(v))
    return out


def cmd_nodes(args) -> int:
    root = Path(args.root) if args.root else repo_root()
    if args.topology:
        names = topology_nodes(Path(args.topology))
    else:
        names = node_names(root)
    for n in names:
        print(n)
    return 0


def cmd_asns(args) -> int:
    root = Path(args.root) if args.root else repo_root()
    for n in node_names(root):
        asn = asn_of(n, root)
        print("%s\t%s" % (n, asn if asn is not None else ""))
    return 0


def cmd_expected_peers(args) -> int:
    root = Path(args.root) if args.root else repo_root()
    for n in node_names(root):
        for ip, asn in neighbors_of(n, root):
            print("%s\t%s\t%s" % (n, ip, asn))
    return 0


def cmd_ifaces(args) -> int:
    root = Path(args.root) if args.root else repo_root()
    for n in node_names(root):
        for iface, addr in ifaces_of(n, root):
            print("%s\t%s\t%s" % (n, iface, addr))
    return 0


def cmd_originated(args) -> int:
    root = Path(args.root) if args.root else repo_root()
    for n in node_names(root):
        for pfx in originated_of(n, root):
            print("%s\t%s" % (n, pfx))
    return 0


def cmd_summary_dump(args) -> int:
    try:
        data = load_json(sys.stdin.read())
    except json.JSONDecodeError as e:
        print("bgp.py: not JSON: %s" % e, file=sys.stderr)
        return 2
    for ip, peer in sorted(peers_from(data).items()):
        print("%s\t%s" % (ip, state_of(peer)))
    return 0


def cmd_summary_require(args) -> int:
    try:
        data = load_json(sys.stdin.read())
    except json.JSONDecodeError as e:
        print("bgp.py: not JSON: %s" % e, file=sys.stderr)
        return 2
    peers = peers_from(data)
    failed = 0
    measured = []
    for ip in args.peers:
        peer = peers.get(ip)
        st = state_of(peer) if peer is not None else "ABSENT"
        measured.append("%s=%s" % (ip, st))
        if st != "Established":
            failed = 1
    print(" ".join(measured))
    return failed


def cmd_table_has(args) -> int:
    try:
        data = load_json(sys.stdin.read())
    except json.JSONDecodeError as e:
        print("bgp.py: not JSON: %s" % e, file=sys.stderr)
        return 2
    routes = routes_from(data)
    paths = routes.get(args.prefix)
    n = len(paths) if isinstance(paths, list) else (1 if paths else 0)
    print("prefix=%s paths=%s" % (args.prefix, n))
    return 0 if n > 0 else 1


def cmd_dashboard(args) -> int:
    try:
        data = load_json(sys.stdin.read())
    except json.JSONDecodeError as e:
        print("bgp.py: not JSON: %s" % e, file=sys.stderr)
        return 2
    if not isinstance(data, dict):
        print("dashboard: not an object", file=sys.stderr)
        return 2
    ready = data.get("ready")
    nodes = data.get("nodes") if isinstance(data.get("nodes"), list) else []
    names = []
    asns = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        names.append(str(n.get("name") or ""))
        asns.append(n.get("asn"))
    print("ready=%s nodes=%s asns=%s" % (ready, ",".join(names), ",".join(str(a) for a in asns)))
    if args.expect_ready and ready is not True:
        return 1
    if args.expect_nodes:
        want = args.expect_nodes.split(",")
        if names != want:
            return 1
    if args.expect_asns:
        want_asns = [int(x) for x in args.expect_asns.split(",")]
        got = [int(a) for a in asns if a is not None]
        if got != want_asns:
            return 1
    return 0


RFC3339_MS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def cmd_events(args) -> int:
    """The event history a reloading page catches up from.

    Three properties, because each one is a defect the dashboard had: the ring
    answers at all (events used to be broadcast and forgotten), the ids are
    strictly increasing (they are what `since` and de-duplication stand on),
    and every stamp is RFC 3339 UTC with milliseconds (it used to be the
    container's local HH:MM:SS, undateable and unorderable).
    """
    try:
        data = load_json(sys.stdin.read())
    except json.JSONDecodeError as e:
        print("bgp.py: not JSON: %s" % e, file=sys.stderr)
        return 2
    if not isinstance(data, dict):
        print("events: not an object", file=sys.stderr)
        return 2
    events = data.get("events") if isinstance(data.get("events"), list) else []
    ids = [e.get("id") for e in events if isinstance(e, dict)]
    bad_stamp = [e.get("ts") for e in events
                 if isinstance(e, dict) and not RFC3339_MS.match(str(e.get("ts") or ""))]
    ordered = all(isinstance(a, int) and isinstance(b, int) and b > a
                  for a, b in zip(ids, ids[1:]))
    span = "%s..%s" % (ids[0], ids[-1]) if ids else "none"
    print("ready=%s events=%s ids=%s lastId=%s stamps=%s" % (
        data.get("ready"), len(events), span, data.get("lastId"),
        "rfc3339" if not bad_stamp else "BAD:" + str(bad_stamp[0])))
    if args.expect_ready and data.get("ready") is not True:
        return 1
    if len(events) < args.min_events:
        return 1
    if bad_stamp or not ordered:
        return 1
    if args.expect_first_id and (not ids or ids[0] != args.expect_first_id):
        return 1
    return 0


def cmd_peer_states(args) -> int:
    try:
        data = load_json(sys.stdin.read())
    except json.JSONDecodeError as e:
        print("bgp.py: not JSON: %s" % e, file=sys.stderr)
        return 2
    blob = data.get("data") if isinstance(data, dict) and "data" in data else data
    if not isinstance(blob, dict):
        print("peer-states: no data", file=sys.stderr)
        return 2
    n_est = 0
    n_other = 0
    parts = []
    for node, nd in blob.items():
        if not isinstance(nd, dict):
            continue
        peers = peers_from(nd.get("summary") or {})
        for ip, peer in sorted(peers.items()):
            st = state_of(peer)
            parts.append("%s:%s=%s" % (node, ip, st or "ABSENT"))
            if st == "Established":
                n_est += 1
            else:
                n_other += 1
    print("established=%s other=%s %s" % (n_est, n_other, " ".join(parts)))
    if args.require_other and n_other == 0:
        return 1
    if args.require_all_established and n_other != 0:
        return 1
    if args.require_all_established and n_est == 0:
        return 1
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="bgp.py")
    root_p = argparse.ArgumentParser(add_help=False)
    root_p.add_argument("--root", default="")
    sub = p.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("nodes", parents=[root_p])
    n.add_argument("--topology", default="")
    n.set_defaults(func=cmd_nodes)

    a = sub.add_parser("asns", parents=[root_p])
    a.set_defaults(func=cmd_asns)

    e = sub.add_parser("expected-peers", parents=[root_p])
    e.set_defaults(func=cmd_expected_peers)

    i = sub.add_parser("ifaces", parents=[root_p])
    i.set_defaults(func=cmd_ifaces)

    o = sub.add_parser("originated", parents=[root_p])
    o.set_defaults(func=cmd_originated)

    d = sub.add_parser("summary-dump", parents=[root_p])
    d.set_defaults(func=cmd_summary_dump)

    r = sub.add_parser("summary-require", parents=[root_p])
    r.add_argument("peers", nargs="+")
    r.set_defaults(func=cmd_summary_require)

    t = sub.add_parser("table-has", parents=[root_p])
    t.add_argument("prefix")
    t.set_defaults(func=cmd_table_has)

    b = sub.add_parser("dashboard", parents=[root_p])
    b.add_argument("--expect-ready", action="store_true")
    b.add_argument("--expect-nodes", default="")
    b.add_argument("--expect-asns", default="")
    b.set_defaults(func=cmd_dashboard)

    e = sub.add_parser("events", parents=[root_p])
    e.add_argument("--expect-ready", action="store_true")
    e.add_argument("--min-events", type=int, default=0)
    e.add_argument("--expect-first-id", type=int, default=0)
    e.set_defaults(func=cmd_events)

    s = sub.add_parser("peer-states", parents=[root_p])
    s.add_argument("--require-other", action="store_true")
    s.add_argument("--require-all-established", action="store_true")
    s.set_defaults(func=cmd_peer_states)

    args = p.parse_args(argv[1:])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
