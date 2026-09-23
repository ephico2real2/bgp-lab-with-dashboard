#!/usr/bin/env python3
"""A read-only HTTP front for FRR's vtysh.

The dashboard used to reach every router through the host's Docker socket:
`docker exec <container> vtysh -c '<command>'`. That socket is the whole host
— any container, any image, any volume, and `docker exec` into any of them —
handed to a web page with no authentication. This agent is what replaces it.

Four properties, in the order they matter:

1. NOTHING FROM THE REQUEST REACHES A COMMAND LINE. The URL names a key of a
   fixed dictionary; the value is what runs. There is no path where a caller's
   bytes are concatenated into a command, so there is nothing to escape and
   nothing to get wrong later.
2. EVERY COMMAND IS A `show`, AND A NAMED ONE. The allow-list holds exactly
   what the dashboard reads and nothing else: no `configure`, no `clear`, no
   `write` — and no `show running-config` either, which is a `show` and is the
   router's whole configuration. `EXPECTED_VIEWS` below pins the set so that
   adding one is a test failure and therefore a decision.
3. IT ANSWERS ONLY THE MANAGEMENT NETWORK. A bind address is NOT a boundary:
   measured on this lab, a container holding only a transit-link interface,
   with one `ip route add 172.22.20.0/24 via 10.0.10.2`, fetched
   `/show/bgp-summary` from isp1's management address — Linux accepts a packet
   for any local address on any interface. FRR_AGENT_ALLOW is the boundary, and
   like FRR_AGENT_ADDR it is required rather than defaulted, because a default
   of "everybody" is the thing that was wrong before.
4. A CLIENT CANNOT SPEND MORE OF THE ROUTER THAN IT IS GIVEN. Measured before
   this was bounded: one client trickling unfinished request lines took the
   agent from 1 thread to 696 and its address space from 0.6 GB to 1.48 GB, on
   a router container with neither a pids nor a memory limit. Every connection
   now has a read timeout, a lifetime, and a slot it must be given.
"""
import ipaddress
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The allow-list. A URL names a KEY; the value is the only string that ever
# reaches vtysh. Adding an entry is a deliberate act with a code review.
SHOW_COMMANDS = {
    "bgp-summary": "show ip bgp summary json",
    # the detail variant: the trimmed `show ip bgp json` returns paths with no
    # community or large-community fields
    "bgp-detail": "show ip bgp detail json",
    "bgp-neighbors": "show bgp neighbors json",
    "ip-route": "show ip route json",
    "interface": "show interface brief json",
}

# The set, pinned. "starts with `show`" is not a safety property: `show
# running-config` starts with `show` and prints the router's entire
# configuration, `neighbor ... password` included. A new view has to change
# this line too, in the same diff, under the same review.
EXPECTED_VIEWS = frozenset(
    {"bgp-summary", "bgp-detail", "bgp-neighbors", "ip-route", "interface"})

TIMEOUT = float(os.environ.get("FRR_AGENT_TIMEOUT", "5"))
# How long a client may be silent mid-request before its socket is dropped.
REQUEST_TIMEOUT = float(os.environ.get("FRR_AGENT_REQUEST_TIMEOUT", "10"))
# How long one keep-alive connection may live, however well behaved.
CONNECTION_LIFETIME = float(os.environ.get("FRR_AGENT_CONNECTION_LIFETIME", "60"))
# Concurrent connections. The dashboard opens at most one per view per poll.
MAX_CONNECTIONS = int(os.environ.get("FRR_AGENT_MAX_CONNECTIONS", "32"))


def parse_allow(value: str) -> list:
    """The networks a connection may come from. `*` is the explicit everybody."""
    if value.strip() == "*":
        return [ipaddress.ip_network("0.0.0.0/0"), ipaddress.ip_network("::/0")]
    nets = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        nets.append(ipaddress.ip_network(part, strict=False))
    if not nets:
        raise ValueError(f"FRR_AGENT_ALLOW names no network: {value!r}")
    return nets


def permitted(client_ip: str, networks: list) -> bool:
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    # A v4 client reaching a dual-stack socket arrives as ::ffff:a.b.c.d; the
    # operator wrote 172.22.20.0/24, and both have to mean the same host.
    candidates = [addr]
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        candidates.append(mapped)
    return any(c in net for c in candidates for net in networks
               if c.version == net.version)


def run_vtysh(command: str, timeout: float = TIMEOUT) -> tuple[int, str, str]:
    """Run one allow-listed command. Never called with caller-supplied text."""
    try:
        p = subprocess.run(["vtysh", "-c", command], capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # A wedged vtysh must not pin the handler: the dashboard polls every
        # two seconds and would queue behind it for ever. CPython kills the
        # child and waits for it (subprocess.run, the POSIX branch), so the
        # process is gone and its pipes are closed by the Popen context
        # manager before this returns.
        return 504, "", f"vtysh timed out after {timeout}s"
    except FileNotFoundError:
        return 500, "", "vtysh is not on PATH"
    if p.returncode != 0:
        return 502, "", (p.stderr or p.stdout or "vtysh failed").strip()[:400]
    return 200, p.stdout, ""


class Handler(BaseHTTPRequestHandler):
    server_version = "frr-agent"
    protocol_version = "HTTP/1.1"
    # socketserver applies this to the connection, so a client that stops
    # writing mid-request loses its socket instead of keeping a thread. Without
    # it the `except TimeoutError` below is unreachable code: measured, 200
    # half-written requests held 200 threads indefinitely.
    timeout = REQUEST_TIMEOUT
    # A class attribute, not only an instance one: a handler built without
    # setup() — the disconnect tests build one with no socket at all — must
    # still work. setup() replaces it with the real deadline.
    _deadline = float("inf")

    def setup(self) -> None:
        super().setup()
        self._deadline = time.monotonic() + CONNECTION_LIFETIME

    def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # The name in a 404 is echoed as JSON data; say so, so that no browser
        # or proxy downstream gets to decide it was something else.
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send(status, json.dumps({"error": message}).encode())

    def _body_refused(self) -> bool:
        """A read-only API takes no request body — and an unread one is not inert.

        Left in the stream on a keep-alive connection the body is parsed as the
        NEXT request: measured, `POST /nope` with `GET /show/bgp-summary` in its
        body returned the 405 that says the verb does not exist and then ran
        bgp-summary, and one `GET /healthz` carrying four requests ran all four.
        Refusing and closing is the only answer that leaves nothing behind.
        """
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            self._error(400, "this agent accepts no request body")
            return True
        raw = self.headers.get("Content-Length")
        if raw is None:
            return False
        try:
            length = int(raw)
        except ValueError:
            length = -1
        # `Content-Length: 0` is what urllib puts on a bodiless POST. It leaves
        # nothing in the stream, so it is not the hole; a length that is not a
        # number is worse than one that is, because nobody knows how many bytes
        # follow.
        if length == 0:
            return False
        self.close_connection = True
        self._error(400, "this agent accepts no request body")
        return True

    def do_GET(self) -> None:                                  # noqa: N802
        if self._body_refused():
            return
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._send(200, b'{"ok":true}')
            return
        if path == "/show":
            self._send(200, json.dumps(sorted(SHOW_COMMANDS)).encode())
            return
        if not path.startswith("/show/"):
            self._error(404, "not found")
            return
        name = path[len("/show/"):]
        command = SHOW_COMMANDS.get(name)
        if command is None:
            # The name is echoed back as DATA, never run. This is the branch a
            # caller reaches by trying `/show/../../etc/passwd` or
            # `/show/summary; reload`: it is a dictionary miss, nothing more.
            self._error(404, f"no such view: {name!r}")
            return
        status, out, err = run_vtysh(command)
        if status != 200:
            self._error(status, err)
            return
        self._send(200, out.encode())

    def do_POST(self) -> None:                                 # noqa: N802
        # There is no write path at all, so this is not a 403 with a story —
        # the verb simply does not exist here. The connection closes with it:
        # a 405 that leaves the body in the stream is a 405 in name only.
        self.close_connection = True
        if self._body_refused():
            return
        self._error(405, "read-only")

    do_PUT = do_DELETE = do_PATCH = do_POST

    def handle_one_request(self) -> None:
        # A client that hangs up mid-request is not an error. Left to the
        # stdlib this prints a full traceback per disconnect, which in a lab
        # means a router's logs fill with alarming-looking noise the first
        # time someone closes a browser tab or a health check times out.
        if time.monotonic() > self._deadline:
            # A per-read timeout bounds silence, not a connection: a client
            # that sends one legal byte inside every window keeps a slot for
            # ever. This is the ceiling on the whole conversation.
            self.close_connection = True
            return
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            self.close_connection = True

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("frr-agent %s\n" % (fmt % args))


class AgentServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a source allow-list and a connection ceiling.

    Both are refusals at accept time, before a thread exists: a rejected
    connection costs one `verify_request` call, not 8 MB of stack.
    """
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, allow, max_connections=MAX_CONNECTIONS):
        self.allow = allow
        self.max_connections = max_connections
        self._lock = threading.Lock()
        self._open = 0
        self.refused_source = 0
        self.refused_full = 0
        super().__init__(address, handler)

    def verify_request(self, request, client_address) -> bool:
        if not permitted(client_address[0], self.allow):
            with self._lock:
                self.refused_source += 1
            sys.stderr.write(
                f"frr-agent: refused {client_address[0]}: not in FRR_AGENT_ALLOW\n")
            return False
        with self._lock:
            if self._open >= self.max_connections:
                self.refused_full += 1
                sys.stderr.write(
                    f"frr-agent: refused {client_address[0]}: "
                    f"{self._open} connections already open\n")
                return False
            self._open += 1
        return True

    def process_request_thread(self, request, client_address) -> None:
        # Paired with the increment in verify_request: socketserver calls this
        # only on the path where verify_request returned True.
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._lock:
                self._open -= 1

    def process_request(self, request, client_address) -> None:
        try:
            super().process_request(request, client_address)
        except Exception:
            # A thread that could not be started still holds a slot otherwise.
            with self._lock:
                self._open -= 1
            raise


def parse_addr(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"FRR_AGENT_ADDR must be host:port, got {value!r}")
    return host, int(port)


def main() -> int:
    addr = os.environ.get("FRR_AGENT_ADDR", "")
    if not addr:
        # Required, not defaulted. A default of 0.0.0.0 would put a router's
        # agent on the data plane the first time someone forgot to set it.
        sys.stderr.write("frr-agent: FRR_AGENT_ADDR is required (host:port)\n")
        return 2
    allow_raw = os.environ.get("FRR_AGENT_ALLOW", "")
    if not allow_raw.strip():
        # Required for the same reason, and measured for a different one: the
        # bind address does not keep a transit-link peer out. A router that
        # cannot say who may ask does not answer.
        sys.stderr.write(
            "frr-agent: FRR_AGENT_ALLOW is required (CIDRs, or '*' to mean "
            "everybody); binding to a management address does not keep a peer "
            "on a transit link out\n")
        return 2
    host, port = parse_addr(addr)
    allow = parse_allow(allow_raw)
    server = AgentServer((host, port), Handler, allow)
    sys.stderr.write(
        f"frr-agent: listening on {host}:{port}, {len(SHOW_COMMANDS)} views, "
        f"from {','.join(str(n) for n in allow)}, "
        f"max {MAX_CONNECTIONS} connections\n")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
