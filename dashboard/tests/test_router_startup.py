"""How a router starts, on BOTH paths, and what it is allowed to bind.

Every assertion here corresponds to a mutant that the 213 tests before it let
through: the agent running as root, a root fallback when the drop fails, an
agent bound to 0.0.0.0, a Docker socket mounted by a path the guard's regex
missed, and an image whose default command starts FRR without the agent at all.
"""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_TEXT = (ROOT / "compose" / "docker-compose.yml").read_text()
COMPOSE = yaml.safe_load(COMPOSE_TEXT)
CLAB = yaml.safe_load((ROOT / "simple.clab.yml").read_text())
def read(*parts: str) -> str:
    """Missing is empty, so a missing file fails an assertion about what it
    should say rather than the import of this module."""
    path = ROOT.joinpath(*parts)
    return path.read_text() if path.exists() else ""


CONTAINERFILE = read("router-agent", "Containerfile")
AGENT_START = read("router-agent", "agent-start.sh")
ROUTER_START = read("router-agent", "start.sh")
RENAME = read("ci", "rename-ifaces.sh")


def router_services():
    return {name: svc for name, svc in COMPOSE["services"].items()
            if "FRR_AGENT_ADDR" in (svc.get("environment") or {})}


def clab_routers():
    return {name: node for name, node in CLAB["topology"]["nodes"].items()
            if name != "dashboard"}


# ---- the agent has to start on both paths ---------------------------------

def test_the_images_default_command_starts_the_agent():
    """Measured before the fix: `docker run -e FRR_AGENT_ADDR=... frr-agent:local`
    gave tini, docker-start, watchfrr, mgmtd, zebra, staticd — and no python3.
    The Containerfile installed router-start and set no CMD, so the image kept
    FRR's own `/usr/lib/frr/docker-start`. simple.clab.yml sets no `cmd`, so
    that WAS the containerlab path: four routers, no agents, and a dashboard
    that could read none of them."""
    cmd = re.search(r'^CMD\s+(\[.*\]|\S.*)$', CONTAINERFILE, re.M)
    assert cmd, "the Containerfile sets no CMD, so the image inherits FRR's"
    assert "router-start" in cmd.group(1), cmd.group(1)


def test_the_containerlab_path_does_not_override_that_command():
    for name, node in clab_routers().items():
        assert "cmd" not in node and "exec" not in node, (
            f"{name} overrides the image command; it must then start the agent itself")


def test_both_start_up_paths_call_the_same_agent_script():
    """A security property that only one of the two start-up paths applies is
    not a property. compose runs ci/rename-ifaces.sh; containerlab runs the
    image's CMD, which is router-start. Both call frr-agent-start."""
    assert "/usr/local/bin/frr-agent-start" in ROUTER_START
    assert "/usr/local/bin/frr-agent-start" in RENAME
    for name, svc in router_services().items():
        assert svc["command"] == ["/ci/rename-ifaces.sh"], (name, svc.get("command"))


# ---- the privilege drop ---------------------------------------------------

def test_the_agent_runs_as_frr_and_never_falls_back_to_root():
    """Both mutants survived the whole suite: dropping the `su` so the agent
    runs as root, and adding `|| /usr/local/bin/frr-agent` so it falls back to
    root when the drop fails."""
    launches = [ln for ln in AGENT_START.splitlines()
                if "$AGENT" in ln or "frr-agent\"" in ln or "/usr/local/bin/frr-agent" in ln]
    launches = [ln for ln in launches if not ln.strip().startswith("#")]
    runs = [ln for ln in launches if "su -s" in ln]
    assert runs, f"nothing drops privilege before running the agent: {launches}"
    for ln in runs:
        assert re.search(r"su -s /bin/sh frr -c", ln), ln
        assert "||" not in ln, f"a root fallback: {ln}"
    # and no line anywhere launches it without the su
    for ln in launches:
        if "su -s" in ln or ln.strip().startswith("AGENT="):
            continue
        assert "exec" not in ln and "&" not in ln, f"an unguarded launch: {ln}"


def test_no_start_up_script_runs_the_agent_outside_the_one_that_drops_privilege():
    for text, where in ((ROUTER_START, "start.sh"), (RENAME, "rename-ifaces.sh")):
        for ln in text.splitlines():
            if ln.strip().startswith("#"):
                continue
            assert "/usr/local/bin/frr-agent " not in ln + " " or "frr-agent-start" in ln, \
                f"{where} launches the agent itself: {ln}"


# ---- where it may listen, and who may ask ---------------------------------

@pytest.mark.parametrize("where,envs", [
    ("compose", [(n, s["environment"]) for n, s in router_services().items()]),
    ("clab", [(n, node["env"]) for n, node in clab_routers().items()]),
])
def test_no_router_binds_the_wildcard_address(where, envs):
    """`FRR_AGENT_ADDR: 0.0.0.0:8080` in either file survived the whole suite —
    the exact configuration the agent's docstring says it exists to prevent. It
    is only prevented by OMISSION; writing it was never checked."""
    assert envs, where
    for name, env in envs:
        addr = env["FRR_AGENT_ADDR"]
        host = addr.rsplit(":", 1)[0]
        assert host not in ("0.0.0.0", "::", "[::]", ""), (where, name, addr)


@pytest.mark.parametrize("where,envs", [
    ("compose", [(n, s["environment"]) for n, s in router_services().items()]),
    ("clab", [(n, node["env"]) for n, node in clab_routers().items()]),
])
def test_every_router_says_who_may_ask(where, envs):
    """The bind address is not a boundary — measured from a transit-link-only
    host. An agent with no allow-list does not start; one configured here has
    to name the management subnet and nothing wider."""
    for name, env in envs:
        allow = env.get("FRR_AGENT_ALLOW")
        assert allow, (where, name, "no FRR_AGENT_ALLOW")
        assert allow.strip() != "*", (where, name, "allows everybody")
        for cidr in allow.split(","):
            assert cidr.strip().startswith("172.22.20."), (where, name, cidr)


# ---- the socket stays gone, by any spelling -------------------------------

def test_no_path_mounts_the_docker_socket_under_any_name():
    """The previous guard matched the literal pair
    `/var/run/docker.sock:/var/run/docker.sock`. Measured: adding
    `- /run/docker.sock:/var/run/docker.sock` — the same socket, the path many
    hosts actually use — put the whole host back into the dashboard and the
    suite stayed green. What matters is the DESTINATION, not the source."""
    offenders = []
    for name in ("README.md", "dashboard/README.md", "simple.clab.yml",
                 "compose/docker-compose.yml", "router-agent/README.md"):
        for n, line in enumerate((ROOT / name).read_text().splitlines(), 1):
            if "docker.sock" not in line:
                continue
            # A mount is a source and a destination separated by a colon, and
            # the destination is what the container ends up holding.
            if re.search(r"[\w${}./-]+:\s*/var/run/docker\.sock(:[\w,]+)?\s*$",
                         line.strip().lstrip("- ")):
                offenders.append(f"{name}:{n}: {line.strip()[:80]}")
            if re.search(r"source:\s*\S*docker\.sock", line):
                offenders.append(f"{name}:{n}: {line.strip()[:80]}")
    assert not offenders, offenders


def test_the_dashboard_service_declares_no_socket_at_all():
    for volume in COMPOSE["services"]["dashboard"].get("volumes", []):
        assert "docker.sock" not in str(volume), volume
