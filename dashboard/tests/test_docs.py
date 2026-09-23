"""The dashboard's README has to describe the dashboard that exists.

Issue #12 was exactly this class of defect: the doc said the `cose` layout
"reruns on every state diff" when `updateGraph` has never re-run a layout, and
its diagram named `show ip bgp json` while the poller runs `show ip bgp detail
json`. Prose cannot be type-checked, but the FACTS it quotes can be read out
of both sides and compared.
"""
import re
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
README = (Path(__file__).resolve().parents[1] / "README.md").read_text()
MAIN = (APP / "main.py").read_text()
POLLER = (APP / "poller.py").read_text()


AGENT = (Path(__file__).resolve().parents[2] / "router-agent" / "agent.py").read_text()


def code_commands() -> set[str]:
    """What actually runs on a router. The poller no longer runs anything: it
    asks a named VIEW of the show-only agent over HTTP, and the agent's
    allow-list is the only place a command string exists."""
    views = set(re.findall(r'_get_json\(base, "([^"]+)"\)', POLLER))
    allowed = dict(re.findall(r'"([a-z0-9-]+)":\s*"(show [^"]+)"', AGENT))
    missing = views - set(allowed)
    assert not missing, f"the poller asks for views the agent does not serve: {missing}"
    return {allowed[v] for v in views}


def doc_commands() -> set[str]:
    return set(re.findall(r"^\s*(show [a-z0-9 ]*json)\b", README, re.M))


def code_endpoints() -> set[str]:
    return set(re.findall(r'@app\.(?:get|websocket)\("([^"]+)"\)', MAIN))


def code_env() -> set[str]:
    """Both files. The app reads its own settings in main.py and the poller
    reads where the routers are — a guard that looked only at main.py passed
    while ROUTERS, the variable this whole architecture turns on, was
    undocumented."""
    return set(re.findall(r'os\.environ\.get\("([A-Z_]+)"', MAIN + POLLER))


def code_frames() -> set[str]:
    return set(re.findall(r'broadcast\(\{"type": "([a-z]+)"', POLLER)) | {"snapshot"}


def test_every_command_the_doc_shows_is_one_the_poller_runs():
    """The diagram named `show ip bgp json`; the poller runs `show ip bgp
    detail json` — the detail variant, because the trimmed one has no
    communities. A reader following the diagram would have looked for a field
    that command cannot return."""
    assert doc_commands() <= code_commands(), (
        f"the README shows {sorted(doc_commands() - code_commands())}, which the poller never runs")


def test_every_command_the_poller_runs_is_in_the_doc():
    assert code_commands() <= doc_commands(), (
        f"the poller runs {sorted(code_commands() - doc_commands())}, which the README does not show")


@pytest.mark.parametrize("endpoint", sorted(code_endpoints()))
def test_every_endpoint_is_named_in_the_files_table(endpoint):
    table = re.search(r"\| `app/main\.py` \|([^|]+)\|", README)
    assert table, "the Files table has no row for app/main.py"
    assert f"`{endpoint}`" in table.group(1), (
        f"{endpoint} is served but the Files table does not mention it: {table.group(1).strip()}")


@pytest.mark.parametrize("var", sorted(code_env()))
def test_every_environment_variable_is_documented(var):
    assert f"`{var}`" in README, f"main.py reads {var} and the README never says so"


def test_the_readme_invents_no_environment_variable():
    documented = set(re.findall(r"^\| `([A-Z_]+)` \|", README, re.M))
    assert documented <= code_env(), (
        f"the README documents {sorted(documented - code_env())}, which nothing reads")


@pytest.mark.parametrize("frame", sorted(code_frames()))
def test_every_frame_the_socket_sends_is_described(frame):
    assert f"`{frame}`" in README, f"the poller broadcasts a {frame!r} frame the README does not mention"


def test_the_doc_does_not_claim_the_layout_re_runs():
    """#12's headline. `updateGraph` patches data and never lays out again —
    that is what keeps a node where a reader dragged it."""
    js = (APP / "static" / "dashboard.js").read_text()
    update = re.search(r"function updateGraph\(\)\s*\{(.+?)\n\}", js, re.S)
    assert update and "layout(" not in update.group(1), (
        "updateGraph runs a layout now — the README says it does not")
    assert "reruns on every state diff" not in README


def test_nothing_still_tells_a_reader_to_mount_the_docker_socket():
    """The socket is gone from every path — compose, containerlab and the two
    `docker run` examples. A doc that still mounts it would hand a reader the
    whole host for a dashboard that no longer uses it."""
    root = Path(__file__).resolve().parents[2]
    offenders = []
    for name in ("README.md", "dashboard/README.md", "simple.clab.yml",
                 "compose/docker-compose.yml", "router-agent/README.md"):
        for n, line in enumerate((root / name).read_text().splitlines(), 1):
            if "docker.sock" not in line:
                continue
            # Saying what was REMOVED is the point of the change; mounting it
            # is not. A line that binds it has a colon-separated path pair.
            if re.search(r"/var/run/docker\.sock:/var/run/docker\.sock", line):
                offenders.append(f"{name}:{n}: {line.strip()[:70]}")
    assert not offenders, offenders


def test_the_poller_cannot_reach_docker_at_all():
    """Not a comment about not using it — the import and the dependency are
    gone, so there is nothing to reach for."""
    root = Path(__file__).resolve().parents[2]
    assert "import docker" not in POLLER
    assert "docker" not in (root / "dashboard" / "requirements.txt").read_text()
