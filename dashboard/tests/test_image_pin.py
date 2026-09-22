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


WORKFLOW = (ROOT / ".github" / "workflows" / "lab-ci.yml").read_text()
DOCKERFILE = (ROOT / "dashboard" / "Dockerfile").read_text()


def test_every_image_ci_builds_is_labelled_with_its_commit():
    """A Dockerfile cannot know the commit it is built from, so CI has to say.
    Without it nothing on a running host can answer "which build is this?" —
    measured on the published image before this: `.Config.Labels` was `null`,
    and identifying a container meant diffing its served files against a
    checkout byte for byte.

    Both buildx invocations, not one: the cache pass and the loaded pass have
    to produce the same image, and a label is part of the image.
    """
    builds = re.findall(r"docker buildx build(.*?)dashboard/", WORKFLOW, re.S)
    assert len(builds) >= 3, f"expected the cache, load and push builds; found {len(builds)}"
    for i, build in enumerate(builds):
        assert "REVISION=" in build or "LABELS[@]" in build, (
            f"buildx invocation {i} is built without a revision")


def test_the_revision_reaches_both_the_label_and_the_running_app():
    """One build argument, two destinations. Two independent mechanisms for the
    same fact would be two things to keep in step, and the first time they
    disagreed the page would be confidently wrong about which build it is."""
    live = "\n".join(line for line in DOCKERFILE.splitlines()
                     if line.strip() and not line.lstrip().startswith("#"))
    assert re.search(r"^ARG REVISION=", live, re.M), "the Dockerfile takes no REVISION argument"
    assert re.search(r'org\.opencontainers\.image\.revision="?\$REVISION', live), (
        "the OCI label is not built from the argument")
    assert re.search(r"DASHBOARD_REVISION=\$REVISION", live), (
        "the app is not given the argument to serve on /api/version")


def test_the_image_says_what_it_is():
    """Read only the lines that BUILD something. A comment mentioning a label
    is not a label — measured: commenting the LABEL line out left every one of
    these strings in the file and this test passed."""
    live = "\n".join(line for line in DOCKERFILE.splitlines()
                     if line.strip() and not line.lstrip().startswith("#"))
    assert re.search(r"^LABEL\b", live, re.M), "the Dockerfile sets no labels at all"
    for label in ("org.opencontainers.image.title",
                  "org.opencontainers.image.description",
                  "org.opencontainers.image.source"):
        assert label in live, f"the Dockerfile does not set {label}"
