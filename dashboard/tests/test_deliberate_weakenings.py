"""Two defaults this lab turns off on purpose, and the promise that it says so.

A weakened default that nobody wrote down is indistinguishable from one nobody
noticed. These do not argue about whether the choices are right — they are
right for a teaching lab — only that each one is stated where a reader will
meet it, and that the safe default is still the default.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text()
COMPOSE = (ROOT / "compose" / "docker-compose.yml").read_text()
CLAB = (ROOT / "simple.clab.yml").read_text()
CONFIGS = sorted((ROOT / "configs").glob("*/frr.conf"))


@pytest.mark.parametrize("conf", CONFIGS, ids=[c.parent.name for c in CONFIGS])
def test_every_router_that_disables_rfc_8212_says_why(conf):
    """`no bgp ebgp-requires-policy` turns off the rule that an eBGP session
    advertises nothing until a policy exists. Someone copying these configs
    toward production inherits the override; the file has to tell them."""
    text = conf.read_text()
    if "no bgp ebgp-requires-policy" not in text:
        pytest.skip("this router does not disable it")
    before = text.split("no bgp ebgp-requires-policy")[0]
    comment = before.rsplit("!", 1)[-1] if "!" in before else ""
    assert "RFC 8212" in before, f"{conf.parent.name} disables it with no reference to the rule"
    assert "production" in before.lower() or "outside a lab" in before.lower(), (
        f"{conf.parent.name} does not say the override is wrong outside a lab: {comment[:80]!r}")


def test_the_readme_names_both_weakenings():
    section = re.search(r"## What this lab deliberately weakens(.+?)\n## ", README, re.S)
    assert section, "the README has no section naming what it turns off"
    body = section.group(1)
    assert "RFC 8212" in body
    assert "no bgp ebgp-requires-policy" in body
    assert "loopback" in body.lower()


def test_the_dashboard_is_published_on_the_loopback_by_default():
    """Both paths, because a reader follows one or the other. The page has no
    authentication and its container holds the Docker socket."""
    clab = re.search(r"^\s*- (\S*8088:8080)\s*$", CLAB, re.M)
    assert clab and clab.group(1).startswith("127.0.0.1:"), (
        f"simple.clab.yml publishes {clab.group(1) if clab else '?'} — not loopback-only")

    compose = re.search(r'-\s*"(\S+:8080)"', COMPOSE)
    assert compose, "the compose file publishes nothing"
    assert compose.group(1).startswith("${DASHBOARD_BIND:-127.0.0.1}"), (
        f"the compose default is {compose.group(1)}")


def test_reaching_it_from_elsewhere_is_a_deliberate_step_and_is_documented():
    assert "DASHBOARD_BIND" in COMPOSE, "there is no way out of the loopback but editing the file"
    assert "DASHBOARD_BIND" in README, "the escape hatch exists and is not documented"
