"""What the poller will accept from the thing answering at a router's address.

`exec_timeout` exists for one stated reason — "one wedged router must not stall
every poll, because poll_all gathers them all". These check that it does that,
against the three shapes a router's answer can take that a 5-second socket
timeout does not cover: slow, huge, and somewhere else.
"""
import http.server
import json
import socketserver
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import poller as poller_mod  # noqa: E402
from poller import LabPoller  # noqa: E402

# Read through the module, not imported by name: the point of the test is the
# behaviour, and a build with no cap at all must fail on the assertion rather
# than on the import.
MAX_RESPONSE_BYTES = getattr(poller_mod, "MAX_RESPONSE_BYTES", 8 * 1024 * 1024)


def serve(handler_cls):
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler_cls)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def poller(tmp_path):
    topology = tmp_path / "topology.yml"
    topology.write_text("topology:\n  nodes:\n    leaf1: {}\n")
    return LabPoller(topology_path=topology, lab_prefix="clab-x",
                     broadcast=None, exec_timeout=2.0)


# ---- slow --------------------------------------------------------------

def test_a_router_that_drips_cannot_hold_the_poll_open(poller):
    """Measured before the fix: `urlopen(timeout=2.0)` against a server writing
    one byte per second was still blocked after 35 seconds, because that
    timeout is per socket operation and not a budget for the exchange. Every
    byte reset it. poll_all gathers all four routers, so one router dripping
    like this froze the whole dashboard — the exact failure the timeout was
    added to prevent."""
    class Drip(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "1000000")
            self.end_headers()
            body = b'{"ipv4Unicast":{"as":1,"peers":{}}}' + b" " * 999965
            for i in range(len(body)):
                try:
                    self.wfile.write(body[i:i + 1])
                    self.wfile.flush()
                except OSError:
                    return
                time.sleep(0.2)

        def log_message(self, *a):
            pass

    srv = serve(Drip)
    budget = poller.exec_timeout * 3
    outcome: list = []

    def call():
        try:
            outcome.append(("returned", poller._get_json(
                f"http://127.0.0.1:{srv.server_address[1]}", "bgp-summary")))
        except BaseException as exc:                 # noqa: BLE001 - recorded, not handled
            outcome.append(("raised", exc))

    # In a thread with a join deadline, not inline: without a deadline this
    # test does not fail on a build with no deadline — it hangs with it, for
    # the 55 hours the drip server would take, which is what the dashboard did.
    worker = threading.Thread(target=call, daemon=True)
    t0 = time.monotonic()
    worker.start()
    worker.join(budget)
    elapsed = time.monotonic() - t0
    srv.shutdown()
    assert not worker.is_alive(), (
        f"still reading after {elapsed:.1f}s with exec_timeout={poller.exec_timeout}")
    kind, value = outcome[0]
    assert kind == "raised", f"a drip was accepted as an answer: {value!r}"
    assert "too slow" in str(value), repr(value)


def test_a_router_that_accepts_and_says_nothing_is_given_up_on(poller):
    """The other half of slow: a socket that connects and never sends a status
    line. The deadline loop cannot help here — it only runs once there is a
    response object — so the socket timeout on the open() itself is what has to
    be there, which is why this is a separate test from the drip."""
    import socket as _socket

    listener = _socket.socket()
    listener.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    held = []

    def accept_and_ignore():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            held.append(conn)          # accepted, never answered

    threading.Thread(target=accept_and_ignore, daemon=True).start()

    outcome: list = []
    worker = threading.Thread(
        target=lambda: outcome.append(_call(poller, listener.getsockname()[1])),
        daemon=True)
    t0 = time.monotonic()
    worker.start()
    worker.join(poller.exec_timeout * 3)
    elapsed = time.monotonic() - t0
    listener.close()
    for c in held:
        c.close()
    assert not worker.is_alive(), (
        f"still waiting for a status line after {elapsed:.1f}s "
        f"with exec_timeout={poller.exec_timeout}")
    assert isinstance(outcome[0], RuntimeError), outcome[0]


def _call(poller, port):
    try:
        return poller._get_json(f"http://127.0.0.1:{port}", "bgp-summary")
    except BaseException as exc:                     # noqa: BLE001 - recorded
        return exc


# ---- huge --------------------------------------------------------------

def test_an_oversized_answer_is_refused_rather_than_read(poller):
    """Measured before the fix: a 60 MB body was accepted in 0.1 s and cost the
    dashboard 309 MB of RSS. `r.read()` with no argument consumes what it is
    handed, and what it is handed is whatever is listening at that address."""
    big = b'{"x":"' + b"A" * (MAX_RESPONSE_BYTES + 1024) + b'"}'

    class Big(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(big)))
            self.end_headers()
            self.wfile.write(big)

        def log_message(self, *a):
            pass

    srv = serve(Big)
    with pytest.raises(RuntimeError) as exc:
        poller._get_json(f"http://127.0.0.1:{srv.server_address[1]}", "bgp-summary")
    srv.shutdown()
    assert "exceeds" in str(exc.value), str(exc.value)


# ---- somewhere else ----------------------------------------------------

def test_a_redirect_is_not_a_routers_answer(poller):
    """Measured before the fix: an agent answering `302 Location: http://other/`
    made the poller fetch that host and publish its body on /api/state, where
    every connected browser read it. urllib follows 3xx by default, to any
    http/https/ftp host."""
    secret = b'{"ipv4Unicast":{"as":4242,"peers":{}}}'

    class Victim(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(secret)))
            self.end_headers()
            self.wfile.write(secret)

        def log_message(self, *a):
            pass

    victim = serve(Victim)

    class Evil(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{victim.server_address[1]}/x")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    evil = serve(Evil)
    with pytest.raises(RuntimeError) as exc:
        poller._get_json(f"http://127.0.0.1:{evil.server_address[1]}", "bgp-summary")
    evil.shutdown()
    victim.shutdown()
    assert "4242" not in str(exc.value)
    assert "302" in str(exc.value), str(exc.value)


# ---- the inventory is an address list, and addresses are checked ---------

def test_a_malformed_routers_entry_is_named_not_dropped(tmp_path, monkeypatch, capsys):
    """`ROUTERS=companya,companyb=http://b:8080` used to produce a dashboard
    with one router on it and nothing anywhere saying where the other went."""
    monkeypatch.setenv("ROUTERS", "companya,companyb=http://b:8080")
    topology = tmp_path / "topology.yml"
    topology.write_text("topology:\n  nodes: {}\n")
    p = LabPoller(topology_path=topology, lab_prefix="x", broadcast=None)
    assert p._agents() == {"companyb": "http://b:8080"}
    assert "companya" in capsys.readouterr().out


@pytest.mark.parametrize("routers,expected", [
    # urlopen serves the file scheme: measured, `a=file:///dir` returned the
    # contents of `<dir>/show/bgp-summary` as a router's answer.
    ("a=file:///etc", {}),
    ("a=ftp://example.com", {}),
    # the host here is evil.example.com, not victim
    ("a=http://victim@evil.example.com:8080", {}),
    # urlsplit resolves this to evil.example.com PORT 80, with the whole
    # `:8080/show/bgp-summary` swallowed into the fragment
    ("a=http://evil.example.com#:8080", {}),
    ("a=http://10.0.0.1:8080/", {"a": "http://10.0.0.1:8080"}),
    ("a=https://router-a:8443", {"a": "https://router-a:8443"}),
])
def test_only_a_plain_http_host_is_an_agent_address(tmp_path, monkeypatch, routers, expected):
    monkeypatch.setenv("ROUTERS", routers)
    topology = tmp_path / "topology.yml"
    topology.write_text("topology:\n  nodes: {}\n")
    p = LabPoller(topology_path=topology, lab_prefix="x", broadcast=None)
    assert p._agents() == expected


def test_a_topology_node_name_cannot_point_the_poller_elsewhere(tmp_path, monkeypatch):
    """With no ROUTERS the address is built from the node's NAME, verbatim:
    `http://{name}:{port}`. A name with a `#` or an `@` in it is a different
    host than it reads as."""
    monkeypatch.delenv("ROUTERS", raising=False)
    topology = tmp_path / "topology.yml"
    topology.write_text(
        'topology:\n'
        '  nodes:\n'
        '    leaf1: {}\n'
        '    "evil.example.com#": {}\n'
        '    "victim@evil.example.com": {}\n')
    p = LabPoller(topology_path=topology, lab_prefix="x", broadcast=None)
    assert p._agents() == {"leaf1": "http://leaf1:8080"}
