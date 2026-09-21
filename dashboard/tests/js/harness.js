// Runs the REAL dashboard.js in a vm with the smallest stubs the page needs,
// and exercises the logic that decides what a reader sees: which of a
// session's two states colours the edge, what happens to an edge whose session
// is gone, and whether an event delivered twice is drawn twice.
//
// There is no JS test runner in this repo and adding one to assert a handful
// of properties would cost more than it is worth — but these are decisions,
// not markup, and a static read of the source cannot check a decision. So the
// file is loaded as it ships, with one line appended that hands the harness
// the functions to call. Nothing is re-implemented here.
//
// Prints JSON: {"cases": [{"name": ..., "ok": bool, "detail": ...}]}
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = path.resolve(__dirname, "..", "..", "app", "static", "dashboard.js");

// ---- the smallest DOM the page touches ------------------------------------
function el(id) {
  const node = {
    id, className: "", innerHTML: "", textContent: "", title: "", hidden: false,
    dataset: {}, children: [],
    prepend(child) { this.children.unshift(child); },
    removeChild(child) { this.children = this.children.filter((c) => c !== child); },
    appendChild(child) { this.children.push(child); },
    addEventListener() {},
    querySelector() { return null; },
    get lastChild() { return this.children[this.children.length - 1] || null; },
  };
  return node;
}

function makeContext() {
  const byId = new Map();
  const document = {
    getElementById(id) {
      if (!byId.has(id)) byId.set(id, el(id));
      return byId.get(id);
    },
    createElement() { return el("new"); },
  };
  const ctx = {
    document,
    console,
    // bootstrap() runs when the file loads. A promise that never settles keeps
    // it from reaching connect() or the events catch-up, so the harness has
    // the module's state to itself.
    fetch: () => new Promise(() => {}),
    WebSocket: function () { return { close() {} }; },
    cytoscape: () => { throw new Error("buildGraph must not run in the harness"); },
    location: { protocol: "http:", host: "127.0.0.1:8089" },
    setTimeout: () => 0,
    Date,
    JSON,
    Math,
    Number,
    String,
    Object,
    Set,
    Map,
    NaN,
  };
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  const src = fs.readFileSync(SRC, "utf8") + `
globalThis.__t = {
  setNodes: (v) => { nodes = v; },
  setState: (v) => { lastState = v; },
  setCy: (v) => { cy = v; },
  buildElements, updateGraph, worseState, stateRank, stateColor, addEvent,
  eventClock, VANISHED_GRACE_MS,
  eventsEl,
};`;
  vm.runInContext(src, ctx, { filename: "dashboard.js" });
  return ctx.__t;
}

// ---- a Cytoscape stub, only what updateGraph calls -------------------------
function fakeCy(initial) {
  const store = new Map(initial.map((e) => [e.id, { ...e }]));
  const wrap = (id) => ({
    empty: () => !store.has(id),
    isEdge: () => (store.get(id) || {}).source !== undefined,
    id: () => id,
    data(k, v) {
      const rec = store.get(id);
      if (v === undefined) return rec ? rec[k] : undefined;
      rec[k] = v;
      return rec;
    },
    remove() { store.delete(id); },
  });
  return {
    store,
    getElementById: (id) => wrap(id),
    add: (el) => store.set(el.data.id, { ...el.data }),
    edges: () => [...store.keys()].filter((k) => store.get(k).source !== undefined).map(wrap),
  };
}

// ---- cases ----------------------------------------------------------------
function eq(actual, expected, what) {
  const a = JSON.stringify(actual), b = JSON.stringify(expected);
  if (a !== b) throw new Error(`${what}: got ${a}, expected ${b}`);
}

const twoRouters = [{ name: "leaf1", asn: 65101 }, { name: "spine", asn: 65100 }];

function stateOf(peers) {
  // one session seen from both ends, each end's own view of it
  return {
    leaf1: { summary: { ipv4Unicast: { peers: { "10.0.0.2": { remoteAs: 65100, state: peers.fromLeaf } } } } },
    spine: { summary: { ipv4Unicast: { peers: { "10.0.0.1": { remoteAs: 65101, state: peers.fromSpine } } } } },
  };
}

const CASES = {
  "the worse of two states wins, whichever order they arrive in": (t) => {
    eq(t.worseState("Established", "Idle"), "Idle", "down beats up");
    eq(t.worseState("Idle", "Established"), "Idle", "and the other way round");
    eq(t.worseState("Active", "Idle"), "Idle", "Idle is worse than Active");
    eq(t.worseState("Established", "Active"), "Active", "Active is worse than Established");
  },

  "a state FRR adds later is never treated as healthy": (t) => {
    eq(t.stateRank("Established"), 0, "Established");
    eq(t.worseState("Established", "Clearing"), "Clearing", "an unknown state outranks Established");
    eq(t.stateRank("Idle (Admin)") , 3, "a qualified state matches on its prefix");
  },

  "an empty state is not a state": (t) => {
    eq(t.worseState("", "Active"), "Active", "empty then a state");
    eq(t.worseState("Established", ""), "Established", "a state then empty");
  },

  "a half-down link draws down, not whichever end was iterated last": (t) => {
    t.setNodes(twoRouters);
    t.setState(stateOf({ fromLeaf: "Established", fromSpine: "Idle" }));
    const edge = t.buildElements().find((e) => e.data.source && e.data.target);
    eq(edge.data.state, "Idle", "edge state");
    // and the same fabric with the ends swapped must read the same
    t.setState(stateOf({ fromLeaf: "Idle", fromSpine: "Established" }));
    const swapped = t.buildElements().find((e) => e.data.source && e.data.target);
    eq(swapped.data.state, "Idle", "edge state with the ends swapped");
  },

  "each end's own state is kept, and labelled only when they disagree": (t) => {
    t.setNodes(twoRouters);
    t.setState(stateOf({ fromLeaf: "Established", fromSpine: "Idle" }));
    const e = t.buildElements().find((x) => x.data.source).data;
    // leaf1 sorts before spine, so source is leaf1. Each end carries what THAT
    // router says about the session — leaf1 still reads Established while
    // spine has gone to Idle — beside that router's own address. Reading it
    // the other way round (each end showing the far side's opinion) would put
    // the disagreement on the wrong node.
    eq([e.sourceState, e.targetState], ["Established", "Idle"], "per-end states");
    // the state on its own line under the address, not in parentheses beside
    // it: the width is what collides on a crowded graph
    eq(e.sourceLabel, "10.0.0.1\nEstablished", "source label");
    eq(e.targetLabel, "10.0.0.2\nIdle", "target label");

    t.setState(stateOf({ fromLeaf: "Established", fromSpine: "Established" }));
    const agreed = t.buildElements().find((x) => x.data.source).data;
    eq([agreed.sourceLabel, agreed.targetLabel], ["10.0.0.1", "10.0.0.2"],
       "agreeing ends show addresses only");
  },

  "a session neither router reports is marked, held, then dropped": (t) => {
    t.setNodes(twoRouters);
    const cy = fakeCy([
      { id: "leaf1" }, { id: "spine" },
      { id: "leaf1--spine", source: "leaf1", target: "spine", state: "Established" },
    ]);
    t.setCy(cy);
    t.setState({ leaf1: { summary: { ipv4Unicast: { peers: {} } } },
                 spine: { summary: { ipv4Unicast: { peers: {} } } } });

    const realNow = Date.now;
    try {
      let now = 1_000_000;
      Date.now = () => now;

      t.updateGraph();
      eq(cy.store.get("leaf1--spine").state, "vanished", "the edge is marked");

      now += t.VANISHED_GRACE_MS - 1;
      t.updateGraph();
      if (!cy.store.has("leaf1--spine")) throw new Error("dropped before the grace period was up");

      now += 2;
      t.updateGraph();
      if (cy.store.has("leaf1--spine")) throw new Error("still on the graph after the grace period");
    } finally {
      Date.now = realNow;
    }
  },

  "a session that comes back stops counting down": (t) => {
    t.setNodes(twoRouters);
    const cy = fakeCy([
      { id: "leaf1" }, { id: "spine" },
      { id: "leaf1--spine", source: "leaf1", target: "spine", state: "Established" },
    ]);
    t.setCy(cy);
    t.setState({ leaf1: { summary: { ipv4Unicast: { peers: {} } } },
                 spine: { summary: { ipv4Unicast: { peers: {} } } } });
    const realNow = Date.now;
    try {
      let now = 1_000_000;
      Date.now = () => now;
      t.updateGraph();
      eq(cy.store.get("leaf1--spine").vanishedAt, now, "marked at this tick");

      t.setState(stateOf({ fromLeaf: "Established", fromSpine: "Established" }));
      now += 1000;
      t.updateGraph();
      eq(cy.store.get("leaf1--spine").vanishedAt, 0, "the countdown is cleared");
      eq(cy.store.get("leaf1--spine").state, "Established", "and it is painted again");

      now += t.VANISHED_GRACE_MS * 2;
      t.updateGraph();
      if (!cy.store.has("leaf1--spine")) throw new Error("a live session was removed by a stale countdown");
    } finally {
      Date.now = realNow;
    }
  },

  "an unreachable router does not delete its edges": (t) => {
    t.setNodes(twoRouters);
    const cy = fakeCy([
      { id: "leaf1" }, { id: "spine" },
      { id: "leaf1--spine", source: "leaf1", target: "spine", state: "Established" },
    ]);
    t.setCy(cy);
    // spine answered and reports nothing; leaf1's poll FAILED. The session is
    // not in the rebuilt set either way — but we have not looked at leaf1.
    t.setState({ leaf1: { error: "RuntimeError('vtysh timed out')" },
                 spine: { summary: { ipv4Unicast: { peers: {} } } } });
    const realNow = Date.now;
    try {
      let now = 2_000_000;
      Date.now = () => now;
      t.updateGraph();
      eq(cy.store.get("leaf1--spine").state, "Established", "still painted as last measured");
      if (cy.store.get("leaf1--spine").vanishedAt) throw new Error("started a countdown on an unread router");
      now += t.VANISHED_GRACE_MS * 3;
      t.updateGraph();
      if (!cy.store.has("leaf1--spine")) throw new Error("deleted the edge of a router it could not read");
    } finally {
      Date.now = realNow;
    }
  },

  "an event delivered twice is drawn once": (t) => {
    const before = t.eventsEl.children.length;
    const ev = { id: 4242, kind: "session", change: "state", node: "leaf1",
                 peer: "10.0.0.2", remoteAs: 65100, state: "Idle", was: "Established",
                 ts: "2026-09-21T10:00:00.000Z" };
    t.addEvent(ev);
    t.addEvent({ ...ev });                       // the same event, second delivery
    eq(t.eventsEl.children.length - before, 1, "lines added");
    t.addEvent({ ...ev, id: 4243 });
    eq(t.eventsEl.children.length - before, 2, "a different id is a different event");
  },

  "the stamp is shown on the reader's clock and kept in the tooltip": (t) => {
    const c = t.eventClock("2026-09-21T10:00:00.250Z");
    if (!/^\d{2}:\d{2}:\d{2}\.\d{3}$/.test(c.text)) throw new Error(`text: ${c.text}`);
    eq(c.title, "2026-09-21T10:00:00.250Z", "tooltip carries the server's stamp");
    eq(c.text.endsWith(".250"), true, "milliseconds survive");
    const junk = t.eventClock("not a time");
    eq(junk.text, "not a time", "an unparseable stamp is shown as it came");
  },
};

const results = [];
for (const [name, fn] of Object.entries(CASES)) {
  try {
    fn(makeContext());
    results.push({ name, ok: true, detail: "" });
  } catch (err) {
    results.push({ name, ok: false, detail: String(err && err.message || err) });
  }
}
process.stdout.write(JSON.stringify({ cases: results }));
