// BGP Lab Dashboard frontend.
// Connects to /ws, builds a Cytoscape topology graph from BGP session data,
// updates colors on state changes, and renders details in the sidebar on click.

const statusEl = document.getElementById("status");
const sidebarTitle = document.getElementById("sidebar-title");
const sidebarContent = document.getElementById("sidebar-content");
const eventsEl = document.getElementById("events");

let cy = null;
let selectedNode = null;
let lastState = {};
let nodes = [];

const STATE_COLORS = {
  Established: "#1a7f37",
  Active: "#9a6700",
  Connect: "#9a6700",
  OpenSent: "#9a6700",
  OpenConfirm: "#9a6700",
  Idle: "#cf222e",
  // Not an FRR state: the session is no longer in either router's table. It
  // is drawn red rather than grey because "the peer is gone" is a failure, not
  // an absence of information.
  vanished: "#cf222e",
  unknown: "#8b8b8b",
};

// How long a vanished session's edge stays on the graph before it is removed.
// Long enough for a human who looked away to see WHAT went missing — an edge
// that disappears the instant it fails leaves nothing to read.
const VANISHED_GRACE_MS = 30000;

// How bad each state is. Anything FRR adds later ranks worst, so an unknown
// state is never quietly treated as healthy.
const STATE_RANK = { Established: 0, OpenConfirm: 1, OpenSent: 1, Connect: 1, Active: 2, Idle: 3 };

function stateRank(state) {
  const s = String(state || "").trim();
  for (const key of Object.keys(STATE_RANK)) {
    if (s.startsWith(key)) return STATE_RANK[key];
  }
  return 4;
}

function worseState(a, b) {
  if (!a) return b;
  if (!b) return a;
  return stateRank(b) > stateRank(a) ? b : a;
}

// stateColor matches on a PREFIX, not the whole string.
//
// FRR qualifies a state with a reason: an administrative shutdown reads
// "Idle (Admin)", which is not a key in the table above, so an exact lookup
// dropped it into the grey "unknown" colour — the one session a human had just
// taken down was the one the graph refused to draw as down.
function stateColor(state) {
  const s = String(state || "").trim();
  if (!s) return STATE_COLORS.unknown;
  for (const key of Object.keys(STATE_COLORS)) {
    if (key !== "unknown" && s.startsWith(key)) return STATE_COLORS[key];
  }
  return STATE_COLORS.unknown;
}

const ROLE_COLORS = {
  edge: { bg: "#d6f0d3", border: "#2c9d3c" },   // companies (heuristic)
  isp:  { bg: "#cfe6fd", border: "#2c79d9" },
  // A peer that answered but is not in the topology file: real, and not ours
  // to describe. Grey, because we know nothing about it beyond what it said.
  external: { bg: "#f3f4f6", border: "#8b8b8b" },
};

function setStatus(text, cls) {
  statusEl.textContent = text;
  statusEl.className = "status " + cls;
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  setStatus("connecting…", "status-connecting");

  ws.onopen = () => {
    setStatus("connected", "status-connected");
    catchUp();
  };
  ws.onclose = () => {
    setStatus("disconnected", "status-disconnected");
    setTimeout(connect, 2000);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (msg) => {
    const data = JSON.parse(msg.data);
    if (data.type === "snapshot") {
      nodes = data.nodes || [];
      lastState = data.data || {};
      buildGraph();
    } else if (data.type === "state") {
      lastState = data.data || {};
      updateGraph();
      if (selectedNode) renderDetail(selectedNode);
    } else if (data.type === "signal") {
      // Every tick, whether or not the topology changed. A heartbeat that only
      // beats when the graph changes is not a heartbeat.
      lastSignal = data.data || {};
      if (!document.getElementById("traffic").hidden) renderTraffic();
    } else if (data.type === "event") {
      addEvent(data.data);
    }
  };
}

function nodeRole(name) {
  // crude heuristic: anything starting with "isp" is transit
  return /isp/i.test(name) ? "isp" : "edge";
}

// What each router says about ITSELF, read from BGP rather than guessed.
// `show ip bgp summary json` carries the router-id and the local AS; the
// topology file and a regex over configs/ only ever said what the lab was
// WRITTEN to be, which is a different claim from what it is running.
function identities() {
  const out = new Map();                       // node name -> { routerId, asn }
  for (const n of nodes) {
    const ipv4 = ((lastState[n.name] || {}).summary || {}).ipv4Unicast || {};
    out.set(n.name, {
      routerId: ipv4.routerId || "",
      // the measured AS, with the config-derived one as the fallback that
      // labels the graph before the first poll answers
      asn: ipv4.as ?? n.asn ?? null,
    });
  }
  return out;
}

// Router-id -> node name. The router-id is the identity BGP itself uses, and
// unlike the AS it is unique per router: keying the graph by AS collapsed two
// routers in one AS into a single node, and made every peer whose AS was not
// in the topology file invisible.
function byRouterId(ids) {
  const out = new Map();
  for (const [name, id] of ids) if (id.routerId) out.set(id.routerId, name);
  return out;
}

function buildElements() {
  const els = [];
  const ids = identities();
  const routerIdToNode = byRouterId(ids);
  const external = new Map();                  // router-id (or address) -> node data

  // nodes
  for (const n of nodes) {
    const role = nodeRole(n.name);
    const id = ids.get(n.name) || {};
    els.push({
      data: {
        id: n.name,
        label: `${n.name}\nAS${id.asn ?? "?"}`,
        role,
        routerId: id.routerId || "",
      },
    });
  }

  // Build AS -> node lookup. This is the FALLBACK, for a router that answered
  // its summary but not `show bgp neighbors` (that call is non-fatal), and it
  // carries the ambiguity this whole change is about: it can only ever return
  // one node per AS.
  const asToNode = new Map();
  for (const [name, id] of ids) if (id.asn && !asToNode.has(id.asn)) asToNode.set(id.asn, name);

  // For each BGP session we walk both sides so we can label each end of the
  // edge with the IP that belongs to that side. When node N reports peer IP X,
  // X lives on the OTHER router (the remote side of the session).
  const edges = new Map();   // sorted-pair key -> { source, target, sourceIP, targetIP, state }

  for (const n of nodes) {
    const peers = ((lastState[n.name] || {}).summary || {}).ipv4Unicast?.peers || {};
    const neighbours = (lastState[n.name] || {}).neighbors || {};
    for (const [peerIp, info] of Object.entries(peers)) {
      // Who is on the other end, asked in the order of how much the answer is
      // worth: the peer's own router-id (BGP's identity for it), then the AS
      // (ambiguous), and if neither names a router we know, the peer is real
      // and simply outside this topology file — draw it rather than drop it.
      const nbr = neighbours[peerIp] || {};
      let remote = nbr.remoteRouterId ? routerIdToNode.get(nbr.remoteRouterId) : undefined;
      if (!remote) remote = asToNode.get(info.remoteAs);
      if (!remote) {
        const key = nbr.remoteRouterId || peerIp;
        remote = nbr.hostname || `AS${info.remoteAs ?? "?"} ${peerIp}`;
        if (!external.has(key)) {
          external.set(key, {
            id: remote,
            label: `${remote}\nAS${info.remoteAs ?? "?"}`,
            role: "external",
            routerId: nbr.remoteRouterId || "",
          });
        }
      }
      if (remote === n.name) continue;          // a router cannot peer with itself
      const [a, b] = [n.name, remote].sort();
      const key = `${a}--${b}`;
      let edge = edges.get(key);
      if (!edge) {
        edge = { id: key, source: a, target: b };
        edges.set(key, edge);
      }
      // peerIp belongs to the OTHER side of the session as seen from n.name,
      // and info.state is THIS node's view of it — so the state belongs to the
      // end peerIp is not on. Both are kept: the edge takes one colour, and a
      // reader looking at a red link needs to see which end called it down.
      if (remote === edge.source) {
        edge.sourceIP = peerIp;
        edge.targetState = info.state;
      } else {
        edge.targetIP = peerIp;
        edge.sourceState = info.state;
      }
      // The WORSE of the two ends, not whichever was iterated last.
      //
      // A session has two views and they disagree while it is coming up or
      // going down: one end can still read Established while the other has
      // already gone to Idle. Taking the last one seen made the edge's colour
      // depend on the order Object.entries happened to yield, so the same
      // fabric could draw a half-down link green or red on alternating polls.
      // The worse end is the honest one — a link is only up when both ends
      // agree it is.
      if (info.state) edge.state = worseState(edge.state, info.state);
    }
  }

  // A peer that is not in the topology file is still a peer. It used to be
  // dropped silently — `asToNode.get()` returned nothing and the loop moved
  // on — so a session the lab really had was missing from the picture with no
  // indication anything was hidden.
  for (const node of external.values()) els.push({ data: node });

  for (const e of edges.values()) {
    // When the two ends disagree, each end's label names its own state: the
    // edge is one colour and it cannot say by itself that leaf1 still reads
    // Established while spine has already gone to Idle.
    const split = e.sourceState && e.targetState && e.sourceState !== e.targetState;
    els.push({
      data: {
        id: e.id,
        source: e.source,
        target: e.target,
        state: e.state || "unknown",
        sourceState: e.sourceState || "",
        targetState: e.targetState || "",
        // On its own line, not in parentheses: these labels sit between two
        // nodes that the layout may place close together, and the width is
        // what collides. A second line costs 12 px of height and nothing of
        // width.
        sourceLabel: (e.sourceIP || "") + (split ? `\n${e.sourceState}` : ""),
        targetLabel: (e.targetIP || "") + (split ? `\n${e.targetState}` : ""),
      },
    });
  }

  return els;
}

// Which routers this poll actually READ. A router whose poll failed has no
// peers for the same reason a router with no sessions has none, and the page
// must not read the first as the second: "we could not reach isp1" is not
// "isp1's sessions are gone".
function reportedNodes() {
  const out = new Set();
  for (const n of nodes) {
    const d = lastState[n.name];
    if (d && !d.error && d.summary && typeof d.summary === "object") out.add(n.name);
  }
  return out;
}

function buildGraph() {
  if (cy) {
    // Already built; just patch in any updates.
    return updateGraph();
  }
  cy = cytoscape({
    container: document.getElementById("graph"),
    elements: buildElements(),
    style: [
      {
        selector: "node",
        style: {
          "label": "data(label)",
          "text-wrap": "wrap",
          "text-valign": "center",
          "text-halign": "center",
          "font-size": "11px",
          "font-weight": 600,
          "background-color": "#ffffff",
          "border-width": 2,
          "border-color": "#666",
          "width": 80,
          "height": 60,
          "shape": "round-rectangle",
          // Cytoscape draws nodes above edges regardless of z-index while a
          // node's comparison is `auto` — so a label that reached a node was
          // cut off by it. Measured against the vendored 3.30.4 with a long
          // label and two nodes 180 px apart: default and manual-on-the-EDGE
          // both rendered "0.10.2 (Idle (Admi"; manual on the NODE, with a
          // z-index below the edge's, rendered "10.0.10.2 (Idle (Admin))"
          // whole. The address is what the label is for.
          "z-index-compare": "manual",
          "z-index": 1,
        },
      },
      {
        selector: "node[role = 'edge']",
        style: {
          "background-color": ROLE_COLORS.edge.bg,
          "border-color": ROLE_COLORS.edge.border,
        },
      },
      {
        // Dashed, because the topology file does not know this node exists.
        selector: "node[role = 'external']",
        style: {
          "background-color": ROLE_COLORS.external.bg,
          "border-color": ROLE_COLORS.external.border,
          "border-style": "dashed",
          "shape": "ellipse",
        },
      },
      {
        selector: "node[role = 'isp']",
        style: {
          "background-color": ROLE_COLORS.isp.bg,
          "border-color": ROLE_COLORS.isp.border,
        },
      },
      {
        selector: "node:selected",
        style: { "border-width": 4, "border-color": "#0969da" },
      },
      {
        selector: "edge",
        style: {
          "width": 3,
          "line-color": (e) => stateColor(e.data("state")),
          "curve-style": "bezier",
          "source-label": "data(sourceLabel)",
          "target-label": "data(targetLabel)",
          "source-text-offset": 50,
          "target-text-offset": 50,
          "text-wrap": "wrap",
          "font-size": "9px",
          "font-family": "ui-monospace, SFMono-Regular, Menlo, monospace",
          "color": "#1f2328",
          "text-background-color": "#ffffff",
          "text-background-opacity": 0.95,
          "text-background-padding": 3,
          "text-background-shape": "round-rectangle",
          "text-border-color": "#d0d7de",
          "text-border-width": 1,
          "text-border-opacity": 1,
          "z-index": 10,
        },
      },
      {
        // A session that is no longer in either router's table. Dashed as well
        // as red so it is distinguishable from a peer that is merely down: one
        // is a session failing, the other is a session that no longer exists.
        selector: "edge[state = 'vanished']",
        style: { "line-style": "dashed", "opacity": 0.75 },
      },
    ],
    layout: { name: "cose", animate: false, padding: 30 },
    // Shift-drag draws a selection box (plain drag still pans), clicking adds
    // to the selection rather than replacing it, and Cytoscape moves every
    // selected node when one of them is grabbed. Without `additive` a reader
    // can only ever hold one node, which makes "move them together"
    // impossible to discover.
    boxSelectionEnabled: true,
    selectionType: "additive",
  });

  cy.on("tap", "node", (e) => {
    selectedNode = e.target.id();
    renderDetail(selectedNode);
  });
  cy.on("select unselect", "node", updateSelectionCount);
  paintLegend();
  updateSelectionCount();
}

// paintLegend colours each swatch from the SAME source the graph is drawn
// from: `data-state` through stateColor(), `data-role` through ROLE_COLORS. A
// legend that carried its own values would be a second source of truth, and
// the first time a colour changed the page and its key would disagree without
// anything failing.
function paintLegend() {
  const legend = document.getElementById("legend");
  if (!legend || !legend.querySelectorAll) return;
  for (const el of legend.querySelectorAll("[data-state]")) {
    const colour = stateColor(el.dataset.state);
    if (el.classList && el.classList.contains("swatch-dashed")) el.style.borderTopColor = colour;
    else el.style.background = colour;
  }
  for (const el of legend.querySelectorAll("[data-role]")) {
    const role = ROLE_COLORS[el.dataset.role];
    if (!role) continue;
    el.style.background = role.bg;
    el.style.border = `2px solid ${role.border}`;
  }
}

function updateSelectionCount() {
  const el = document.getElementById("sel-count");
  if (!el) return;
  const n = cy ? cy.$("node:selected").length : 0;
  el.textContent = n
    ? `${n} selected · drag one to move them together`
    : "shift-drag to select · drag a selected node to move them together";
}

// The controls exist so the selection is DISCOVERABLE: shift-drag and
// Ctrl/Cmd+A are not things a reader guesses at. Every one of them tolerates
// its element being absent, because the page is also loaded by tests that
// build only the parts they are exercising.
// Cytoscape sizes its canvas when it is created and never again: the pane
// changes size when the window does, and the graph then sits at its old
// dimensions with most of itself outside the viewport. Measured at 375 px
// after the columns stack — one node visible out of four. resize() re-reads
// the container, fit() brings the nodes back into view; neither moves a node,
// so an arrangement a reader made by hand survives.
let resizeTimer = null;

function handleResize() {
  if (!cy) return;
  if (resizeTimer) clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    resizeTimer = null;
    cy.resize();
    cy.fit(undefined, 30);
  }, 150);
}

let toolsWired = false;

function wireGraphTools() {
  // Idempotent. Every listener here TOGGLES or acts, so binding a second set
  // makes one click do the work twice — the legend button opened and closed
  // again in the same event, which is exactly how this was found.
  if (toolsWired) return;
  toolsWired = true;
  const on = (id, fn) => {
    const el = document.getElementById(id);
    if (el && el.addEventListener) el.addEventListener("click", fn);
  };
  on("select-all", () => { if (cy) cy.nodes().select(); updateSelectionCount(); });
  on("clear-sel", () => { if (cy) cy.nodes().unselect(); updateSelectionCount(); });
  on("reset-layout", () => { if (cy) cy.layout({ name: "cose", animate: false, padding: 30 }).run(); });
  on("legend-toggle", (e) => {
    const legend = document.getElementById("legend");
    if (!legend) return;
    const open = legend.hidden;
    legend.hidden = !open;
    const btn = e && e.currentTarget;
    if (btn && btn.setAttribute) btn.setAttribute("aria-expanded", String(open));
  });
  if (typeof window !== "undefined" && window.addEventListener) {
    window.addEventListener("resize", handleResize);
  }
  const graph = document.getElementById("graph");
  if (graph && graph.addEventListener) {
    // Scoped to the graph, so Ctrl/Cmd+A still selects text everywhere else.
    graph.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && (e.key === "a" || e.key === "A")) {
        e.preventDefault();
        if (cy) cy.nodes().select();
        updateSelectionCount();
      } else if (e.key === "Escape") {
        if (cy) cy.nodes().unselect();
        updateSelectionCount();
      }
    });
  }
}

// updateGraph is the only thing that sweeps a vanished edge away, and it runs
// on a `state` frame — which the poller sends ONLY when the signature changed.
// Measured against the running lab: 24 s of a steady fabric delivered 11
// `signal` frames and 0 `state` frames. So the poll that made a peer vanish is
// normally the last state frame for a long while, and an edge marked by it
// would sit dashed on the graph until something else on the fabric changed —
// for ever on a fabric that then stays quiet. The grace is a timer.
let vanishSweepArmed = false;

function scheduleVanishSweep() {
  if (vanishSweepArmed) return;
  vanishSweepArmed = true;
  setTimeout(() => {
    vanishSweepArmed = false;
    updateGraph();
  }, VANISHED_GRACE_MS + 250);
}

function updateGraph() {
  if (!cy) return buildGraph();

  // Incremental update only — never re-run layout after initial build, so the
  // user's manual node positions stick. We also intentionally don't remove
  // edges on transient absence (one side's peer data missing for a poll); the
  // edge stays and just changes color if the session goes away.
  const elements = buildElements();

  for (const el of elements) {
    const existing = cy.getElementById(el.data.id);
    if (existing.empty()) {
      cy.add(el);
    } else if (existing.isEdge()) {
      if (existing.data("state") !== el.data.state) {
        existing.data("state", el.data.state);
      }
      for (const k of ["sourceState", "targetState"]) {
        if (existing.data(k) !== el.data[k]) existing.data(k, el.data[k]);
      }
      if (el.data.sourceLabel && existing.data("sourceLabel") !== el.data.sourceLabel) {
        existing.data("sourceLabel", el.data.sourceLabel);
      }
      if (el.data.targetLabel && existing.data("targetLabel") !== el.data.targetLabel) {
        existing.data("targetLabel", el.data.targetLabel);
      }
      // It is back. Whatever it looks like now, it is no longer missing.
      if (existing.data("vanishedAt")) existing.data("vanishedAt", 0);
    }
  }

  // An edge NOT in the rebuilt set is a session neither router reports any
  // more. Keeping it painted with its last colour was the defect: a peer that
  // disappeared — a dynamic neighbour that left, `no neighbor`, a dead bgpd —
  // left a green line behind it, because with no peer data there is no new
  // colour to paint. Mark it, hold it long enough to be read, then drop it.
  const present = new Set(elements.map((el) => el.data.id));
  const reported = reportedNodes();
  const now = Date.now();
  let counting = false;
  for (const edge of cy.edges()) {
    if (present.has(edge.id())) continue;
    // Both ends have to have answered. Otherwise a poller that lost its
    // Docker socket — every node erroring at once — would quietly delete the
    // whole topology's edges while the page still said "connected".
    if (!reported.has(edge.data("source")) || !reported.has(edge.data("target"))) continue;
    const since = edge.data("vanishedAt");
    if (!since) {
      edge.data("vanishedAt", now);
      edge.data("state", "vanished");
      counting = true;
    } else if (now - since >= VANISHED_GRACE_MS) {
      edge.remove();
    } else {
      counting = true;
    }
  }
  // An edge is counting down and nothing promises another state frame.
  if (counting) scheduleVanishSweep();
}

// esc renders a value as TEXT wherever it is put into markup.
//
// Everything below is built with template literals and assigned to innerHTML,
// and none of it is the page's own data: it is whatever FRR reported, which is
// in turn shaped by what a peer advertised and by names taken from the
// topology file. Today's FRR output happens to be addresses, AS numbers and
// state words, so nothing here is known to be exploitable — but that is a
// property of FRR's formatting, not a promise this page makes, and the day a
// field carries a "<" the page stops being a viewer and starts being a
// renderer of someone else's markup. Escaping costs one function.
function esc(v) {
  if (v === null || v === undefined) return "";
  return String(v).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}


// ---- the signal -----------------------------------------------------------
//
// The poller measures what each session DID between two polls and broadcasts
// it every tick as a "signal" frame. The page ignored those frames entirely,
// so an Established line was all it could ever say: a session carrying updates
// and one that has been silent for a minute looked identical.
//
// Everything below is driven by a number the poller measured. `hasDelta` and
// `hasTimers` say when it did NOT measure one, and those cases render as
// "unmeasured" rather than as a zero that reads like silence.

let lastSignal = {};

// sessionHealth reads FRR's own clock, not our sampling.
//
// quietMsec is bgpTimerLastRead: how long since this peer last sent an UPDATE
// or a KEEPALIVE. Healthy, it sawtooths between 0 and the keepalive interval;
// past that a keepalive was missed, and at holdMsec FRR tears the session
// down. The thresholds are fractions of the NEGOTIATED timers, because this
// lab runs FRR's defaults (keepalive 60s, hold 180s) while other fabrics run
// 3 and 9 — a hard number would be wrong on one of them.
function sessionHealth(s) {
  if (!s || !s.hasTimers || !(s.holdMsec > 0)) return "unknown";
  const quiet = Math.max(0, s.quietMsec || 0);
  const keepalive = s.keepaliveMsec > 0 ? s.keepaliveMsec : s.holdMsec / 3;
  if (quiet >= s.holdMsec * (2 / 3)) return "critical";
  if (quiet > keepalive) return "late";
  return "ok";
}

// A withdrawal lowers pfxRcd while msgRcvd rises, so the prefix delta is
// SIGNED. The magnitude is what gets drawn and the sign is what it means.
function sessionTraffic(s) {
  if (!s || !s.hasDelta) return { known: false, messages: 0, prefixes: 0, withdrew: false };
  const dPfx = (s.dPfxRcd || 0) + (s.dPfxSnt || 0);
  return {
    known: true,
    messages: Math.max(0, s.dRcvd || 0) + Math.max(0, s.dSent || 0),
    prefixes: Math.abs(dPfx),
    withdrew: dPfx < 0,
  };
}

function fmtQuiet(ms) {
  if (ms == null) return "—";
  return ms >= 10000 ? Math.round(ms / 1000) + "s" : (ms / 1000).toFixed(1) + "s";
}

function renderTraffic() {
  const host = document.getElementById("traffic");
  const note = document.getElementById("activity-note");
  if (!host) return;
  const nodes = Object.keys(lastSignal).sort();
  if (!nodes.length) {
    host.innerHTML = '<p class="hint">waiting for the first poll</p>';
    if (note) note.textContent = "";
    return;
  }

  const rows = [];
  let measured = 0;
  let moving = 0;
  for (const node of nodes) {
    for (const peer of Object.keys(lastSignal[node]).sort()) {
      const s = lastSignal[node][peer];
      const traffic = sessionTraffic(s);
      const health = sessionHealth(s);
      if (traffic.known) measured += 1;
      if (traffic.known && traffic.messages > 0) moving += 1;
      rows.push({ node, peer, s, traffic, health });
    }
  }
  // Busiest first, with a stable tiebreak so equally quiet rows do not shuffle
  // between renders.
  rows.sort((a, b) => (b.traffic.messages - a.traffic.messages)
    || (a.node + a.peer < b.node + b.peer ? -1 : 1));

  if (note) {
    note.textContent = measured
      ? `${rows.length} sessions · ${moving} of ${measured} measured carried a message on the last poll`
      : `${rows.length} sessions · nothing measured on the last poll`;
  }

  const body = rows.map((r) => {
    const msgs = !r.traffic.known
      ? '<span class="idle">unmeasured</span>'
      : `<span class="${r.traffic.messages > 0 ? "moving" : "idle"}">${esc(r.traffic.messages)}</span>`;
    const pfx = r.traffic.known && r.traffic.prefixes
      ? `<span class="${r.traffic.withdrew ? "withdrew" : "moving"}">${r.traffic.withdrew ? "−" : "+"}${esc(r.traffic.prefixes)}</span>`
      : '<span class="idle">0</span>';
    const stateCls = r.s.state === "Established" ? "event-up" : "event-down";
    return `<tr>
      <td>${esc(r.node)}</td>
      <td>${esc(r.peer)}</td>
      <td class="${esc(stateCls)}">${esc(r.s.state ?? "?")}</td>
      <td class="num">${msgs}</td>
      <td class="num">${pfx}</td>
      <td class="num">${esc(r.s.pfxRcd ?? "?")} / ${esc(r.s.pfxSnt ?? "?")}</td>
      <td class="num health-${esc(r.health)}">${r.s.hasTimers ? esc(fmtQuiet(r.s.quietMsec)) : "—"}</td>
      <td class="num">${esc(r.s.flaps ?? 0)}</td>
    </tr>`;
  }).join("");

  host.innerHTML = `<table class="traffic-table">
    <thead><tr>
      <th>router</th><th>peer</th><th>state</th>
      <th class="num">msg</th><th class="num">pfx Δ</th>
      <th class="num">rcd / snt</th><th class="num">last heard</th><th class="num">flaps</th>
    </tr></thead>
    <tbody>${body || '<tr><td colspan=8>no sessions</td></tr>'}</tbody>
  </table>`;
}

function setActivityView(view) {
  const v = view === "traffic" ? "traffic" : "events";
  document.getElementById("events").hidden = v !== "events";
  document.getElementById("traffic").hidden = v !== "traffic";
  for (const id of ["view-events", "view-traffic"]) {
    const el = document.getElementById(id);
    if (el) el.setAttribute("aria-selected", el.dataset.view === v ? "true" : "false");
  }
  if (v === "traffic") renderTraffic();
  else if (document.getElementById("activity-note")) {
    document.getElementById("activity-note").textContent = "";
  }
}

function renderDetail(name) {
  const data = lastState[name] || {};
  const summary = data.summary || {};
  const ipv4 = summary.ipv4Unicast || {};
  const peers = ipv4.peers || {};
  const routes = (data.bgp || {}).routes || {};

  sidebarTitle.textContent = `${name}  AS${ipv4.as ?? "?"}`;

  const peerRows = Object.entries(peers).map(([ip, info]) => {
    const cls = info.state === "Established" ? "" : "event-down";
    return `<tr class="${esc(cls)}"><td>${esc(ip)}</td><td>AS${esc(info.remoteAs ?? "?")}</td>
            <td>${esc(info.state ?? "?")}</td><td>${esc(info.pfxRcd ?? "?")}</td></tr>`;
  }).join("");

  const routeRows = [];
  for (const [prefix, paths] of Object.entries(routes)) {
    if (!Array.isArray(paths)) continue;
    for (const p of paths) {
      const isBest = (p.bestpath && (p.bestpath.overall || p.bestpath === true));
      const nh = (p.nexthops?.[0]?.ip) ?? "?";
      const aspath = p.path ?? p.aspath?.string ?? "";
      const lp = p.locPrf ?? p.localpref ?? "";
      const med = p.metric ?? p.med ?? "";
      const community = (p.community?.string) ?? "";
      routeRows.push(`<tr class="${isBest ? 'best' : ''}">
        <td>${esc(prefix)}</td>
        <td>${esc(nh)}</td>
        <td class="aspath">${esc(aspath)}</td>
        <td>${esc(lp)}</td>
        <td>${esc(med)}</td>
        <td class="community">${esc(community)}</td>
      </tr>`);
    }
  }

  sidebarContent.innerHTML = `
    <dl class="summary-grid">
      <dt>Router-id</dt><dd>${esc(ipv4.routerId ?? "?")}</dd>
      <dt>RIB entries</dt><dd>${esc(ipv4.ribCount ?? "?")}</dd>
      <dt>Peers</dt><dd>${Object.keys(peers).length}</dd>
    </dl>
    <h3>Neighbors</h3>
    <table>
      <thead><tr><th>Peer</th><th>AS</th><th>State</th><th>Pfx Rcd</th></tr></thead>
      <tbody>${peerRows || '<tr><td colspan=4>none</td></tr>'}</tbody>
    </table>
    <h3>BGP table</h3>
    <table>
      <thead><tr><th>Prefix</th><th>Next-hop</th><th>AS-path</th><th>LP</th><th>MED</th><th>Communities</th></tr></thead>
      <tbody>${routeRows.join("") || '<tr><td colspan=6>empty</td></tr>'}</tbody>
    </table>
  `;
}

// eventClock renders the server's RFC 3339 UTC stamp on the READER's clock,
// and keeps the stamp itself as the tooltip. The payload carries the instant;
// the line carries the time the reader recognises. Milliseconds are shown
// because a reconvergence happens inside one second and the events of it would
// otherwise all read the same.
function eventClock(ts) {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return { text: String(ts ?? ""), title: String(ts ?? "") };
  const p = (n, w = 2) => String(n).padStart(w, "0");
  const text = `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
  return { text, title: String(ts) };
}

// Ids already rendered, so an event delivered twice — once in the catch-up
// fetch, once on the socket — is drawn once. Pruned with the list it mirrors.
const renderedEvents = new Set();
let lastEventId = 0;
// The process that issued the ids in renderedEvents. Ids restart at 1 with
// the poller, and `lastId < lastEventId` only sees a restart whose new
// history is SHORTER than what the page holds: measured on this lab, a fresh
// poller's first poll issues 18 events, so a page holding 18 saw lastId=18,
// asked since=18, and suppressed all 18 new events by id.
let pollerEpoch = null;

function addEvent(ev) {
  if (!ev) return;
  if (ev.id !== undefined && ev.id !== null) {
    if (renderedEvents.has(ev.id)) return;
    renderedEvents.add(ev.id);
    if (ev.id > lastEventId) lastEventId = ev.id;
  }
  const li = document.createElement("li");
  if (ev.id !== undefined && ev.id !== null) li.dataset.eventId = String(ev.id);
  const clock = eventClock(ev.ts);
  li.title = clock.title;
  const stamp = esc(clock.text);
  if (ev.kind === "session") {
    const gone = ev.change === "vanished";
    const cls = !gone && ev.state === "Established" ? "event-up" : "event-down";
    const who = `${esc(ev.node)} ↔ AS${esc(ev.remoteAs)} (${esc(ev.peer)})`;
    li.className = "event-session";
    if (gone) {
      li.innerHTML = `<span class="${esc(cls)}">[${stamp}]</span> ${who} <strong>vanished</strong> (was ${esc(ev.was || "?")})`;
    } else if (ev.change === "appeared") {
      li.innerHTML = `<span class="${esc(cls)}">[${stamp}]</span> ${who} appeared <strong>${esc(ev.state)}</strong>`;
    } else {
      li.innerHTML = `<span class="${esc(cls)}">[${stamp}]</span> ${who} ${esc(ev.was || "—")} → <strong>${esc(ev.state)}</strong>`;
    }
  } else if (ev.kind === "route") {
    const withdrawn = ev.change === "withdrawn";
    li.className = withdrawn ? "event-route event-down" : "event-route";
    li.innerHTML = withdrawn
      ? `[${stamp}] ${esc(ev.node)}: <strong>${esc(ev.prefix)}</strong> withdrawn (was via ${esc(ev.from || "—")})`
      : `[${stamp}] ${esc(ev.node)}: <strong>${esc(ev.prefix)}</strong> added via ${esc(ev.to || "—")}`;
  } else if (ev.kind === "bestpath") {
    li.className = "event-bestpath";
    li.innerHTML = `[${stamp}] ${esc(ev.node)} best-path for ${esc(ev.prefix)}: ${esc(ev.from || "—")} → <strong>${esc(ev.to || "—")}</strong>`;
  } else {
    li.textContent = `[${clock.text}] ${JSON.stringify(ev)}`;
  }
  eventsEl.prepend(li);
  // cap at 100 lines
  while (eventsEl.children.length > 100) {
    const dropped = eventsEl.lastChild;
    eventsEl.removeChild(dropped);
    const id = dropped && dropped.dataset ? dropped.dataset.eventId : null;
    if (id) renderedEvents.delete(Number(id));
  }
}

// catchUp asks for what the page missed. It runs on every socket open, not
// only the first: the reconnect gap is two seconds at best, and every event in
// it used to be lost with no trace that anything had been missed.
async function catchUp(retry = true) {
  try {
    const res = await fetch(`/api/events?since=${lastEventId}`, { cache: "no-store" });
    if (!res.ok) return;
    const body = await res.json();
    // A poller that restarted issues its ids from 1 again, and every one of
    // them is an id this page has already marked rendered: the socket's events
    // would be swallowed one by one until the new process passed the old
    // high-water mark, on a page that still said "connected". A lastId BELOW
    // the id we hold cannot have come from the process that gave us that id —
    // that is the tell. Forget the numbering and ask again from nothing.
    // `lastId < lastEventId` below is now the COMPATIBILITY path: it is all a
    // page has against a server that predates `epoch` — one already open
    // against the old build when this one is deployed. It can go one deploy
    // after this, and until then it costs a comparison.
    const otherProcess = typeof body.epoch === "string" && pollerEpoch !== null && body.epoch !== pollerEpoch;
    if (typeof body.epoch === "string") pollerEpoch = body.epoch;
    if (otherProcess || (typeof body.lastId === "number" && body.lastId < lastEventId)) {
      renderedEvents.clear();
      lastEventId = 0;
      // The rows already on the pane were numbered by the previous process
      // and they stay (they did happen) — but the prune reads a dropped
      // row's id back out of renderedEvents, and from here on that number
      // belongs to the new process. Measured: process A at 82 events, a
      // restart, then B's next 6 events pruned A's rows 1..6 and un-marked
      // B's 1..6, so a second delivery of any of them was drawn twice.
      for (const li of eventsEl.children) delete li.dataset.eventId;
      if (retry) return catchUp(false);
    }
    for (const ev of body.events || []) addEvent(ev);
  } catch (err) {
    // The socket is the primary path; a failed catch-up costs history, not
    // the live view, so it must not stop the page from connecting.
    console.warn("events catch-up failed", err);
  }
}

// The page used to render nothing at all until the socket delivered a
// snapshot: a slow, proxied or blocked WebSocket showed an empty page with no
// explanation of what it was waiting for. HTTP first, then the stream.
async function bootstrap() {
  try {
    const res = await fetch("/api/state", { cache: "no-store" });
    if (res.ok) {
      const body = await res.json();
      if (body.ready) {
        nodes = body.nodes || [];
        lastState = body.data || {};
        buildGraph();
      }
    }
  } catch (err) {
    console.warn("state bootstrap failed", err);
  }
  await catchUp();
  connect();
}

wireGraphTools();
bootstrap();

for (const id of ["view-events", "view-traffic"]) {
  const el = document.getElementById(id);
  if (el) el.addEventListener("click", (e) => setActivityView(e.currentTarget.dataset.view));
}
