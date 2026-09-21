"""Static guards on the browser code.

There is no JS test runner in this repo, and adding one to assert three things
would cost more than it is worth. These read the file instead — which is enough
for the properties that matter, because both are about what the SOURCE may
contain rather than about what it computes at runtime.
"""
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
JS = STATIC / "dashboard.js"
HTML = STATIC / "index.html"



def wrapped_in_esc(e: str) -> bool:
    """True when the WHOLE expression is one esc(...) call.

    `e.startswith("esc(") and e.endswith(")")` is not enough: it also accepts
    `esc(a) + b`, where b is unescaped.
    """
    if not e.startswith("esc("):
        return False
    depth = 0
    for i, c in enumerate(e):
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i == len(e) - 1
    return False


def has_top_level_ternary(e: str) -> bool:
    depth, quote = 0, ""
    for i, c in enumerate(e):
        if quote:
            if c == quote:
                quote = ""
            continue
        if c in "\"'":
            quote = c
        elif c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        elif c == "?" and depth == 0 and e[i:i + 2] != "??":
            return True
    return False


def split_top_level(e: str) -> list[str]:
    """Split on ?, :, || and && that choose between values, at depth 0 only.

    Depth matters: `esc(x ?? "?")` contains a `?` that belongs to the call, and
    splitting there produced the fragment `esc(r.s.state ?` — which is neither
    a literal nor a complete call, so every escaped field failed. `??` is a
    default, not a choice between two rendered values, so it is not a split.
    """
    out, buf, depth, i = [], "", 0, 0
    quote = ""
    while i < len(e):
        c = e[i]
        if quote:
            buf += c
            if c == quote:
                quote = ""
            i += 1
            continue
        if c in "\"'":
            quote = c
            buf += c
            i += 1
            continue
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        if depth == 0:
            if e[i:i + 2] in ("||", "&&"):
                out.append(buf.strip()); buf = ""; i += 2; continue
            if e[i:i + 2] == "??":       # a default, not a choice
                buf += "??"; i += 2; continue
            if c in "?:":
                out.append(buf.strip()); buf = ""; i += 1; continue
        buf += c
        i += 1
    out.append(buf.strip())
    return [o for o in out if o]


def markup_templates(src: str) -> list[str]:
    """Every template literal in the file that builds MARKUP.

    Not just the ones assigned to innerHTML. The peer rows and the route rows
    are built in their own literals and interpolated into the innerHTML
    template later, so a version of this that matched `innerHTML = \`` only
    was blind to exactly the cells that render a peer's data — measured: a
    mutant that dropped esc() from the community cell passed the whole suite.
    A literal counts as markup when it contains an HTML tag.
    """
    out, out_spans = [], []
    for m in re.finditer(r"`", src):
        # skip the closing backtick of a literal already captured
        if any(m.start() >= s and m.start() <= e for s, e in out_spans):
            continue
        i = m.end()
        depth = 0
        while i < len(src):
            c = src[i]
            if c == "\\":
                i += 2
                continue
            if c == "$" and src[i + 1 : i + 2] == "{":
                depth += 1
                i += 2
                continue
            if c == "}" and depth:
                depth -= 1
            elif c == "`" and not depth:
                body = src[m.end() : i]
                out_spans.append((m.start(), i))
                if re.search(r"<[a-zA-Z/]", body):
                    out.append(body)
                break
            i += 1
    return out


def test_every_innerhtml_interpolation_is_escaped():
    """The page renders what FRR reported, which is shaped by what a peer
    advertised. Whether any of today's fields can carry a '<' is a property of
    FRR's formatting, not a promise this page makes."""
    src = JS.read_text()
    templates = markup_templates(src)
    assert len(templates) >= 3, (
        "expected the innerHTML template plus the peer-row and route-row "
        f"literals; found {len(templates)} — did the file change shape?")

    # Named exemptions, each one safe for a stated reason. The list is explicit
    # rather than a pattern, so a new unescaped interpolation has to be argued
    # for here instead of slipping past a loose rule.
    allowed = {
        # a count the page computes itself, never input
        "Object.keys(peers).length",
        # markup the page has ALREADY built out of escaped values: escaping it
        # again would print the tags instead of rendering the rows
        """peerRows || '<tr><td colspan=4>none</td></tr>'""",
        """routeRows.join("") || '<tr><td colspan=6>empty</td></tr>'""",
        # a class name chosen between two literals the page owns
        """isBest ? 'best' : ''""",
        # markup the traffic rows built out of already-escaped values
        "msgs",
        "pfx",
        """body || '<tr><td colspan=8>no sessions</td></tr>'""",
    }

    def is_safe(e: str) -> bool:
        """An expression is safe when every value it can produce is safe.

        A rule rather than a list: split on the operators that choose between
        values (?, :, ||, &&) and require each operand to be a source literal
        or an esc(...) call. That covers `x ? "a" : "b"` and
        `cond ? esc(v) : "-"` without naming either, and still rejects a bare
        field. Only a variable holding markup the page built needs an entry in
        `allowed`, because its safety is a fact about how it was made.
        """
        if e in allowed:
            return True
        parts = split_top_level(e)
        # In `cond ? a : b` only a and b are rendered — the condition is never
        # put on the page, so requiring it to be escaped rejected every
        # perfectly safe `x > 0 ? "moving" : "idle"`.
        if has_top_level_ternary(e) and len(parts) > 1:
            parts = parts[1:]
        for o in parts:
            if not o:
                continue
            if re.fullmatch(r"""['"][^'"]*['"]""", o):
                continue
            if wrapped_in_esc(o):
                continue
            return False
        return True

    unescaped = [e for tpl in templates
                 for e in (x.strip() for x in
                           re.findall(r"\$\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", tpl))
                 if not is_safe(e)]
    assert not unescaped, (
        "these go into innerHTML without esc(): " + "; ".join(unescaped)
    )


def test_esc_covers_every_character_that_changes_markup():
    """Read the table out of the source rather than trusting the name."""
    src = JS.read_text()
    m = re.search(r"function esc\(v\)\s*\{(.+?)\n\}", src, re.S)
    assert m, "esc() is gone"
    body = m.group(1)
    for ch, ent in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"),
                    ('"', "&quot;"), ("'", "&#39;")):
        assert ent in body, f"esc() does not map {ch!r} to {ent}"
    # & must be replaced too, or every other entity is double-broken
    assert body.index("&amp;") < body.index("&lt;"), "& must be escaped first"


def test_no_script_is_fetched_from_the_internet_at_page_load():
    """A lab demonstrating BGP should not need the public internet to draw its
    own topology, and a CDN script is a third party inside every run."""
    html = HTML.read_text()
    remote = re.findall(r'<script[^>]+src="(https?://[^"]+)"', html)
    assert not remote, f"remote script(s) still loaded: {remote}"
    # The path has to be the one the app SERVES. index.html is returned from
    # "/" while the files are mounted at "/static", so a relative
    # `vendor/cytoscape.min.js` resolves to /vendor/... and 404s — the page
    # loaded, drew nothing, and said "cytoscape is not defined" (measured).
    assert 'src="/static/vendor/cytoscape.min.js"' in html, (
        "the vendored copy is not referenced at the path the app serves")


def test_the_vendored_library_is_present_and_is_cytoscape():
    lib = STATIC / "vendor" / "cytoscape.min.js"
    assert lib.is_file(), "vendor/cytoscape.min.js is missing"
    head = lib.read_text(errors="replace")[:4000]
    assert "cytoscape" in head.lower()
    assert lib.stat().st_size > 100_000, "the vendored file looks truncated"


@pytest.mark.parametrize("path,needle", [
    ("simple.clab.yml", "127.0.0.1:8088:8080"),
    ("compose/docker-compose.yml", "127.0.0.1:${DASHBOARD_PORT:-8089}:8080"),
])
def test_the_dashboard_is_published_on_loopback_only(path, needle):
    """It has no authentication and holds the Docker socket. Publishing it on
    every interface put it on the network the moment the lab came up."""
    root = Path(__file__).resolve().parents[2]
    assert needle in (root / path).read_text(), f"{path} does not publish on loopback"


def test_a_qualified_state_is_not_dropped_into_the_unknown_colour():
    """FRR qualifies a state with a reason: an administrative shutdown reads
    "Idle (Admin)". An exact key lookup misses it, so the one session a human
    had just taken down was the one the graph refused to draw as down."""
    src = JS.read_text()
    assert "function stateColor(" in src, "stateColor() is gone"
    # the edge style must go through it, not index the table directly
    assert not re.search(r'STATE_COLORS\[e\.data\("state"\)\]', src), (
        "the edge style still indexes STATE_COLORS by the exact state string")
    assert 'stateColor(e.data("state"))' in src, "the edge style does not use stateColor()"
    m = re.search(r"function stateColor\(state\)\s*\{(.+?)\n\}", src, re.S)
    assert m and "startsWith" in m.group(1), "stateColor() does not match on a prefix"
