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
function el(id, attrs = {}) {
  const node = {
    id, className: attrs.className || "", innerHTML: "", textContent: "", title: "", hidden: false,
    dataset: attrs.dataset || {}, children: attrs.children || [], style: {}, handlers: {},
    prepend(child) { this.children.unshift(child); },
    removeChild(child) { this.children = this.children.filter((c) => c !== child); },
    appendChild(child) { this.children.push(child); },
    // Handlers are KEPT and fired by the cases. A stub that swallowed them
    // would let a button wired to nothing pass every test.
    addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); },
    fire(type, ev = {}) { for (const fn of this.handlers[type] || []) fn({ currentTarget: this, preventDefault() {}, ...ev }); },
    setAttribute(k, v) { this[k] = v; (this.attrs ||= {})[k] = v; },
    getAttribute(k) { return (this.attrs || {})[k]; },
    classList: { contains: (c) => (attrs.className || "").split(/\s+/).includes(c) },
    querySelector() { return null; },
    querySelectorAll(sel) {
      const m = /^\[data-([a-z]+)\]$/.exec(sel);
      if (!m) return [];
      return this.children.filter((c) => c.dataset && c.dataset[m[1]] !== undefined);
    },
    get lastChild() { return this.children[this.children.length - 1] || null; },
  };
  return node;
}

function makeContext() {
  const byId = new Map();
  const timers = [];
  // The legend as index.html declares it, so a case can check that the page
  // paints the swatches rather than that the harness invented some.
  const legend = el("legend", { children: [
    el("s1", { className: "swatch", dataset: { state: "Established" } }),
    el("s2", { className: "swatch", dataset: { state: "Active" } }),
    el("s3", { className: "swatch", dataset: { state: "Idle" } }),
    el("s4", { className: "swatch swatch-dashed", dataset: { state: "vanished" } }),
    el("s5", { className: "swatch", dataset: { role: "edge" } }),
    el("s6", { className: "swatch", dataset: { role: "isp" } }),
  ] });
  legend.hidden = true;
  const document = {
    getElementById(id) {
      if (id === "legend") return legend;
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
    // Timers are RECORDED, not run. The page arms one to sweep a vanished edge
    // away, and a stub that swallowed it would let that timer disappear without
    // a single case noticing.
    setTimeout: (fn, ms) => { const id = timers.length + 1; timers.push({ fn, ms, id }); return id; },
    setImmediate,
    clearTimeout: (id) => { const i = timers.findIndex((t) => t.id === id); if (i >= 0) timers.splice(i, 1); },
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
  eventClock, VANISHED_GRACE_MS, connect, catchUp,
  eventsEl, sidebarContent, paintLegend, updateSelectionCount, wireGraphTools, handleResize,
  setSelected: (v) => { selectedNode = v; },
  trafficEl: document.getElementById("traffic"),
};`;
  vm.runInContext(src, ctx, { filename: "dashboard.js" });
  // ctx is the sandbox's global object: a case that needs a different fetch, or
  // a WebSocket it can drive, assigns it here — AFTER the module has loaded, so
  // the load-time bootstrap() still stalls on the never-settling default.
  return Object.assign(ctx.__t, {
    ctx,
    timers,
    runTimers: () => {
      const due = timers.splice(0, timers.length);
      for (const timer of due) timer.fn();
      return due.length;
    },
  });
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

  "a vanished edge is swept away even when no further state frame arrives": (t) => {
    // The poller broadcasts `state` only when the signature CHANGED, and
    // updateGraph is the only thing that removes a marked edge. Measured on the
    // running lab: 24 s of a steady fabric delivered 11 `signal` frames and 0
    // `state` frames — so the poll that made the peer vanish is the last one
    // that will call updateGraph, and the grace has to be a timer.
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
      let now = 3_000_000;
      Date.now = () => now;
      t.updateGraph();                       // the one and only state frame
      eq(cy.store.get("leaf1--spine").state, "vanished", "the edge is marked");
      if (!t.timers.length) throw new Error("nothing was scheduled to sweep it away");
      if (!(t.timers[0].ms >= t.VANISHED_GRACE_MS)) {
        throw new Error(`swept after ${t.timers[0].ms} ms, before the grace was up`);
      }
      t.updateGraph();
      t.updateGraph();
      eq(t.timers.length, 1, "one sweep is armed, not one per state frame");
      now += t.VANISHED_GRACE_MS + 1000;
      t.runTimers();
      if (cy.store.has("leaf1--spine")) throw new Error("the dead edge is still on the graph");
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

  "the labels of an existing edge are repainted, not only drawn once": (t) => {
    // buildGraph runs once; every later change reaches the graph through
    // updateGraph. A per-end state that is only ever set at first build is a
    // state the reader never sees change.
    t.setNodes(twoRouters);
    t.setState(stateOf({ fromLeaf: "Established", fromSpine: "Established" }));
    const cy = fakeCy(t.buildElements().map((e) => e.data));
    t.setCy(cy);
    t.setState(stateOf({ fromLeaf: "Established", fromSpine: "Idle" }));
    t.updateGraph();
    const e = cy.store.get("leaf1--spine");
    eq([e.sourceState, e.targetState], ["Established", "Idle"], "per-end states on the live edge");
    eq(e.sourceLabel, "10.0.0.1\nEstablished", "source label repainted");
    eq(e.targetLabel, "10.0.0.2\nIdle", "target label repainted");
    eq(e.state, "Idle", "and the edge takes the worse end");
  },

  "every socket open asks for the gap, not only the first": async (t) => {
    // The reconnect gap is two seconds at best and every event in it is
    // delivered to nobody: the socket carries no history of its own.
    const asked = [];
    t.ctx.fetch = (url) => {
      asked.push(String(url));
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ ready: true, events: [], lastId: 0 }) });
    };
    let sock = null;
    t.ctx.WebSocket = function () { sock = this; this.close = () => {}; };
    t.connect();
    if (!sock || typeof sock.onopen !== "function") throw new Error("connect() opened no socket");
    sock.onopen();
    await new Promise((r) => setImmediate(r));
    eq(asked.filter((u) => u.includes("/api/events")).length, 1, "catch-up fetches on socket open");
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

  "a poller that restarted does not silence the page": async (t) => {
    // Ids restart at 1 with the process. A page holding lastEventId=32 asks
    // `since=32`, is told the truth — lastId=2 — and would otherwise go on
    // suppressing every id it has already seen once, drawing nothing at all
    // until the new poller passed 32.
    for (let i = 1; i <= 32; i += 1) {
      t.addEvent({ id: i, kind: "session", change: "state", node: "leaf1", peer: "10.0.0.2",
                   remoteAs: 65100, state: "Idle", was: "Established", ts: "2026-09-21T10:00:00.000Z" });
    }
    const before = t.eventsEl.children.length;
    // The stub FILTERS on `since`, exactly as /api/events does — a stub that
    // returned its whole body whatever was asked would pass even if the page
    // kept asking from the old high-water mark, which is the half of this bug
    // that silences the pane.
    const fresh = [
      { id: 1, kind: "session", change: "appeared", node: "leaf1", peer: "10.0.0.2",
        remoteAs: 65100, state: "Established", ts: "2026-09-21T11:00:00.000Z" },
      { id: 2, kind: "route", change: "added", node: "leaf1", prefix: "10.9.9.0/24",
        to: "10.0.0.2", ts: "2026-09-21T11:00:00.000Z" },
    ];
    const asked = [];
    t.ctx.fetch = (url) => {
      const since = Number(new URL(String(url), "http://x").searchParams.get("since") || 0);
      asked.push(since);
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ ready: true, lastId: 2, events: fresh.filter((e) => e.id > since) }),
      });
    };
    await t.catchUp();
    eq(asked, [32, 0], "asks again from nothing once the restart is recognised");
    eq(t.eventsEl.children.length - before, 2, "the new poller's events are drawn");
  },

  "a second session that vanishes later is swept on its own timer": (t) => {
    // One sweep per countdown, not one per page: the sweep that removes the
    // first edge has to arm another while a second edge is still inside its
    // grace, and the flag it sets has to be cleared when it fires. Neither
    // was asserted: a flag that was never cleared, and a sweep that ignored
    // an edge still counting, both passed every case.
    t.setNodes([{ name: "a", asn: 1 }, { name: "b", asn: 2 }, { name: "c", asn: 3 }]);
    const cy = fakeCy([
      { id: "a" }, { id: "b" }, { id: "c" },
      { id: "a--b", source: "a", target: "b", state: "Established" },
      { id: "b--c", source: "b", target: "c", state: "Established" },
    ]);
    t.setCy(cy);
    const answered = (peers) => ({ summary: { ipv4Unicast: { peers } } });
    const bc = { "10.0.1.2": { remoteAs: 3, state: "Established" } };
    const cb = { "10.0.1.1": { remoteAs: 2, state: "Established" } };
    const realNow = Date.now;
    try {
      let now = 5_000_000;
      Date.now = () => now;
      t.setState({ a: answered({}), b: answered(bc), c: answered(cb) });
      t.updateGraph();                                   // a--b vanishes
      now += 20_000;
      t.setState({ a: answered({}), b: answered({}), c: answered({}) });
      t.updateGraph();                                   // b--c vanishes; the first sweep is still pending
      now += t.VANISHED_GRACE_MS + 250 - 20_000;
      eq(t.runTimers(), 1, "the first sweep fires");
      eq(cy.store.has("a--b"), false, "the first edge is gone");
      eq(cy.store.has("b--c"), true, "the second is still inside its grace");
      eq(t.timers.length, 1, "and a sweep is armed for it");
      now += t.VANISHED_GRACE_MS + 250;
      eq(t.runTimers(), 1, "the second sweep fires");
      eq(cy.store.has("b--c"), false, "the second edge is gone");
      cy.add({ data: { id: "a--c", source: "a", target: "c", state: "Established" } });
      t.updateGraph();
      eq(t.timers.length, 1, "a countdown that starts after both fired arms a fresh sweep");
    } finally {
      Date.now = realNow;
    }
  },

  "a closed socket is reopened, not left disconnected": (t) => {
    // The poller restarts on every redeploy. A page that did not reconnect
    // would read "disconnected" until someone reloaded it.
    let opened = 0;
    let sock = null;
    t.ctx.WebSocket = function () { opened += 1; sock = this; this.close = () => {}; };
    t.connect();
    eq(opened, 1, "one socket");
    sock.onclose();
    const retry = t.timers.find((x) => x.fn === t.connect);
    if (!retry) throw new Error("nothing was scheduled to reconnect after the socket closed");
    if (!(retry.ms >= 1000 && retry.ms <= 10000)) throw new Error(`reconnects after ${retry.ms} ms`);
    retry.fn();
    eq(opened, 2, "a second socket is opened");
  },

  "the newest event is at the top of the pane": (t) => {
    t.addEvent({ id: 1, kind: "route", change: "added", node: "a", prefix: "10.0.0.0/24", to: "x", ts: "2026-09-21T10:00:00.000Z" });
    t.addEvent({ id: 2, kind: "route", change: "added", node: "a", prefix: "10.0.1.0/24", to: "x", ts: "2026-09-21T10:00:01.000Z" });
    eq(t.eventsEl.children[0].dataset.eventId, "2", "the event added last is first");
    eq(t.eventsEl.children[1].dataset.eventId, "1", "and the older one is below it");
  },

  "the clock shows the reader's zone, not UTC": (t) => {
    // The format regex alone passes a clock that prints UTC hours. Node
    // re-reads TZ on every access, so the reader's zone can be set here.
    const realTZ = process.env.TZ;
    try {
      process.env.TZ = "Etc/GMT+5";               // five hours WEST of UTC (POSIX sign), no DST
      eq(t.eventClock("2026-09-21T10:00:00.250Z").text, "05:00:00.250", "10:00Z read five hours west");
      process.env.TZ = "Etc/GMT-9";               // nine hours east
      eq(t.eventClock("2026-09-21T10:00:00.250Z").text, "19:00:00.250", "10:00Z read nine hours east");
    } finally {
      if (realTZ === undefined) delete process.env.TZ; else process.env.TZ = realTZ;
    }
  },

  "a state frame repaints the selected router's sidebar": (t) => {
    t.setNodes(twoRouters);
    t.setState(stateOf({ fromLeaf: "Established", fromSpine: "Established" }));
    t.setCy(fakeCy(t.buildElements().map((e) => e.data)));
    let sock = null;
    t.ctx.WebSocket = function () { sock = this; this.close = () => {}; };
    t.connect();
    t.setSelected("leaf1");
    sock.onmessage({ data: JSON.stringify({ type: "state", data: stateOf({ fromLeaf: "Idle", fromSpine: "Idle" }) }) });
    if (!/Idle/.test(t.sidebarContent.innerHTML)) throw new Error("the sidebar still shows the state before the frame");
  },

  "a signal frame repaints the Traffic view while it is showing": (t) => {
    let sock = null;
    t.ctx.WebSocket = function () { sock = this; this.close = () => {}; };
    t.connect();
    t.trafficEl.hidden = false;
    sock.onmessage({ data: JSON.stringify({ type: "signal", data: { leaf1: { "10.0.0.2": {
      state: "Established", hasDelta: true, dRcvd: 3, dSent: 1, dPfxRcd: 0, dPfxSnt: 0,
      msgRcvd: 10, msgSent: 10, pfxRcd: 2, pfxSnt: 2, flaps: 0, hasTimers: false } } } }) });
    if (!/10\.0\.0\.2/.test(t.trafficEl.innerHTML)) throw new Error("the Traffic view was not repainted from the signal frame");
  },

  "a vanished session is drawn as a failure, not as unknown": (t) => {
    eq(t.stateColor("vanished"), t.stateColor("Idle"), "the same colour as a session that is down");
    if (t.stateColor("vanished") === t.stateColor("")) throw new Error("vanished is drawn in the unknown colour");
  },

  "a restart whose first poll is as long as the page's history is still recognised": async (t) => {
    // Measured on this lab: the first poll of a fresh poller issues 18 events
    // (10 appeared, 8 added). A page holding 18 ids asks `since=18` and is
    // told lastId=18 — not below what it holds — so the id tell alone misses
    // the restart and all 18 new events are suppressed by ids the page marked
    // rendered in the previous process. The process has to name itself.
    const burst = (hour) => Array.from({ length: 18 }, (_, i) => ({
      id: i + 1, kind: "session", change: "appeared", node: "leaf1", peer: `10.9.${i}.1`,
      remoteAs: 65100, state: "Established", ts: `2026-09-21T${hour}:00:00.000Z` }));
    let ring = burst("10");
    let epoch = "epoch-a";                              // opaque on purpose: the page compares, never parses
    const asked = [];
    t.ctx.fetch = (url) => {
      const since = Number(new URL(String(url), "http://x").searchParams.get("since") || 0);
      asked.push(since);
      const events = ring.filter((e) => e.id > since);
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ ready: true, epoch, lastId: ring.length, events }) });
    };
    await t.catchUp();
    eq(t.eventsEl.children.length, 18, "the first process's history is drawn");
    ring = burst("11");
    epoch = "epoch-b";                                  // the poller restarted and re-read the fabric
    await t.catchUp();
    eq(asked, [0, 18, 0], "the new process is recognised and asked from nothing");
    eq(t.eventsEl.children.length, 36, "all 18 of the new process's events are drawn");
  },

  "rows numbered by the previous process do not un-mark the new one's events": async (t) => {
    // The pane keeps the previous process's rows after the restart tell —
    // they did happen — but the prune reads a dropped row's id back into
    // renderedEvents, and after the tell those numbers belong to the NEW
    // process. Measured on the lab: process A at 82 events, restart, B's 18
    // fetched, B's next 6 arrive → B's ids 1..6 no longer in the Set while
    // their rows are on the pane. A second delivery of one of them — the
    // catch-up/socket overlap the Set exists for — was then drawn twice.
    const evs = (n, hour) => Array.from({ length: n }, (_, i) => ({
      id: i + 1, kind: "route", change: "added", node: "leaf1", prefix: `10.${i}.0.0/16`, to: "x",
      ts: `2026-09-21T${hour}:00:${String(i % 60).padStart(2, "0")}.000Z` }));
    let ring = evs(90, "10"), epoch = "A";
    t.ctx.fetch = (url) => {
      const since = Number(new URL(String(url), "http://x").searchParams.get("since") || 0);
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ ready: true, epoch, lastId: ring.length, events: ring.filter((e) => e.id > since) }) });
    };
    await t.catchUp();
    eq(t.eventsEl.children.length, 90, "process A's history");
    ring = evs(18, "11"); epoch = "B";
    await t.catchUp();                                  // the tell fires; B's 18 are drawn; 8 of A's rows are pruned
    eq(t.eventsEl.children.length, 100, "the pane is at its cap");
    t.addEvent({ ...ring[0] });                         // B's event 1 delivered again (socket after catch-up)
    const drawnAs1 = t.eventsEl.children.filter((li) => li.dataset.eventId === "1").length;
    eq(drawnAs1, 1, "B's event 1 is drawn once");
  },

  "a sweep armed before a poller restart still removes the edge after it": (t) => {
    // The restart tell resets the events' numbering; the sweep is graph
    // state and must neither be lost nor fire against the wrong data. The
    // reconnect delivers a snapshot whose `data` may be {} (the new poller
    // has not polled yet), then the new poller's first state frame.
    t.setNodes(twoRouters);
    const cy = fakeCy([
      { id: "leaf1" }, { id: "spine" },
      { id: "leaf1--spine", source: "leaf1", target: "spine", state: "Established" },
    ]);
    t.setCy(cy);
    const realNow = Date.now;
    try {
      let now = 7_000_000;
      Date.now = () => now;
      t.setState({ leaf1: { summary: { ipv4Unicast: { peers: {} } } }, spine: { summary: { ipv4Unicast: { peers: {} } } } });
      t.updateGraph();                                   // marked; sweep armed
      eq(t.timers.length, 1, "armed");
      now += 10_000;
      t.setState({}); t.updateGraph();                   // the reconnect's snapshot: nobody has answered yet
      eq(cy.store.get("leaf1--spine").state, "vanished", "still marked, not removed on unread routers");
      now += 3_000;
      t.setState({ leaf1: { summary: { ipv4Unicast: { peers: {} } } }, spine: { summary: { ipv4Unicast: { peers: {} } } } });
      t.updateGraph();                                   // the new poller's first state frame
      eq(t.timers.length, 1, "one sweep, still the original");
      now += t.VANISHED_GRACE_MS + 250 - 13_000;
      eq(t.runTimers(), 1, "it fires");
      if (cy.store.has("leaf1--spine")) throw new Error("the edge marked before the restart was not swept");
      eq(t.timers.length, 0, "nothing left armed");
    } finally {
      Date.now = realNow;
    }
  },

  "every legend swatch is painted from the colour the graph uses": (t) => {
    // The legend carries no colour of its own. If it did, the page and its key
    // would be two sources of truth for the same fact, and the first time one
    // changed the other would quietly lie.
    t.paintLegend();
    const legend = t.ctx.document.getElementById("legend");
    const byState = Object.fromEntries(legend.children
      .filter((c) => c.dataset.state)
      .map((c) => [c.dataset.state, c.classList.contains("swatch-dashed") ? c.style.borderTopColor : c.style.background]));
    eq(byState.Established, t.stateColor("Established"), "Established");
    eq(byState.Idle, t.stateColor("Idle"), "Idle");
    eq(byState.Active, t.stateColor("Active"), "Active");
    eq(byState.vanished, t.stateColor("vanished"), "vanished (the dashed swatch)");
    if (byState.Established === byState.Idle) throw new Error("up and down are drawn the same colour");
    const isp = legend.children.find((c) => c.dataset.role === "isp");
    if (!/#/.test(String(isp.style.background))) throw new Error("the role swatch was not painted");
  },

  "the tools select, clear and toggle — and are wired to something": (t) => {
    t.setNodes(twoRouters);
    t.setState(stateOf({ fromLeaf: "Established", fromSpine: "Established" }));
    const sel = new Set();
    const nodes = [{ id: () => "leaf1" }, { id: () => "spine" }];
    const collection = {
      select: () => nodes.forEach((n) => sel.add(n.id())),
      unselect: () => sel.clear(),
    };
    t.setCy({
      nodes: () => collection,
      $: () => ({ length: sel.size }),
      edges: () => [],
      getElementById: () => ({ empty: () => true }),
      add: () => {},
      layout: () => ({ run: () => { collection.laidOut = true; } }),
    });
    t.wireGraphTools();
    const doc = t.ctx.document;

    doc.getElementById("select-all").fire("click");
    eq([...sel].sort(), ["leaf1", "spine"], "select all");
    if (!/2 selected/.test(doc.getElementById("sel-count").textContent)) {
      throw new Error(`the count does not say what is selected: ${doc.getElementById("sel-count").textContent}`);
    }
    doc.getElementById("clear-sel").fire("click");
    eq(sel.size, 0, "clear");

    // Ctrl/Cmd+A and Escape, scoped to the graph
    doc.getElementById("graph").fire("keydown", { ctrlKey: true, key: "a" });
    eq(sel.size, 2, "ctrl+A selects every node");
    doc.getElementById("graph").fire("keydown", { key: "Escape" });
    eq(sel.size, 0, "escape clears");

    doc.getElementById("reset-layout").fire("click");
    eq(collection.laidOut, true, "reset layout runs a layout");

    const legend = doc.getElementById("legend");
    const toggle = doc.getElementById("legend-toggle");
    eq(legend.hidden, true, "the legend starts closed");
    toggle.fire("click");
    eq(legend.hidden, false, "and opens");
    eq(toggle.getAttribute("aria-expanded"), "true", "the button says so");
    toggle.fire("click");
    eq(legend.hidden, true, "and closes again");
    eq(toggle.getAttribute("aria-expanded"), "false", "and says that too");
  },

  "the graph is re-fitted when the pane changes size": (t) => {
    // Cytoscape reads its container's size once. After the columns stack at a
    // phone width the canvas keeps its old dimensions and most of the graph is
    // outside the viewport — measured: one node of four visible at 375 px.
    const calls = [];
    t.setCy({ resize: () => calls.push("resize"), fit: (e, p) => calls.push(`fit:${p}`),
              edges: () => [], nodes: () => ({ select() {}, unselect() {} }), $: () => ({ length: 0 }),
              getElementById: () => ({ empty: () => true }), add: () => {} });
    t.handleResize();
    eq(calls, [], "nothing happens until the burst of resize events settles");
    eq(t.timers.length, 1, "one debounce timer");
    t.handleResize();
    t.handleResize();
    eq(t.timers.length, 1, "still one — a drag across the screen is not fifty re-fits");
    t.runTimers();
    eq(calls, ["resize", "fit:30"], "the canvas is re-read and the nodes brought back into view");
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

(async () => {
  const results = [];
  for (const [name, fn] of Object.entries(CASES)) {
    try {
      await fn(makeContext());      // a case may be async: two of them drive fetch
      results.push({ name, ok: true, detail: "" });
    } catch (err) {
      results.push({ name, ok: false, detail: String(err && err.message || err) });
    }
  }
  process.stdout.write(JSON.stringify({ cases: results }));
})();
