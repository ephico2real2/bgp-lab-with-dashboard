"""What the agent costs a router, and who is allowed to spend it.

test_agent.py answers "can a request become a command?" — no. These answer the
three questions it does not ask: how much of the router one unauthenticated
client can take, what happens to the bytes of a request the agent refuses, and
whether the bind address is a boundary. Every number quoted here was measured
against the running lab before the fix.
"""
import ipaddress
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "router-agent"))

import agent  # noqa: E402


@pytest.fixture
def served(monkeypatch):
    """A live agent on a loopback port, with vtysh recorded rather than run."""
    ran: list[str] = []

    def fake(command, timeout=agent.TIMEOUT):
        ran.append(command)
        return 200, json.dumps({"ran": command}), ""

    monkeypatch.setattr(agent, "run_vtysh", fake)
    # Fall back to the plain stdlib server when the agent has no AgentServer:
    # the smuggling and timeout tests below are about the Handler, and they
    # must FAIL against a build without the ceiling rather than error on a
    # missing name.
    server_cls = getattr(agent, "AgentServer", None)
    if server_cls is None:
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), agent.Handler)
        srv.max_connections = 0
        srv.refused_full = 0
        srv.refused_source = 0
    else:
        srv = server_cls(("127.0.0.1", 0), agent.Handler,
                         [ipaddress.ip_network("127.0.0.0/8")])
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv, ran
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def raw(port, payload: bytes, wait: float = 2.0) -> bytes:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(payload)
    s.settimeout(wait)
    out = b""
    try:
        while True:
            b = s.recv(65536)
            if not b:
                break
            out += b
    except (TimeoutError, OSError):
        pass
    s.close()
    return out


# ---- the request body is not inert ----------------------------------------

def test_a_refused_post_does_not_run_the_command_in_its_body(served):
    """Measured before the fix: `POST /nope` with `GET /show/bgp-summary` in
    its body returned `405 read-only` AND ran `show ip bgp summary json`. The
    body was never read, so on a keep-alive connection the stdlib parsed it as
    the next request — the 405 said the verb does not exist while the verb's
    payload executed behind it."""
    srv, ran = served
    body = b"GET /show/bgp-summary HTTP/1.1\r\nHost: x\r\n\r\n"
    out = raw(srv.server_address[1],
              b"POST /nope HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
    assert ran == [], f"the smuggled request ran: {ran}"
    assert out.count(b"HTTP/1.1") == 1, out[:400]


def test_one_request_cannot_carry_four(served):
    """Measured before the fix: one `GET /healthz` with four requests in its
    body produced five responses and four vtysh runs. One TCP write, four forks
    on a router — the cheapest amplification there is."""
    srv, ran = served
    body = b"".join(b"GET /show/%s HTTP/1.1\r\nHost: x\r\n\r\n" % v.encode()
                    for v in ["bgp-summary", "bgp-detail", "ip-route", "interface"])
    out = raw(srv.server_address[1],
              b"GET /healthz HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
    assert ran == [], f"the smuggled requests ran: {ran}"
    assert out.count(b"HTTP/1.1") == 1, out[:400]


def test_a_chunked_body_is_refused_too(served):
    """Transfer-Encoding is the same hole by another header: BaseHTTPRequestHandler
    does not decode chunks either, so the chunk data is left in the stream."""
    srv, ran = served
    body = b"2c\r\nGET /show/ip-route HTTP/1.1\r\nHost: x\r\n\r\n\r\n0\r\n\r\n"
    out = raw(srv.server_address[1],
              b"POST /nope HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n" + body)
    assert ran == []
    assert b"400" in out.split(b"\r\n")[0], out[:200]


# ---- one client cannot take the router ------------------------------------

def test_a_half_written_request_does_not_own_a_thread_for_ever(served):
    """Measured before the fix, on the running lab: one client trickling
    unfinished request lines took clab-simple-lab-isp1's agent from 1 thread to
    696 and its address space from 0.6 GB to 1.48 GB, on a container with
    neither a pids nor a memory limit, and nothing ever timed out."""
    srv = served[0]
    assert agent.Handler.timeout is not None, (
        "Handler.timeout is None, so socketserver sets no socket timeout and "
        "the `except TimeoutError` in handle_one_request is unreachable code")
    assert agent.Handler.timeout <= 30

    s = socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=5)
    s.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n")   # headers never terminated
    s.settimeout(agent.Handler.timeout + 10)
    t0 = time.monotonic()
    data = s.recv(65536)                    # b"" when the agent drops us
    held = time.monotonic() - t0
    s.close()
    assert held < agent.Handler.timeout + 5, f"held for {held:.1f}s"


def test_the_number_of_connections_is_bounded(served):
    """A ceiling the accept path enforces, so a refused connection costs a
    verify_request call and not 8 MB of thread stack."""
    srv = served[0]
    srv.max_connections = 4
    port = srv.server_address[1]
    held = []
    try:
        for _ in range(4):
            s = socket.create_connection(("127.0.0.1", port), timeout=5)
            s.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n")   # unfinished: holds a slot
            held.append(s)
        time.sleep(0.5)
        extra = socket.create_connection(("127.0.0.1", port), timeout=5)
        extra.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
        extra.settimeout(3)
        # A refusal at accept time closes the socket: the client sees EOF or a
        # reset, never a response.
        try:
            served_bytes = extra.recv(100)
        except ConnectionResetError:
            served_bytes = b""
        assert served_bytes == b"", "the 5th connection was served past the ceiling"
        extra.close()
        assert srv.refused_full >= 1
    finally:
        for s in held:
            s.close()


# ---- the bind address is not a boundary -----------------------------------

def test_a_source_outside_the_allow_list_is_refused(monkeypatch):
    """Measured on the running lab BEFORE this existed: a container holding only
    the companya-isp1 transit interface (10.0.10.4/29), after one
    `ip route add 172.22.20.0/24 via 10.0.10.2`, fetched
    `http://172.22.20.13:8080/show/bgp-summary` from isp1 and got the router's
    peer table. Linux accepts a packet addressed to any local address on any
    interface, so `FRR_AGENT_ADDR: 172.22.20.13:8080` keeps nobody out."""
    ran: list[str] = []
    monkeypatch.setattr(agent, "run_vtysh",
                        lambda c, timeout=agent.TIMEOUT: (ran.append(c), (200, "{}", ""))[1])
    # 127.0.0.1 is the client; the allow-list names a network it is not in.
    srv = agent.AgentServer(("127.0.0.1", 0), agent.Handler,
                            [ipaddress.ip_network("172.22.20.0/24")])
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        # URLError and ConnectionResetError are both OSError: the refusal is at
        # accept time, so whether the client sees EOF or a reset is the
        # platform's business, not the agent's.
        with pytest.raises(OSError):
            urllib.request.urlopen(
                f"http://127.0.0.1:{srv.server_address[1]}/show/bgp-summary", timeout=5)
        assert ran == []
        assert srv.refused_source >= 1
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def test_the_allow_list_is_required_not_defaulted(monkeypatch):
    """The same rule FRR_AGENT_ADDR already follows, for the same reason: a
    default of everybody is what was wrong before.

    In a thread with a join deadline, because the failure mode being ruled out
    is main() SERVING: a build that defaults the allow-list binds the socket
    and calls serve_forever(), and an inline call would hang this suite rather
    than fail it.
    """
    monkeypatch.setenv("FRR_AGENT_ADDR", "127.0.0.1:0")
    monkeypatch.delenv("FRR_AGENT_ALLOW", raising=False)
    result: list = []
    worker = threading.Thread(target=lambda: result.append(agent.main()), daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "main() started serving with no allow-list"
    assert result == [2], result


@pytest.mark.parametrize("value,client,ok", [
    ("172.22.20.0/24", "172.22.20.100", True),
    ("172.22.20.0/24", "10.0.10.4", False),
    ("172.22.20.0/24", "::ffff:172.22.20.100", True),   # v4 on a dual-stack socket
    ("*", "10.0.10.4", True),
    ("172.22.20.100/32,172.22.20.101/32", "172.22.20.101", True),
    ("172.22.20.100/32", "172.22.20.101", False),
])
def test_parse_allow_and_permitted(value, client, ok):
    assert agent.permitted(client, agent.parse_allow(value)) is ok


# ---- the allow-list is a SET, not a prefix --------------------------------

def test_the_views_are_exactly_these_five():
    """`startswith("show ")` is not a safety property. `show running-config` is
    a `show` and it prints the router's entire configuration — every
    `neighbor ... password` in it. Adding a view has to change this line too,
    in the same diff, under the same review."""
    assert set(agent.SHOW_COMMANDS) == set(agent.EXPECTED_VIEWS)
    assert agent.SHOW_COMMANDS == {
        "bgp-summary": "show ip bgp summary json",
        "bgp-detail": "show ip bgp detail json",
        "bgp-neighbors": "show bgp neighbors json",
        "ip-route": "show ip route json",
        "interface": "show interface brief json",
    }


def test_no_view_reads_the_configuration():
    for name, command in agent.SHOW_COMMANDS.items():
        assert "running-config" not in command, name
        assert "startup-config" not in command, name
