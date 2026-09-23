#!/usr/bin/env python3
"""A read-only HTTP front for FRR's vtysh.

The dashboard used to reach every router through the host's Docker socket:
`docker exec <container> vtysh -c '<command>'`. That socket is the whole host
— any container, any image, any volume, and `docker exec` into any of them —
handed to a web page with no authentication. This agent is what replaces it.

Three properties, in the order they matter:

1. NOTHING FROM THE REQUEST REACHES A COMMAND LINE. The URL names a key of a
   fixed dictionary; the value is what runs. There is no path where a caller's
   bytes are concatenated into a command, so there is nothing to escape and
   nothing to get wrong later.
2. EVERY COMMAND IS A `show`. The allow-list holds exactly what the dashboard
   reads and nothing else: no `configure`, no `clear`, no `write`. A compromised
   dashboard cannot change a router through this.
3. IT LISTENS WHERE IT IS TOLD AND NOWHERE ELSE. FRR_AGENT_ADDR is required
   rather than defaulted, so an agent cannot end up on the data plane because
   someone forgot a flag. The management LAN is the boundary; there is no
   authentication, exactly as before — but the thing behind it is now five
   read-only commands instead of the host.
"""
import json
import os
import subprocess
import sys
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

TIMEOUT = float(os.environ.get("FRR_AGENT_TIMEOUT", "5"))


def run_vtysh(command: str, timeout: float = TIMEOUT) -> tuple[int, str, str]:
    """Run one allow-listed command. Never called with caller-supplied text."""
    try:
        p = subprocess.run(["vtysh", "-c", command], capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # A wedged vtysh must not pin the handler: the dashboard polls every
        # two seconds and would queue behind it for ever.
        return 504, "", f"vtysh timed out after {timeout}s"
    except FileNotFoundError:
        return 500, "", "vtysh is not on PATH"
    if p.returncode != 0:
        return 502, "", (p.stderr or p.stdout or "vtysh failed").strip()[:400]
    return 200, p.stdout, ""


class Handler(BaseHTTPRequestHandler):
    server_version = "frr-agent"
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send(status, json.dumps({"error": message}).encode())

    def do_GET(self) -> None:                                  # noqa: N802
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
        # the verb simply does not exist here.
        self._error(405, "read-only")

    do_PUT = do_DELETE = do_PATCH = do_POST

    def handle_one_request(self) -> None:
        # A client that hangs up mid-request is not an error. Left to the
        # stdlib this prints a full traceback per disconnect, which in a lab
        # means a router's logs fill with alarming-looking noise the first
        # time someone closes a browser tab or a health check times out.
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            self.close_connection = True

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("frr-agent %s\n" % (fmt % args))


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
    host, port = parse_addr(addr)
    server = ThreadingHTTPServer((host, port), Handler)
    sys.stderr.write(f"frr-agent: listening on {host}:{port}, {len(SHOW_COMMANDS)} views\n")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
