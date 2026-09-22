"""The legend must describe the page it is on.

A key is only worth having if it cannot drift from what the graph draws. Two
ways it could: by carrying its own colour values, and by naming a state the
page has no colour for — which `stateColor` answers with the grey it reserves
for "unknown", so the legend would quietly show grey beside the word
"Established" and nothing would fail.
"""
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
HTML = (STATIC / "index.html").read_text()
JS = (STATIC / "dashboard.js").read_text()

LEGEND = re.search(r'<ul id="legend".*?</ul>', HTML, re.S)


def state_colour_keys() -> list[str]:
    block = re.search(r"const STATE_COLORS = \{(.+?)\n\};", JS, re.S)
    assert block, "STATE_COLORS is not in dashboard.js any more"
    return re.findall(r"^\s*([A-Za-z]+):", block.group(1), re.M)


def role_colour_keys() -> list[str]:
    block = re.search(r"const ROLE_COLORS = \{(.+?)\n\};", JS, re.S)
    assert block, "ROLE_COLORS is not in dashboard.js any more"
    return re.findall(r"^\s*([A-Za-z]+):", block.group(1), re.M)


def test_the_legend_is_there_at_all():
    assert LEGEND, "no #legend list in index.html"
    assert LEGEND.group(0).count("<li") >= 6, "the legend lost most of its entries"


def test_the_legend_carries_no_colour_of_its_own():
    """Every swatch is painted at runtime from the same function the graph uses.
    A hex value here would be a second source of truth for the same fact."""
    colours = re.findall(r"#[0-9a-fA-F]{3,8}\b", LEGEND.group(0))
    assert not colours, f"the legend hard-codes {colours}; paint it from stateColor()/ROLE_COLORS"


ROWS = re.findall(r"<li>(.*?)</li>", LEGEND.group(0) if LEGEND else "", re.S)
STATE_ROWS = [(re.search(r'data-state="([^"]+)"', r).group(1), r) for r in ROWS if "data-state=" in r]


@pytest.mark.parametrize("state,row", STATE_ROWS, ids=[s for s, _ in STATE_ROWS])
def test_every_state_the_legend_names_is_one_the_page_can_colour(state, row):
    """stateColor matches on a PREFIX and falls back to the grey it reserves for
    unknown. A legend entry naming something that is not a prefix of a real key
    would render in that grey beside its own description of a colour."""
    keys = [k for k in state_colour_keys() if k != "unknown"]
    assert any(state.startswith(k) for k in keys), (
        f"the legend names {state!r}, which stateColor() paints in the unknown grey; "
        f"known: {keys}")


@pytest.mark.parametrize("state,row", STATE_ROWS, ids=[s for s, _ in STATE_ROWS])
def test_the_swatch_is_painted_by_the_state_its_row_talks_about(state, row):
    """Colourable is not the same as correct. `data-state="Connected"` prefix-
    matches `Connect`, so a row describing Established would be painted the
    amber of a session coming up and every other check would pass — measured:
    that mutant survived the whole suite. The state a row is painted by has to
    be a word the row itself says."""
    words = re.sub(r"<[^>]+>", " ", row)
    assert re.search(rf"\b{re.escape(state)}\b", words), (
        f"the swatch is painted by {state!r} but the row never says it: {words.strip()[:90]!r}")


@pytest.mark.parametrize("role", re.findall(r'data-role="([^"]+)"', LEGEND.group(0) if LEGEND else ""))
def test_every_role_the_legend_names_is_one_the_page_draws(role):
    assert role in role_colour_keys(), f"ROLE_COLORS has no {role!r}"


def test_the_legend_says_the_role_is_a_guess():
    """`nodeRole` is a regex on the node's NAME — it is not read from BGP, and a
    key that presented it as fact would be claiming knowledge the page does not
    have. The comment in dashboard.js says 'crude heuristic'; the page has to
    say so too, where a reader can see it."""
    assert re.search(r"/isp/i", LEGEND.group(0)), "the legend does not name the pattern it classifies by"
    assert re.search(r"not read from BGP", LEGEND.group(0)), (
        "the legend does not tell the reader the role is guessed from the name")


def test_the_page_paints_the_legend_from_its_own_colours():
    assert "function paintLegend()" in JS
    assert "stateColor(el.dataset.state)" in JS, "the swatches are not painted from stateColor()"
    assert "ROLE_COLORS[el.dataset.role]" in JS, "the role swatches are not painted from ROLE_COLORS"


def test_the_graph_asks_for_a_selection_that_can_be_moved_together():
    """Cytoscape moves every selected node when one is grabbed — but only if a
    reader can select more than one, which `additive` is what allows."""
    assert "boxSelectionEnabled: true" in JS
    assert 'selectionType: "additive"' in JS


CSS = (STATIC / "styles.css").read_text()


def test_the_legend_can_actually_be_hidden():
    """`display: grid` on a class beats `display: none` from the `hidden`
    attribute, so a legend styled this way is open for ever and the toggle's
    aria-expanded describes nothing. Measured before the rule below existed:
    #legend was 804 px wide with `hidden` set."""
    assert re.search(r"\.legend\[hidden\]\s*\{[^}]*display:\s*none", CSS), (
        "styles.css sets display on .legend without a [hidden] rule to beat it")


def test_the_panes_that_hold_the_graph_have_a_floor():
    """A grid item — and an implicit grid TRACK — sizes to its content unless
    told otherwise. The legend's longest line held #graph at 820 px inside a
    375 px pane, so the canvas sized itself to 820 and three of the four
    routers were drawn off-screen."""
    pane = re.search(r"#graph-pane\s*\{(.+?)\n\}", CSS, re.S)
    assert pane, "#graph-pane is not in styles.css"
    assert "min-width: 0" in pane.group(1), "#graph-pane may not shrink below its content"
    assert "minmax(0, 1fr)" in pane.group(1), "#graph-pane's column sizes to max-content"
