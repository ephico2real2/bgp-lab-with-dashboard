"""The poller's first tests.

The fixtures are real FRR output, captured from a running fabric two seconds
apart with the same commands the poller runs (`show ip bgp summary json` and
`show ip bgp detail json`). Nothing changed on that fabric between the two
captures except the clock and the keepalive counters, which is exactly the case
the broadcast short-circuit has to recognise.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from poller import LabPoller  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def two_ticks() -> tuple[dict, dict]:
    """Two polls of the same unchanged fabric, two seconds apart."""
    t0 = {"leaf1": {"summary": load("summary-t0.json"), "bgp": load("bgp-t0.json")}}
    t1 = {"leaf1": {"summary": load("summary-t1.json"), "bgp": load("bgp-t1.json")}}
    return t0, t1


def test_the_fixtures_really_do_differ(two_ticks):
    """Guards the two tests below: if these were equal they would prove nothing."""
    t0, t1 = two_ticks
    assert t0 != t1, "the captures are identical — recapture them further apart"
    p0 = t0["leaf1"]["summary"]["ipv4Unicast"]["peers"]
    p1 = t1["leaf1"]["summary"]["ipv4Unicast"]["peers"]
    moved = {k for ip in p1 for k in p1[ip] if p0.get(ip, {}).get(k) != p1[ip][k]}
    assert moved == {"msgRcvd", "msgSent", "peerUptime", "peerUptimeMsec"}, moved


def test_counters_and_the_clock_do_not_count_as_a_change(two_ticks):
    """The bug: `state == self.last_state` compared FRR's counters.

    peerUptime advances with the clock, so the comparison could never succeed
    and every poll was broadcast to every client. The signature covers what the
    page renders and leaves the counters out.
    """
    t0, t1 = two_ticks
    assert LabPoller._signature(t0) == LabPoller._signature(t1)


def test_a_session_going_down_is_a_change(two_ticks):
    t0, _ = two_ticks
    changed = json.loads(json.dumps(t0))
    peers = changed["leaf1"]["summary"]["ipv4Unicast"]["peers"]
    first = sorted(peers)[0]
    peers[first]["state"] = "Idle"
    assert LabPoller._signature(t0) != LabPoller._signature(changed)


def test_a_withdrawn_prefix_is_a_change(two_ticks):
    """A withdrawal changes no peer state, so a session-only signature misses it
    and the RIB keeps showing a prefix the events pane has reported gone."""
    t0, _ = two_ticks
    changed = json.loads(json.dumps(t0))
    routes = changed["leaf1"]["bgp"]["routes"]
    assert routes, "the fixture carries no routes"
    routes.pop(sorted(routes)[0])
    assert LabPoller._signature(t0) != LabPoller._signature(changed)


def test_a_peer_that_vanishes_is_a_change(two_ticks):
    t0, _ = two_ticks
    changed = json.loads(json.dumps(t0))
    peers = changed["leaf1"]["summary"]["ipv4Unicast"]["peers"]
    peers.pop(sorted(peers)[0])
    assert LabPoller._signature(t0) != LabPoller._signature(changed)


def test_the_signature_does_not_depend_on_iteration_order(two_ticks):
    """FRR's JSON objects are dicts and Python keeps insertion order, so an
    unsorted signature would change when the daemon happened to emit its peers
    in another order — a broadcast storm from nothing at all."""
    t0, _ = two_ticks
    shuffled = json.loads(json.dumps(t0))
    ipv4 = shuffled["leaf1"]["summary"]["ipv4Unicast"]
    ipv4["peers"] = dict(reversed(list(ipv4["peers"].items())))
    routes = shuffled["leaf1"]["bgp"]["routes"]
    shuffled["leaf1"]["bgp"]["routes"] = dict(reversed(list(routes.items())))
    assert LabPoller._signature(t0) == LabPoller._signature(shuffled)


def test_an_unreachable_router_is_a_change_and_back_again(two_ticks):
    t0, _ = two_ticks
    down = {"leaf1": {"error": "container not found"}}
    assert LabPoller._signature(t0) != LabPoller._signature(down)
    assert LabPoller._signature(down) == LabPoller._signature({"leaf1": {"error": "container not found"}})


def test_the_signature_survives_an_empty_or_malformed_poll():
    """A router that answered nothing must not raise: the poller would drop the
    whole tick, including the routers that did answer."""
    for state in ({}, {"leaf1": {}}, {"leaf1": {"summary": None, "bgp": None}},
                  {"leaf1": {"summary": {}, "bgp": {"routes": None}}}):
        LabPoller._signature(state)
