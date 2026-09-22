"""The page's decisions, run rather than read.

test_dashboard_js.py checks what the SOURCE may contain — escaping, the
markup. These are the decisions it makes at runtime: which of a session's two
states colours the edge, what becomes of an edge whose session is gone, and
whether an event delivered twice is drawn twice. A static read cannot check
any of them.

The cases live in tests/js/harness.js, which loads the shipped dashboard.js in
a vm with the smallest stubs the page needs and prints one result per case. Node is what runs the page's language; CI already uses it
for the Playwright walk.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "js" / "harness.js"
NODE = shutil.which("node")


def run_harness() -> list[dict]:
    proc = subprocess.run([NODE, str(HARNESS)], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f"harness failed to run: {proc.stderr.strip()}")
    return json.loads(proc.stdout)["cases"]


CASES = run_harness() if NODE else []


@pytest.mark.skipif(not NODE, reason="node is not installed; the page's logic cannot be run")
def test_the_harness_ran_every_case():
    """A harness that silently stopped loading the file would otherwise report
    nothing and pass."""
    assert len(CASES) >= 26, f"only {len(CASES)} cases ran — did dashboard.js stop loading?"


@pytest.mark.skipif(not NODE, reason="node is not installed; the page's logic cannot be run")
@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES] or None)
def test_page_logic(case):
    assert case["ok"], case["detail"]
