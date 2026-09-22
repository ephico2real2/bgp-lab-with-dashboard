"""A LabPoller with no Docker client, built the same way everywhere.

Three separate tests had grown their own `LabPoller.__new__(LabPoller)` plus a
hand-written list of attributes, and each time the class gained state — the
event ring, then live container discovery — every one of those lists was a
little bit wrong and the failure looked like a bug in the change rather than
an incomplete stand-in. One factory, so the next attribute is added once.

It deliberately does NOT call __init__: that reaches for the Docker daemon,
and none of these tests have or want one.
"""
from collections import deque
from pathlib import Path

import pytest


@pytest.fixture
def bare_poller():
    def build(**overrides):
        from poller import LabPoller

        p = LabPoller.__new__(LabPoller)
        p.broadcast = None
        p.nodes = []
        p.last_state = {}
        p.last_signature = None
        p.events = deque(maxlen=500)
        p.last_event_id = 0
        p.epoch = "epoch-a"
        # Discovery has to fail harmlessly: no client, and a path that is not
        # a topology file. poll_all then keeps whatever `nodes` the test set.
        p.client = None
        p.lab_prefix = "clab-test"
        p.topology_path = Path("/nonexistent/topology.yml")
        p.interval = 0
        p.exec_timeout = 5.0
        for k, v in overrides.items():
            setattr(p, k, v)
        return p

    return build
