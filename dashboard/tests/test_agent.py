"""The show-only agent: what it will run, and what it refuses.

Driven against a real HTTP server on a real socket, with `vtysh` replaced by a
recorder. The point of this component is what it will NOT do, and a test that
only read the source could not tell you whether a request reached a command
line — this one records every command that did.
"""
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
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
    srv = ThreadingHTTPServer(("127.0.0.1", 0), agent.Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        yield base, ran
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def get(url, method="GET"):
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_every_view_runs_exactly_the_command_it_is_named_for(served):
    base, ran = served
    for name, command in agent.SHOW_COMMANDS.items():
        status, body = get(f"{base}/show/{name}")
        assert status == 200, (name, body)
        assert json.loads(body)["ran"] == command
    assert ran == list(agent.SHOW_COMMANDS.values())


def test_every_command_is_a_show():
    """No `configure`, no `clear`, no `write`. A compromised dashboard cannot
    change a router through this agent, because there is no verb here that
    changes one."""
    for name, command in agent.SHOW_COMMANDS.items():
        assert command.startswith("show "), (name, command)
    forbidden = ("conf", "clear", "write", "reload", "shutdown", "no ", "debug")
    for command in agent.SHOW_COMMANDS.values():
        assert not any(command.startswith(f) for f in forbidden), command


@pytest.mark.parametrize("attack", [
    "summary%3B%20reload",                      # summary; reload
    "summary%20%26%26%20clear%20bgp%20%2A",     # summary && clear bgp *
    "../../etc/passwd",
    "..%2f..%2fetc%2fpasswd",
    "bgp-summary%00",
    "bgp-summary%20json%27%20-c%20%27configure%20terminal",
    "%24%28reboot%29",                          # $(reboot)
    "%60reboot%60",                             # `reboot`
    "bgp-summary/../../show",
])
def test_nothing_from_the_url_reaches_a_command_line(served, attack):
    """The URL names a KEY. A miss is a 404 and nothing runs — there is no
    string being built, so there is nothing to escape.

    Percent-encoded, because that is what a client that means it would send:
    urllib refuses to transmit a raw space, which would have made these tests
    pass without the request ever reaching the agent. The literal-byte case is
    below, written straight onto the socket."""
    base, ran = served
    status, _ = get(f"{base}/show/{attack}")
    assert status == 404, attack
    assert ran == [], f"{attack!r} caused {ran!r} to run"


@pytest.mark.parametrize("raw", [
    b"GET /show/summary; reload HTTP/1.1\r\nHost: x\r\n\r\n",
    b"GET /show/bgp-summary json' -c 'configure terminal HTTP/1.1\r\nHost: x\r\n\r\n",
    b"GET /show/bgp-summary%0d%0aX-Injected: 1 HTTP/1.1\r\nHost: x\r\n\r\n",
    b"GET /../../etc/passwd HTTP/1.1\r\nHost: x\r\n\r\n",
])
def test_a_hostile_client_writing_raw_bytes_runs_nothing(served, raw):
    """No client library in the way: the bytes a request would have to contain
    to reach a shell, written onto the socket. Whatever the server makes of
    them, the recorder must stay empty."""
    import socket

    base, ran = served
    host, port = base.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=5) as s:
        s.sendall(raw)
        s.settimeout(5)
        try:
            reply = s.recv(4096)
        except socket.timeout:
            reply = b""
    assert b" 200 " not in reply.split(b"\r\n")[0], reply.split(b"\r\n")[0]
    assert ran == [], f"{raw!r} caused {ran!r} to run"


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_there_is_no_write_verb(served, method):
    base, ran = served
    status, body = get(f"{base}/show/bgp-summary", method=method)
    assert status == 405, body
    assert ran == []


def test_healthz_runs_nothing(served):
    base, ran = served
    status, body = get(f"{base}/healthz")
    assert status == 200 and json.loads(body) == {"ok": True}
    assert ran == [], "liveness should not touch the router"


def test_the_view_list_is_discoverable_and_runs_nothing(served):
    base, ran = served
    status, body = get(f"{base}/show")
    assert status == 200
    assert json.loads(body) == sorted(agent.SHOW_COMMANDS)
    assert ran == []


def test_a_wedged_vtysh_is_killed_rather_than_queued(monkeypatch):
    """The dashboard polls every two seconds. A command that never returns
    would hold the handler and every poll behind it."""
    import subprocess

    def hang(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="vtysh", timeout=kw.get("timeout", 5))

    monkeypatch.setattr(subprocess, "run", hang)
    status, out, err = agent.run_vtysh("show ip bgp summary json", timeout=0.01)
    assert status == 504
    assert "timed out" in err


def test_a_failing_command_reports_the_router_s_words_not_a_crash(monkeypatch):
    import subprocess

    class Result:
        returncode = 1
        stdout = ""
        stderr = "bgpd is not running\n"

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: Result())
    status, out, err = agent.run_vtysh("show ip bgp summary json")
    assert status == 502
    assert err == "bgpd is not running"


def test_the_listen_address_is_required_not_defaulted(monkeypatch, capsys):
    """A default of 0.0.0.0 would put a router's agent on the data plane the
    first time someone forgot to set it."""
    monkeypatch.delenv("FRR_AGENT_ADDR", raising=False)
    assert agent.main() == 2
    assert "required" in capsys.readouterr().err


@pytest.mark.parametrize("value,expected", [
    ("172.22.20.11:8080", ("172.22.20.11", 8080)),
    ("127.0.0.1:9", ("127.0.0.1", 9)),
])
def test_the_address_is_parsed_host_and_port(value, expected):
    assert agent.parse_addr(value) == expected


@pytest.mark.parametrize("value", ["8080", "no-port:", ":8080", "host:port"])
def test_a_malformed_address_is_refused(value):
    with pytest.raises(ValueError):
        agent.parse_addr(value)


@pytest.mark.parametrize("err", [ConnectionResetError, BrokenPipeError, TimeoutError])
def test_a_client_that_hangs_up_is_not_an_error(err):
    """Left to the stdlib, every disconnect prints a traceback — a router's
    logs would fill with them the first time a browser tab closed."""
    class Stub(agent.Handler):
        def __init__(self):                      # no socket, no parsing
            self.close_connection = False

        def _parent(self):
            raise err("gone")

    stub = Stub()
    import http.server

    original = http.server.BaseHTTPRequestHandler.handle_one_request
    try:
        http.server.BaseHTTPRequestHandler.handle_one_request = lambda self: (_ for _ in ()).throw(err("gone"))
        stub.handle_one_request()                # must not raise
    finally:
        http.server.BaseHTTPRequestHandler.handle_one_request = original
    assert stub.close_connection is True, "the connection is not closed after a disconnect"
