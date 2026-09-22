"""The per-tick signal: what each session DID between two polls.

The fixtures are real FRR output. `neighbors-t0.json` is trimmed to the fields
the poller reads, captured with the same `show bgp neighbors json` the poller
runs.
"""
import json
import sys
from collections import deque
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from poller import LabPoller  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
PEER = "10.200.1.3"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def node(summary: dict, neighbors: dict | None = None) -> dict:
    return {"leaf1": {"summary": summary, "bgp": {"routes": {}}, "neighbors": neighbors}}


def summary_with(**over) -> dict:
    peer = {
        "remoteAs": 65100, "state": "Established", "peerUptime": "00:10:00",
        "msgRcvd": 100, "msgSent": 100, "inq": 0, "outq": 0,
        "pfxRcd": 2, "pfxSnt": 6, "connectionsDropped": 3,
    }
    peer.update(over)
    return {"ipv4Unicast": {"routerId": "10.200.255.11", "as": 65101, "peers": {PEER: peer}}}


def sig(prev: dict, curr: dict) -> dict:
    return LabPoller._signal(prev, curr)["leaf1"][PEER]


def test_the_first_sight_of_a_session_reports_no_delta():
    """There is nothing to subtract yet. Reporting zero would read as silence."""
    s = sig({}, node(summary_with()))
    assert s["hasDelta"] is False
    assert (s["dRcvd"], s["dSent"], s["dPfxRcd"], s["dPfxSnt"]) == (0, 0, 0, 0)


def test_a_second_tick_measures_the_interval():
    prev = node(summary_with())
    curr = node(summary_with(msgRcvd=104, msgSent=103, pfxRcd=4))
    s = sig(prev, curr)
    assert s["hasDelta"] is True
    assert (s["dRcvd"], s["dSent"], s["dPfxRcd"], s["dPfxSnt"]) == (4, 3, 2, 0)
    assert s["flaps"] == 3, "FRR's own connectionsDropped survives between polls"


def test_a_counter_that_went_backwards_is_a_reset_not_a_negative_pulse():
    """bgpd restarts these at zero. There is no interval to measure, so the
    tick reports no delta — a negative pulse and a clamped zero are both
    inventions, and the flap is still visible in `flaps`."""
    prev = node(summary_with(msgRcvd=5000, msgSent=5000))
    curr = node(summary_with(msgRcvd=2, msgSent=2, connectionsDropped=4))
    s = sig(prev, curr)
    assert s["hasDelta"] is False
    assert (s["dRcvd"], s["dSent"]) == (0, 0)
    assert s["flaps"] == 4


def test_a_withdrawal_is_a_signed_prefix_delta():
    """pfxRcd falls while msgRcvd RISES — the withdrawal is itself an update —
    so this is not a reset and the guard must not fire."""
    prev = node(summary_with(pfxRcd=2))
    curr = node(summary_with(msgRcvd=103, msgSent=102, pfxRcd=1))
    s = sig(prev, curr)
    assert s["hasDelta"] is True
    assert s["dPfxRcd"] == -1, "a withdrawal is a signed change, never a magnitude"
    assert s["dRcvd"] == 3


def test_timers_come_from_frr_when_the_peer_is_established():
    curr = node(summary_with(), load("neighbors-t0.json"))
    s = sig({}, curr)
    assert s["hasTimers"] is True
    assert s["quietMsec"] == 1000
    assert s["holdMsec"] == 9000
    assert s["keepaliveMsec"] == 3000


def test_timers_are_not_taken_from_a_peer_that_is_not_established():
    """FRR emits bgpTimerLastRead for every peer, and for one that never came
    up it is the peer's AGE: measured on 10.5.3, bgpState "Active" with
    bgpTimerLastRead 14000 against a holdMsec of 9000. It also wraps every 24h,
    so a peer down for exactly a day reads 0 — which a heartbeat would draw as
    "it just spoke"."""
    neighbors = {PEER: {"bgpState": "Active", "bgpTimerLastRead": 14000,
                        "bgpTimerHoldTimeMsecs": 9000, "bgpTimerKeepAliveIntervalMsecs": 3000}}
    s = sig({}, node(summary_with(state="Active"), neighbors))
    assert s["hasTimers"] is False
    assert (s["quietMsec"], s["holdMsec"], s["keepaliveMsec"]) == (0, 0, 0)


def test_a_peer_established_in_the_summary_but_not_in_neighbors_gets_no_timers():
    """The two views are separate polls and can disagree mid-transition."""
    neighbors = {PEER: {"bgpState": "OpenConfirm", "bgpTimerLastRead": 500,
                        "bgpTimerHoldTimeMsecs": 9000}}
    assert sig({}, node(summary_with(), neighbors))["hasTimers"] is False


def test_a_missing_neighbors_view_leaves_the_session_without_timers():
    """The call is non-fatal: the router is still up, it just has no heartbeat.
    Zero here must mean "not measured", which is what hasTimers says."""
    s = sig({}, node(summary_with(), None))
    assert s["hasTimers"] is False
    assert s["quietMsec"] == 0
    assert s["msgRcvd"] == 100, "the rest of the signal still arrives"


def test_a_dynamic_peer_is_marked():
    """`bgp listen range` peers are servers, not fabric routers."""
    assert sig({}, node(summary_with(dynamicPeer=True)))["dynamic"] is True
    assert sig({}, node(summary_with()))["dynamic"] is False


def test_the_signal_survives_an_empty_or_malformed_poll():
    for state in ({}, {"leaf1": {}}, {"leaf1": {"summary": None, "neighbors": None}}):
        LabPoller._signal({}, state)


@pytest.mark.parametrize("field", ["msgRcvd", "msgSent", "inq", "outq", "pfxRcd", "pfxSnt"])
def test_every_raw_counter_is_carried_through(field):
    s = sig({}, node(summary_with(**{field: 42})))
    assert s[field] == 42


def test_the_signal_is_broadcast_on_a_tick_that_changes_nothing():
    """The regression this frame exists to prevent.

    A full state frame goes out only when the signature changes — the graph or
    the routes. The signal changes every tick by design, and carried on the
    state frame alone it would never reach a page watching a healthy fabric:
    the heartbeat would sit frozen while the fabric was perfectly fine.
    """
    import asyncio

    sent: list[dict] = []

    async def broadcast(message):
        sent.append(message)

    poller = LabPoller.__new__(LabPoller)          # no docker client wanted here
    poller.broadcast = broadcast
    poller.nodes = [{"name": "leaf1", "asn": 65101}]
    poller.last_state = {}
    poller.last_signature = None
    # __init__ is skipped above, so the event ring has to be built by hand:
    # poll_all records every event it broadcasts.
    poller.events = deque(maxlen=500)
    poller.last_event_id = 0

    async def drive(states):
        for st in states:
            poller._poll_node_sync = lambda n, s=st: s          # noqa: ARG005
            await LabPoller.poll_all(poller)

    first = {"summary": summary_with(), "bgp": {"routes": {}}, "neighbors": None}
    # identical topology, counters advanced: the signature must not move
    second = {"summary": summary_with(msgRcvd=108, msgSent=107), "bgp": {"routes": {}}, "neighbors": None}

    asyncio.run(drive([first, second]))

    kinds = [m["type"] for m in sent]
    assert kinds.count("state") == 1, f"state should be sent once, got {kinds}"
    assert kinds.count("signal") == 2, f"the signal must go out every tick, got {kinds}"

    last = sent[-1]
    assert last["type"] == "signal"
    peer = last["data"]["leaf1"][PEER]
    assert peer["hasDelta"] is True and peer["dRcvd"] == 8


def test_a_quiet_tick_is_a_measured_zero_not_unmeasured():
    """Equal counters ARE a measurement: nothing was said. Read as a reset
    (`>` instead of `>=`) every idle session would show "unmeasured" on every
    tick, which is the one word the Traffic view reserves for a reading it
    did not take."""
    s = sig(node(summary_with()), node(summary_with()))
    assert s["hasDelta"] is True
    assert (s["dRcvd"], s["dSent"], s["dPfxRcd"], s["dPfxSnt"]) == (0, 0, 0, 0)


def test_the_delta_is_per_tick_even_when_the_signature_did_not_change():
    """poll_all short-circuits when the signature is unchanged, and that path
    has to advance last_state too: the signal is subtracted from the PREVIOUS
    tick, not from the last tick that changed the topology. Otherwise a quiet
    fabric's deltas grow every poll and every idle session reads as moving."""
    import asyncio

    sent: list[dict] = []

    async def broadcast(message):
        sent.append(message)

    p = LabPoller.__new__(LabPoller)
    p.broadcast = broadcast
    p.nodes = [{"name": "leaf1", "asn": 65101}]
    p.last_state, p.last_signature = {}, None
    p.events, p.last_event_id = deque(maxlen=500), 0
    ticks = [node(summary_with(msgRcvd=100, msgSent=100)),
             node(summary_with(msgRcvd=104, msgSent=103)),
             node(summary_with(msgRcvd=108, msgSent=106))]

    async def drive():
        for st in ticks:
            p._poll_node_sync = lambda n, s=st: s["leaf1"]      # noqa: ARG005
            await LabPoller.poll_all(p)

    asyncio.run(drive())
    signals = [m["data"]["leaf1"][PEER] for m in sent if m["type"] == "signal"]
    assert [s["hasDelta"] for s in signals] == [False, True, True]
    assert [(s["dRcvd"], s["dSent"]) for s in signals] == [(0, 0), (4, 3), (4, 3)]
