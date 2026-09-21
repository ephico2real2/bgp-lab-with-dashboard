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
  unknown: "#8b8b8b",
};

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
};

function setStatus(text, cls) {
  statusEl.textContent = text;
  statusEl.className = "status " + cls;
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  setStatus("connecting…", "status-connecting");

  ws.onopen = () => setStatus("connected", "status-connected");
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

function buildElements() {
  const els = [];
  // nodes
  for (const n of nodes) {
    const role = nodeRole(n.name);
    els.push({
      data: {
        id: n.name,
        label: `${n.name}\nAS${n.asn ?? "?"}`,
        role,
      },
    });
  }

  // Build AS -> node lookup
  const asToNode = new Map();
  for (const n of nodes) if (n.asn) asToNode.set(n.asn, n.name);

  // For each BGP session we walk both sides so we can label each end of the
  // edge with the IP that belongs to that side. When node N reports peer IP X,
  // X lives on the OTHER router (the remote side of the session).
  const edges = new Map();   // sorted-pair key -> { source, target, sourceIP, targetIP, state }

  for (const n of nodes) {
    const peers = ((lastState[n.name] || {}).summary || {}).ipv4Unicast?.peers || {};
    for (const [peerIp, info] of Object.entries(peers)) {
      const remote = asToNode.get(info.remoteAs);
      if (!remote) continue;
      const [a, b] = [n.name, remote].sort();
      const key = `${a}--${b}`;
      let edge = edges.get(key);
      if (!edge) {
        edge = { id: key, source: a, target: b };
        edges.set(key, edge);
      }
      // peerIp belongs to the OTHER side of the session as seen from n.name
      if (remote === edge.source)  edge.sourceIP = peerIp;
      else                         edge.targetIP = peerIp;
      // Take the freshest non-empty state we encounter
      if (info.state) edge.state = info.state;
    }
  }

  for (const e of edges.values()) {
    els.push({
      data: {
        id: e.id,
        source: e.source,
        target: e.target,
        state: e.state || "unknown",
        sourceLabel: e.sourceIP || "",
        targetLabel: e.targetIP || "",
      },
    });
  }

  return els;
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
    ],
    layout: { name: "cose", animate: false, padding: 30 },
  });

  cy.on("tap", "node", (e) => {
    selectedNode = e.target.id();
    renderDetail(selectedNode);
  });
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
      if (el.data.sourceLabel && existing.data("sourceLabel") !== el.data.sourceLabel) {
        existing.data("sourceLabel", el.data.sourceLabel);
      }
      if (el.data.targetLabel && existing.data("targetLabel") !== el.data.targetLabel) {
        existing.data("targetLabel", el.data.targetLabel);
      }
    }
  }
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

function addEvent(ev) {
  const li = document.createElement("li");
  if (ev.kind === "session") {
    const cls = ev.state === "Established" ? "event-up" : "event-down";
    li.className = "event-session";
    li.innerHTML = `<span class="${esc(cls)}">[${esc(ev.ts)}]</span> ${esc(ev.node)} ↔ AS${esc(ev.remoteAs)} (${esc(ev.peer)}) → <strong>${esc(ev.state)}</strong>`;
  } else if (ev.kind === "bestpath") {
    li.className = "event-bestpath";
    li.innerHTML = `[${esc(ev.ts)}] ${esc(ev.node)} best-path for ${esc(ev.prefix)}: ${esc(ev.from || "—")} → <strong>${esc(ev.to || "—")}</strong>`;
  } else {
    li.textContent = `[${ev.ts}] ${JSON.stringify(ev)}`;
  }
  eventsEl.prepend(li);
  // cap at 100 lines
  while (eventsEl.children.length > 100) eventsEl.removeChild(eventsEl.lastChild);
}

connect();

for (const id of ["view-events", "view-traffic"]) {
  const el = document.getElementById(id);
  if (el) el.addEventListener("click", (e) => setActivityView(e.currentTarget.dataset.view));
}
