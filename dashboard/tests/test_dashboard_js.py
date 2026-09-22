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
        elif c in "([{":
            depth += 1
        elif c in ")]}":
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
        if c in "([{":
            depth += 1
        elif c in ")]}":
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


IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


def interpolations(tpl: str) -> list[str]:
    r"""Every `${...}` in a template body, brace-balanced.

    The regex this replaces — `\$\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}` — handled
    ONE level of nested braces and skipped an interpolation with two: measured,
    `${fmt(r, {opts: {raw: true}})}` matched nothing at all, so the expression
    was never offered to is_safe and the unescaped field it rendered went into
    innerHTML with the whole suite green. An interpolation this cannot read has
    to end up in the list and be rejected, never be invisible.
    """
    out, i = [], 0
    while i < len(tpl) - 1:
        if tpl[i] == "\\":
            i += 2
            continue
        if not (tpl[i] == "$" and tpl[i + 1] == "{"):
            i += 1
            continue
        depth, j = 1, i + 2
        while j < len(tpl) and depth:
            c = tpl[j]
            if c == "\\":
                j += 2
                continue
            if c in "\"'":
                j += 1
                while j < len(tpl) and tpl[j] != c:
                    j += 2 if tpl[j] == "\\" else 1
                j += 1
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            j += 1
        out.append(tpl[i + 2:j - 1])
        i = j
    return out


ASSIGN = r"(?<![=!<>+\-*/%&|^])\b{name}\s*=(?![=>])"


def bound_elsewhere(src: str, name: str) -> bool:
    """True when `name` is more than the one declaration this file resolves.

    sole_assignment searches the WHOLE file and knows nothing of scope, and a
    declaration is not the only way a name gets a value. Both of these were
    measured against this guard, and both passed it:

      * `let stamp = esc(clock.text); if (ev.raw) stamp = ev.ts;` — declared
        once, safely, and rendered as the other value.
      * a second function whose PARAMETER is called `who`, resolving to the
        `const who` of an unrelated one and rendering raw text.

    So a name is followed only when it is bound exactly once, in a declaration,
    and never rebound.
    """
    decls = re.findall(rf"\b(?:const|let|var)\s+{re.escape(name)}\s*=\s*", src)
    if len(re.findall(ASSIGN.format(name=re.escape(name)), src)) != len(decls):
        return True
    n = re.escape(name)
    for pattern in (
        rf"function\s*[A-Za-z0-9_$]*\s*\([^)]*\b{n}\b[^)]*\)",                # a parameter
        rf"\([^()]*\b{n}\b[^()]*\)\s*=>",                                     # an arrow parameter
        rf"\b{n}\s*=>",                                                       # a bare arrow parameter
        rf"for\s*\(\s*(?:const|let|var)\s+{n}\b",                             # a loop binding
        rf"\bcatch\s*\(\s*{n}\s*\)",                                          # a caught error
        rf"(?:const|let|var)\s*[\[{{][^\]}}]*\b{n}\b[^\]}}]*[\]}}]\s*=",      # destructuring
    ):
        if re.search(pattern, src):
            return True
    return False


def read_expression(src: str, i: int) -> str:
    """The expression starting at i, up to the `;` or line end that closes it.

    Depth-aware over (), [], {} and template literals, because the values this
    resolves are template literals that span lines and contain both braces and
    semicolons inside their `${...}`.
    """
    start, depth, quote, tick = i, 0, "", 0
    while i < len(src):
        c = src[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = ""
            i += 1
            continue
        if tick:
            if c == "\\":
                i += 2
                continue
            if c == "$" and src[i + 1:i + 2] == "{":
                depth += 1
                i += 2
                continue
            if c == "}" and depth:
                depth -= 1
            elif c == "`":
                tick -= 1
            i += 1
            continue
        if c in "\"'":
            quote = c
        elif c == "`":
            tick += 1
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and (c == ";" or c == "\n"):
            break
        i += 1
    return src[start:i].strip()


def sole_assignment(src: str, name: str) -> str | None:
    """The one `const/let/var <name> = <expr>` in the file, or None.

    None when the name is declared more than once: which value reaches the page
    then depends on which one ran, and reading one of them proves nothing.
    """
    found = [read_expression(src, m.end())
             for m in re.finditer(rf"\b(?:const|let|var)\s+{re.escape(name)}\s*=\s*", src)]
    return found[0] if len(found) == 1 else None


def markup_templates(src: str) -> list[str]:
    r"""Every template literal in the file that builds MARKUP.

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
        # markup the traffic rows built out of already-escaped values. These
        # two stay named rather than resolved: their definitions are ternaries
        # over template literals, which split_top_level would cut at the `?`
        # inside a `${}`. Both literals carry a <span>, so markup_templates
        # collects them in their own right and their interpolations are
        # checked there — the name is exempt, the definition is not.
        "msgs",
        "pfx",
        """body || '<tr><td colspan=8>no sessions</td></tr>'""",
    }

    def resolvable(name: str) -> bool:
        return not bound_elsewhere(src, name)

    def is_safe(e: str, seen: frozenset = frozenset()) -> bool:
        """An expression is safe when every value it can produce is safe.

        A rule rather than a list: split on the operators that choose between
        values (?, :, ||, &&) and require each operand to be a source literal,
        an esc(...) call, or a NAME the file assigns exactly once to something
        that is itself safe. That covers `x ? "a" : "b"`, `cond ? esc(v) : "-"`
        and a line built up in a local, without naming any of them, and still
        rejects a bare field.

        Resolving the name matters more than exempting it: a `const who =
        `${esc(ev.node)}…`` contains no tag, so it is not collected as markup
        on its own, and an entry in `allowed` would make an esc() dropped
        INSIDE it invisible to this test. Following the assignment checks the
        definition instead of trusting it.
        """
        if e in allowed:
            # An exempted NAME is still only as good as its single binding: the
            # exemption says its definition is markup this page built, not that
            # anything may be assigned to it later.
            return not IDENT.fullmatch(e) or resolvable(e)
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
            if IDENT.fullmatch(o) and o not in seen and resolvable(o):
                rhs = sole_assignment(src, o)
                if rhs is not None and value_is_safe(rhs, seen | {o}):
                    continue
            return False
        return True

    def value_is_safe(expr: str, seen: frozenset) -> bool:
        """A resolved definition: a template literal, or any other expression."""
        if expr.startswith("`") and expr.endswith("`"):
            return all(is_safe(m.strip(), seen)
                       for m in interpolations(expr))
        return is_safe(expr, seen)

    unescaped = [e for tpl in templates
                 for e in (x.strip() for x in interpolations(tpl))
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
