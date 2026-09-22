"""The events pane's four defects: what it said, when it said it, and to whom.

Every test here is built from the shape `show ip bgp summary json` and
`show ip bgp detail json` actually return — a peers map keyed by address, and a
routes map whose values are a LIST of paths, at most one of them the bestpath.
The ECMP case is in that list shape on purpose: it is the measured pitfall of
issue #5, where iterating per path logged one prefix three times.
"""
import json
import re
import sys
from collections import deque
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from poller import LabPoller  # noqa: E402

RFC3339_MS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


@pytest.fixture
def poller() -> LabPoller:
    """A poller with no docker client: none of this touches a container."""
    p = LabPoller.__new__(LabPoller)
    p.events = deque(maxlen=500)
    p.last_event_id = 0
    return p


def summary(peers: dict) -> dict:
    return {"ipv4Unicast": {"peers": peers}}


def peer(state: str = "Established", remote_as: int = 65000) -> dict:
    return {"state": state, "remoteAs": remote_as}


def routes(table: dict[str, list[str]] | None = None) -> dict:
    """`bgp` as FRR returns it: prefix -> list of paths, the first one best."""
    out = {}
    for prefix, nexthops in (table or {}).items():
        out[prefix] = [{"bestpath": {"overall": True} if i == 0 else False,
                        "nexthops": [{"ip": nh}]}
                       for i, nh in enumerate(nexthops)]
    return {"routes": out}


def node(peers: dict | None = None, bgp: dict | None = None) -> dict:
    return {"summary": summary(peers or {}), "bgp": bgp or {"routes": {}}}


def kinds(events: list[dict]) -> list[tuple]:
    return [(e["kind"], e.get("change")) for e in events]


# ---- #6 the stamp ---------------------------------------------------------

def test_the_stamp_is_rfc3339_utc_with_milliseconds():
    """`%H:%M:%S` local carried no date, no zone and no sub-second ordering."""
    assert LabPoller.event_ts(0) == "1970-01-01T00:00:00.000Z"
    assert LabPoller.event_ts(1758412800.25) == "2025-09-21T00:00:00.250Z"
    assert RFC3339_MS.match(LabPoller.event_ts())


def test_a_rounding_carry_does_not_produce_a_thousandth_millisecond():
    """.9996 rounds to 1000 ms, which is not a time. It is the next second."""
    assert LabPoller.event_ts(0.9996) == "1970-01-01T00:00:01.000Z"


def test_every_event_of_one_poll_carries_the_same_stamp(poller):
    """They were read from one poll: separate times would imply an order the
    measurement does not have. The ids carry the order."""
    prev = {"leaf1": node({"10.0.0.1": peer("Idle")}, routes({"10.9.9.0/24": ["10.0.0.1"]}))}
    curr = {"leaf1": node({"10.0.0.2": peer()}, routes({"10.8.8.0/24": ["10.0.0.2"]}))}
    events = poller._diff_events(prev, curr)
    assert len(events) == 4, kinds(events)
    stamps = {e["ts"] for e in events}
    assert len(stamps) == 1
    # and it is the stamp event_ts produces, not a local HH:MM:SS
    assert RFC3339_MS.match(stamps.pop())


# ---- #4 a peer that disappears -------------------------------------------

def test_a_vanished_peer_produces_an_event(poller):
    """The loop walked the CURRENT peers only, so a peer that left said
    nothing and the page kept its edge painted with the last state it saw."""
    prev = {"leaf1": node({"10.0.0.1": peer("Established", 65100)})}
    curr = {"leaf1": node({})}
    events = poller._diff_events(prev, curr)
    assert kinds(events) == [("session", "vanished")]
    assert events[0]["peer"] == "10.0.0.1"
    assert events[0]["was"] == "Established"
    assert events[0]["remoteAs"] == 65100


def test_a_peer_that_is_still_there_does_not_vanish(poller):
    prev = {"leaf1": node({"10.0.0.1": peer()})}
    assert poller._diff_events(prev, prev) == []


def test_a_new_peer_is_named_appeared_not_a_bare_transition(poller):
    prev = {"leaf1": node({})}
    curr = {"leaf1": node({"10.0.0.1": peer("Active")})}
    events = poller._diff_events(prev, curr)
    assert kinds(events) == [("session", "appeared")]
    assert events[0]["state"] == "Active"


def test_a_state_change_carries_what_it_was(poller):
    prev = {"leaf1": node({"10.0.0.1": peer("Established")})}
    curr = {"leaf1": node({"10.0.0.1": peer("Idle (Admin)")})}
    events = poller._diff_events(prev, curr)
    assert kinds(events) == [("session", "state")]
    assert (events[0]["was"], events[0]["state"]) == ("Established", "Idle (Admin)")


# ---- #5 added and withdrawn routes ---------------------------------------

def test_a_prefix_that_arrives_is_an_added_event(poller):
    prev = {"leaf1": node({}, routes())}
    curr = {"leaf1": node({}, routes({"10.0.0.0/24": ["10.1.1.1"]}))}
    events = poller._diff_events(prev, curr)
    assert kinds(events) == [("route", "added")]
    assert events[0]["prefix"] == "10.0.0.0/24"
    assert events[0]["to"] == "10.1.1.1"


def test_a_prefix_that_disappears_is_a_withdrawal(poller):
    """Read from the PREVIOUS snapshot: the next-hop it used to arrive by is
    the only place that direction still exists."""
    prev = {"leaf1": node({}, routes({"10.0.0.0/24": ["10.1.1.1"]}))}
    curr = {"leaf1": node({}, routes())}
    events = poller._diff_events(prev, curr)
    assert kinds(events) == [("route", "withdrawn")]
    assert events[0]["from"] == "10.1.1.1"


def test_a_nexthop_change_is_still_a_bestpath_event(poller):
    """The behaviour that already worked must not be swallowed by the two new
    branches: a prefix in both snapshots with a different next-hop moved, it
    was not added and not withdrawn."""
    prev = {"leaf1": node({}, routes({"10.0.0.0/24": ["10.1.1.1"]}))}
    curr = {"leaf1": node({}, routes({"10.0.0.0/24": ["10.2.2.2"]}))}
    events = poller._diff_events(prev, curr)
    assert kinds(events) == [("bestpath", None)]
    assert (events[0]["from"], events[0]["to"]) == ("10.1.1.1", "10.2.2.2")


def test_an_ecmp_prefix_is_logged_once(poller):
    """Issue #5's measured pitfall: iterating per PATH logged one prefix three
    times. The diff is over best paths, which is one per prefix."""
    prev = {"leaf1": node({}, routes())}
    curr = {"leaf1": node({}, routes({"10.0.0.0/24": ["10.1.1.1", "10.2.2.2", "10.3.3.3"]}))}
    assert kinds(poller._diff_events(prev, curr)) == [("route", "added")]


def test_a_quiet_tick_produces_nothing(poller):
    state = {"leaf1": node({"10.0.0.1": peer()}, routes({"10.0.0.0/24": ["10.1.1.1"]}))}
    assert poller._diff_events(state, state) == []


# ---- #6 the ring and the ids ---------------------------------------------

def test_ids_increase_and_events_since_returns_only_the_gap(poller):
    for i in range(5):
        poller.record_event({"kind": "session", "peer": f"10.0.0.{i}"})
    assert [e["id"] for e in poller.events] == [1, 2, 3, 4, 5]
    assert [e["id"] for e in poller.events_since(3)] == [4, 5]
    assert poller.events_since(5) == []
    assert len(poller.events_since(0)) == 5


def test_recording_does_not_mutate_the_caller_s_event(poller):
    """The same dict is broadcast and kept; stamping the caller's copy in place
    would give one object two owners."""
    ev = {"kind": "session"}
    recorded = poller.record_event(ev)
    assert "id" not in ev and recorded["id"] == 1


def test_the_ring_drops_the_oldest_and_keeps_the_ids_honest():
    p = LabPoller.__new__(LabPoller)
    p.events, p.last_event_id = deque(maxlen=3), 0
    for i in range(5):
        p.record_event({"kind": "session", "n": i})
    assert [e["id"] for e in p.events] == [3, 4, 5]
    # An id the ring has already dropped asks for everything it still has,
    # rather than nothing: the page is told what survives, not misled.
    assert [e["id"] for e in p.events_since(1)] == [3, 4, 5]


def test_broadcast_events_carry_their_id(bare_poller):
    """What the socket delivers and what /api/events returns are the same
    objects with the same ids — that is what lets a page de-duplicate."""
    import asyncio

    sent: list[dict] = []

    async def broadcast(message):
        sent.append(message)

    p = bare_poller(broadcast=broadcast, nodes=[{"name": "leaf1", "asn": 65101}])

    states = [node({"10.0.0.1": peer()}), node({})]

    async def drive():
        for st in states:
            p._poll_node_sync = lambda n, s=st: s      # noqa: ARG005
            await LabPoller.poll_all(p)

    asyncio.run(drive())
    events = [m["data"] for m in sent if m["type"] == "event"]
    assert [e["id"] for e in events] == [1, 2]
    assert kinds(events) == [("session", "appeared"), ("session", "vanished")]
    assert [e["id"] for e in p.events_since(0)] == [1, 2]
    assert json.dumps(events)      # it has to survive the socket


# ---- the ring is the size it was configured to be -------------------------

def test_init_sizes_the_ring_from_its_argument(tmp_path, monkeypatch):
    """The hand-built pollers above set maxlen themselves, so none of them can
    see a constructor that forgot it. Measured: with `deque()` in __init__ and
    no maxlen, every test above still passed while the ring grew for ever."""
    import docker

    monkeypatch.setattr(docker, "from_env", lambda **kw: object())
    topology = tmp_path / "topology.yml"
    topology.write_text("topology:\n  nodes:\n    leaf1: {}\n")

    p = LabPoller(topology_path=topology, lab_prefix="clab-x",
                  broadcast=None, events_ring=3)
    assert p.events.maxlen == 3
    for i in range(5):
        p.record_event({"kind": "session", "n": i})
    assert [e["n"] for e in p.events] == [2, 3, 4]


def test_the_endpoint_serves_the_same_ids_the_socket_sent(monkeypatch):
    """`/api/events?since=` is the catch-up path for a page that reloaded or
    reconnected: it has to answer from the same ring, with the same ids."""
    import asyncio
    import main

    p = LabPoller.__new__(LabPoller)
    p.events, p.last_event_id, p.epoch = deque(maxlen=500), 0, "epoch-a"
    for i in range(3):
        p.record_event({"kind": "session", "peer": f"10.0.0.{i}"})
    monkeypatch.setattr(main, "poller", p)

    body = asyncio.run(main.events(since=1))
    assert body["ready"] is True
    assert [e["id"] for e in body["events"]] == [2, 3]
    assert body["lastId"] == 3

    monkeypatch.setattr(main, "poller", None)
    cold = asyncio.run(main.events(since=0))
    assert cold == {"ready": False, "events": [], "lastId": 0}


def test_the_app_gives_the_poller_the_configured_ring(monkeypatch):
    """The size is read from the environment in main and has to arrive."""
    import asyncio
    import main

    seen = {}

    class Stub:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        async def run(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(main, "LabPoller", Stub)

    async def drive():
        async with main.lifespan(None):
            pass

    asyncio.run(drive())
    assert seen["events_ring"] == main.EVENTS_RING


# ---- a router we could not read has not lost anything ---------------------

def test_a_failed_poll_does_not_vanish_its_sessions(poller):
    """The trap this fix introduced and then closed: an errored node has an
    empty peers map for the same reason a dead router does. One unreachable
    router would have sprayed a vanished event per session, and an appeared
    event per session when it answered again."""
    prev = {"isp1": node({"10.0.0.1": peer(), "10.0.0.2": peer()})}
    curr = {"isp1": {"error": "RuntimeError('vtysh timed out')"}}
    assert poller._diff_events(prev, curr) == []


def test_a_poll_that_returned_no_summary_is_not_a_vanished_session(poller):
    """`_exec_json` returns None when vtysh printed nothing parseable. That is
    an unread router, not an empty one."""
    prev = {"isp1": node({"10.0.0.1": peer()})}
    for broken in ({"summary": None, "bgp": None}, {}, {"summary": [], "bgp": {}}):
        assert poller._diff_events(prev, {"isp1": broken}) == [], broken


def test_a_failed_poll_does_not_withdraw_its_routes(poller):
    prev = {"isp1": node({}, routes({"10.0.0.0/24": ["10.1.1.1"]}))}
    curr = {"isp1": {"error": "boom"}}
    assert poller._diff_events(prev, curr) == []


def test_the_router_that_did_answer_is_still_diffed(poller):
    """The guard is per node: one router failing must not silence the others."""
    prev = {"isp1": node({"10.0.0.1": peer()}),
            "isp2": node({"10.0.0.3": peer()})}
    curr = {"isp1": {"error": "boom"},
            "isp2": node({})}
    events = poller._diff_events(prev, curr)
    assert kinds(events) == [("session", "vanished")]
    assert events[0]["node"] == "isp2"


def test_the_first_sight_of_a_router_is_still_an_appeared_event(poller):
    """The guard is deliberately one-sided. We refuse to say something is GONE
    without looking, but the first poll that reads a router is a real first
    sight and the page should show it."""
    curr = {"isp1": node({"10.0.0.1": peer()})}
    assert kinds(poller._diff_events({}, curr)) == [("session", "appeared")]


def test_a_poll_that_reports_an_error_is_not_trusted_even_with_a_table(poller):
    """Today `_poll_node_sync` returns either an error or a full reading, never
    both — but the neighbours view is already fetched non-fatally, and the day
    a partial result carries both, an error means the reading is incomplete.
    An incomplete reading may not be used to declare a session gone."""
    prev = {"isp1": node({"10.0.0.1": peer()}, routes({"10.0.0.0/24": ["10.1.1.1"]}))}
    curr = {"isp1": {"error": "vtysh timed out", **node({}, routes())}}
    assert poller._diff_events(prev, curr) == []


def test_a_router_that_answers_again_does_not_re_announce_its_sessions(poller):
    """The other half of the same guard.

    A poll that failed produces no `vanished` events — but the poll that
    SUCCEEDS after it has an empty previous reading to diff against, so every
    session reads as new and every prefix as added. Measured on this lab's
    isp1, four sessions and two prefixes: one timed-out poll cost six events
    that describe nothing that happened.
    """
    good = {"isp1": node({"10.0.10.1": peer(), "10.0.11.1": peer()},
                         routes({"10.1.1.0/24": ["10.0.10.1"]}))}
    for unread in ({"error": "RuntimeError('vtysh timed out')"},
                   {"summary": None, "bgp": None},
                   {}):
        assert poller._diff_events({"isp1": unread}, good) == [], unread


def test_the_poll_after_a_recovery_is_diffed_normally(poller):
    """Silence lasts one poll, not for ever: once there is a reading to compare
    against, the next change is reported as usual."""
    good = {"isp1": node({"10.0.10.1": peer("Established")})}
    down = {"isp1": node({"10.0.10.1": peer("Idle")})}
    assert kinds(poller._diff_events(good, down)) == [("session", "state")]


# ---- what the page's restart tell reads ------------------------------------

def test_last_id_is_the_highest_id_issued_not_the_ring_s_length(monkeypatch):
    """The page compares lastId with the highest id it holds. A ring that has
    dropped its oldest is shorter than the ids it issued: a lastId read as
    len(ring) would sit at 500 while the page held 501, 502, … and every
    reconnect would then read as a restart and redraw the whole ring."""
    import asyncio
    import main

    p = LabPoller.__new__(LabPoller)
    p.events, p.last_event_id, p.epoch = deque(maxlen=3), 0, "epoch-a"
    for i in range(5):
        p.record_event({"kind": "session", "n": i})
    monkeypatch.setattr(main, "poller", p)
    body = asyncio.run(main.events(since=4))
    assert body["lastId"] == 5
    assert [e["id"] for e in body["events"]] == [5]


def test_the_endpoint_names_the_process_that_issued_the_ids(tmp_path, monkeypatch):
    """Ids restart at 1 with the process. Measured on this lab, a fresh
    poller's first poll issues 18 events, so a page holding 18 is told a
    lastId that is not below its own and cannot see the restart by number.
    Two pollers name themselves differently; one names itself the same way
    on every call."""
    import asyncio
    import docker
    import main

    monkeypatch.setattr(docker, "from_env", lambda **kw: object())
    topology = tmp_path / "topology.yml"
    topology.write_text("topology:\n  nodes:\n    leaf1: {}\n")
    first = LabPoller(topology_path=topology, lab_prefix="clab-x", broadcast=None)
    second = LabPoller(topology_path=topology, lab_prefix="clab-x", broadcast=None)
    assert isinstance(first.epoch, str) and first.epoch
    assert first.epoch != second.epoch

    monkeypatch.setattr(main, "poller", first)
    a = asyncio.run(main.events(since=0))
    b = asyncio.run(main.events(since=0))
    assert a["epoch"] == b["epoch"] == first.epoch


def test_api_state_carries_the_nodes_the_page_draws(monkeypatch):
    """bootstrap() builds the graph from /api/state before the socket opens.
    With no nodes there it builds an empty graph, and the snapshot that follows
    adds every router through cy.add — without a layout, all at the origin."""
    import asyncio
    import main

    p = LabPoller.__new__(LabPoller)
    p.nodes = [{"name": "leaf1", "asn": 65101}, {"name": "spine", "asn": 65100}]
    p.last_state = {}
    monkeypatch.setattr(main, "poller", p)
    body = asyncio.run(main.state())
    assert body["ready"] is True
    assert body["nodes"] == p.nodes


# ---- the inventory: what is running, ordered by the file -------------------

class FakeContainer:
    def __init__(self, name):
        self.name = name


def test_the_inventory_is_what_is_running_not_what_the_file_says(bare_poller, tmp_path):
    """A node added after start-up used to stay invisible until someone
    restarted the dashboard — the README stated it as a limitation."""
    topology = tmp_path / "topology.yml"
    topology.write_text("topology:\n  nodes:\n    isp1: {}\n    companya: {}\n    dashboard: {}\n")
    p = bare_poller(topology_path=topology)

    class Client:
        names = ["clab-test-isp1", "clab-test-companya", "clab-test-dashboard"]

        class containers:
            @staticmethod
            def list(filters=None):
                return [FakeContainer(n) for n in Client.names]

    p.client = Client
    assert [n["name"] for n in p._load_nodes()] == ["isp1", "companya"], "file order, dashboard dropped"

    # a router joins the lab
    Client.names.append("clab-test-leaf9")
    assert [n["name"] for n in p._load_nodes()] == ["isp1", "companya", "leaf9"], (
        "a container the file never mentioned is still a router to poll")

    # and leaves again
    Client.names.remove("clab-test-isp1")
    assert [n["name"] for n in p._load_nodes()] == ["companya", "leaf9"]


def test_the_topology_file_is_optional(bare_poller, tmp_path):
    """It is a presentation preference — which router a reader sees first —
    not the inventory."""
    p = bare_poller(topology_path=tmp_path / "nothing-here.yml")

    class Client:
        class containers:
            @staticmethod
            def list(filters=None):
                return [FakeContainer("clab-test-zebra"), FakeContainer("clab-test-alpha")]

    p.client = Client
    assert [n["name"] for n in p._load_nodes()] == ["alpha", "zebra"], "no file: ordered by name"


def test_a_lab_that_is_not_up_yet_still_draws_its_topology(bare_poller, tmp_path):
    """Discovery answers nothing before the containers start. The file is what
    is left, and a graph with no data beats no graph at all."""
    topology = tmp_path / "topology.yml"
    topology.write_text("topology:\n  nodes:\n    isp1: {}\n    dashboard: {}\n")
    p = bare_poller(topology_path=topology)          # client is None → discovery fails
    assert [n["name"] for n in p._load_nodes()] == ["isp1"]


def test_the_asn_is_no_longer_guessed_from_a_config_file():
    """`^\\s*router bgp (\\d+)` matched the first such line in the file — a
    `router bgp` inside a VRF block, or under different indentation, was read
    as the router's own AS. The number comes from the router now.

    Asked of the SYNTAX TREE, not the text: a first version of this searched
    the source for "router bgp" and failed on the comment that explains why
    the regex was removed. A comment about a thing is not the thing.
    """
    import ast

    src = (Path(__file__).resolve().parents[1] / "app" / "poller.py").read_text()
    tree = ast.parse(src)
    assert "_guess_asn" not in {n.name for n in ast.walk(tree)
                                if isinstance(n, ast.FunctionDef)}, "the guesser is back"

    patterns = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id == "re":
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    patterns.append(arg.value)
    assert not any("bgp" in p.lower() for p in patterns), (
        f"the poller regex-parses router config again: {patterns}")


def test_a_router_that_joins_mid_run_is_polled_without_a_restart(bare_poller, tmp_path):
    """`_load_nodes` answering correctly is not the same as anything CALLING
    it again. Measured: removing the re-read from poll_all left every
    discovery test passing while the dashboard went back to needing a restart.
    """
    import asyncio

    topology = tmp_path / "topology.yml"
    topology.write_text("topology:\n  nodes:\n    isp1: {}\n")

    class Client:
        names = ["clab-test-isp1"]

        class containers:
            @staticmethod
            def list(filters=None):
                return [FakeContainer(n) for n in Client.names]

    sent: list[dict] = []

    async def broadcast(message):
        sent.append(message)

    p = bare_poller(broadcast=broadcast, topology_path=topology, client=Client,
                    nodes=[{"name": "isp1", "asn": None}])
    p._poll_node_sync = lambda n: node({"10.0.0.1": peer()})      # noqa: ARG005

    asyncio.run(LabPoller.poll_all(p))
    assert [n["name"] for n in p.nodes] == ["isp1"]

    Client.names.append("clab-test-leaf9")
    asyncio.run(LabPoller.poll_all(p))
    assert [n["name"] for n in p.nodes] == ["isp1", "leaf9"], (
        "the new router is not being polled — the inventory was read once at start-up")
    assert "leaf9" in (sent[-1]["data"] if sent else {}) or any(
        "leaf9" in (m.get("data") or {}) for m in sent), "and its state never reached the page"


def test_the_node_list_carries_what_each_router_reported(bare_poller, tmp_path):
    """`/api/state` publishes this list. Dropping the config regex without
    filling the number back in from the poll left every node reading
    `asn: None` — measured by ci/check.sh against the live lab:
    `asns=None,None,None,None` where it expected `65001,65100,65200,65002`."""
    import asyncio

    topology = tmp_path / "topology.yml"
    topology.write_text("topology:\n  nodes:\n    isp1: {}\n")

    class Client:
        class containers:
            @staticmethod
            def list(filters=None):
                return [FakeContainer("clab-test-isp1")]

    async def broadcast(message):
        pass

    p = bare_poller(broadcast=broadcast, topology_path=topology, client=Client,
                    nodes=[{"name": "isp1", "asn": None}])
    p._poll_node_sync = lambda n: {                                   # noqa: ARG005
        "summary": {"ipv4Unicast": {"routerId": "10.255.1.1", "as": 65100, "peers": {}}},
        "bgp": {"routes": {}}, "neighbors": {}}

    asyncio.run(LabPoller.poll_all(p))
    assert p.nodes[0]["asn"] == 65100, "the AS the router reported"
    assert p.nodes[0]["routerId"] == "10.255.1.1", "and the identity BGP uses for it"


def test_a_router_that_cannot_be_read_keeps_its_last_known_asn(bare_poller, tmp_path):
    """An unreachable router should not have its label blanked: the last thing
    it said is the honest thing to show, and `lastSeen` is what says how old
    that is."""
    import asyncio

    class Client:
        class containers:
            @staticmethod
            def list(filters=None):
                return [FakeContainer("clab-test-isp1")]

    async def broadcast(message):
        pass

    p = bare_poller(broadcast=broadcast, topology_path=tmp_path / "none.yml", client=Client,
                    nodes=[{"name": "isp1", "asn": 65100, "routerId": "10.255.1.1"}])
    p._poll_node_sync = lambda n: {"error": "vtysh timed out"}        # noqa: ARG005

    asyncio.run(LabPoller.poll_all(p))
    assert p.nodes[0]["asn"] == 65100
    assert p.nodes[0]["routerId"] == "10.255.1.1"


# ---- which build is serving this page -------------------------------------

def test_the_version_endpoint_reports_what_was_baked_in(monkeypatch):
    import asyncio
    import main

    monkeypatch.setenv("DASHBOARD_REVISION", "9a5244173777c79577554d2fc0ed59c77fe3368d")
    monkeypatch.setenv("DASHBOARD_BUILT", "2026-09-22T13:27:25Z")
    body = asyncio.run(main.version())
    assert body["revision"] == "9a5244173777c79577554d2fc0ed59c77fe3368d"
    assert body["short"] == "9a52441", "the header shows the short form"
    assert body["built"] == "2026-09-22T13:27:25Z"
    assert body["source"].startswith("https://"), "the header links the commit somewhere"


def test_an_image_that_was_not_built_by_ci_says_so(monkeypatch):
    """"unknown" is a real answer: it tells a reader the page in front of them
    is not a published build. Inventing a number would be worse than silence."""
    import asyncio
    import main

    monkeypatch.delenv("DASHBOARD_REVISION", raising=False)
    monkeypatch.delenv("DASHBOARD_BUILT", raising=False)
    body = asyncio.run(main.version())
    assert body["revision"] == "unknown"
    assert body["short"] == "unknown", "no 7-character slice of the word 'unknown'"
    assert body["built"] == "unknown"
