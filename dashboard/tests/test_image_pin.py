"""The published image is named in two places; they have to agree.

`simple.clab.yml` is what a reader deploys and README.md is what tells them
what they are deploying. Bumping one and forgetting the other is the whole
failure mode: the lab would run one build while the page said another, and
nothing else in this repo reads either line.
"""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PINNED = re.compile(r"quay\.io/ephico2real/bgp-dashboard:(sha-[0-9a-f]{7})")


def pins(path: Path) -> set[str]:
    return set(PINNED.findall(path.read_text()))


def test_the_topology_pins_this_fork_s_own_build():
    topology = yaml.safe_load((ROOT / "simple.clab.yml").read_text())
    image = topology["topology"]["nodes"]["dashboard"]["image"]
    assert PINNED.fullmatch(image), (
        f"the dashboard node deploys {image!r} — upstream's image predates this "
        "fork's fixes, so the lab would show none of them")


@pytest.mark.parametrize("name", ["README.md", "simple.clab.yml"])
def test_every_file_that_names_the_image_names_the_same_one(name):
    everywhere = pins(ROOT / "README.md") | pins(ROOT / "simple.clab.yml")
    assert len(everywhere) == 1, f"the pin has drifted: {sorted(everywhere)}"
    assert pins(ROOT / name) == everywhere


def test_the_digest_recorded_beside_the_pin_is_a_digest():
    """The comment carries the immutable reference for the tag above it."""
    text = (ROOT / "simple.clab.yml").read_text()
    assert re.search(r"# Digest sha256:[0-9a-f]{64}", text), (
        "the pin has no digest recorded beside it")
