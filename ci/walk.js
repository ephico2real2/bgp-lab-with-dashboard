// ci/walk.js — Playwright walk of the dashboard for CI (the runner has no Chrome app).
// Opens the dashboard (DASHBOARD_URL, or 127.0.0.1:$DASHBOARD_PORT, default 8089),
// waits for four graph nodes, screenshots, runs
// an administrative shutdown of isp1's sessions, screenshots the outage, brings
// them back, screenshots the recovery, then runs the blog's own `clear bgp *`
// for the events pane. Timings go to ci/out/walk.log.
//
// Why shutdown and not `clear bgp *` for the photographed outage: a clear drops
// and re-establishes inside ~2 s (measured: run 35533915645 caught it, run
// 35533917595 polled past it and timed out after 60 s). A 2 s poller cannot be
// relied on to photograph a 2 s transient. `neighbor … shutdown` holds the
// sessions down until they are released, so the picture is of a known state.
//
// The graph is a Cytoscape canvas (no per-node DOM). Four nodes are confirmed
// from /api/state (what dashboard.js builds the graph from) plus #graph canvas
// and #status "connected". Session down/up is the same /api/state the UI reads.
const { chromium } = require("playwright");
const { execSync } = require("child_process");
const fs = require("fs");
const http = require("http");

const OUT = process.env.SHOTS_DIR || "ci/out/screenshots";
const LOG = process.env.WALK_LOG || "ci/out/walk.log";
const URL = process.env.DASHBOARD_URL || `http://127.0.0.1:${process.env.DASHBOARD_PORT || 8089}`;
const ISP1 = process.env.ISP1_CONTAINER || "clab-simple-lab-isp1";

fs.mkdirSync(OUT, { recursive: true });
const lines = [];
function log(msg) {
  lines.push(msg);
  console.log(msg);
}

function getState() {
  return new Promise((resolve, reject) => {
    const req = http.get(`${URL}/api/state`, { timeout: 3000 }, (res) => {
      let d = "";
      res.on("data", (c) => {
        d += c;
      });
      res.on("end", () => {
        try {
          resolve(JSON.parse(d));
        } catch (e) {
          reject(e);
        }
      });
    });
    req.on("error", reject);
    req.on("timeout", () => {
      req.destroy(new Error("timeout"));
    });
  });
}

function peersOf(state) {
  const blob = (state && state.data) || {};
  const out = [];
  for (const [node, nd] of Object.entries(blob)) {
    const ipv4 = (((nd || {}).summary || {}).ipv4Unicast || {});
    const peers = ipv4.peers || {};
    for (const [ip, info] of Object.entries(peers)) {
      out.push({ node, ip, state: (info && info.state) || "" });
    }
  }
  return out;
}

async function waitFor(label, limitSec, pred) {
  const start = Date.now();
  let last = "";
  while ((Date.now() - start) / 1000 < limitSec) {
    try {
      const s = await getState();
      last = JSON.stringify({
        ready: s.ready,
        nodes: (s.nodes || []).map((n) => n.name),
        peers: peersOf(s).map((p) => `${p.node}:${p.ip}=${p.state}`),
      });
      if (pred(s)) {
        const sec = ((Date.now() - start) / 1000).toFixed(1);
        log(`${label}: ${sec} s`);
        return s;
      }
    } catch (e) {
      last = String(e.message || e);
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw new Error(`${label}: timed out after ${limitSec} s last=${last}`);
}

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  await page.goto(`${URL}/`, { waitUntil: "load", timeout: 30000 });

  const startDom = Date.now();
  await page.waitForFunction(
    () => {
      const status = document.getElementById("status");
      const canvas = document.querySelector("#graph canvas");
      const connected = status && /connected/i.test(status.textContent || "");
      return Boolean(connected && canvas);
    },
    { timeout: 30000 }
  );
  log(`DOM connected+#graph canvas: ${((Date.now() - startDom) / 1000).toFixed(1)} s`);

  await waitFor("graph four nodes (/api/state)", 30, (s) => {
    const nodes = s.nodes || [];
    return s.ready === true && nodes.length === 4;
  });

  // The events pane must carry history BEFORE anything happens in this walk:
  // the page fetches /api/events over HTTP on load. Events used to be
  // broadcast and forgotten, so a page that arrived after the lab converged —
  // which is every page, in CI — showed an empty pane.
  const startEvents = Date.now();
  await page.waitForFunction(
    () => document.querySelectorAll("#events li").length > 0,
    { timeout: 30000 }
  );
  const onLoad = await page.evaluate(() => {
    const li = document.querySelector("#events li");
    return { n: document.querySelectorAll("#events li").length, ts: li.title };
  });
  log(`events pane on load: ${onLoad.n} line(s), newest stamped ${onLoad.ts} ` +
      `(${((Date.now() - startEvents) / 1000).toFixed(1)} s)`);

  await page.waitForTimeout(1000);
  await page.screenshot({ path: `${OUT}/01-steady.png`, fullPage: true });
  log(`wrote ${OUT}/01-steady.png`);

  const ISP1_ASN = process.env.ISP1_ASN || "65100";
  const vtysh = (args) =>
    execSync(`docker exec ${ISP1} vtysh ${args}`, { stdio: "inherit" });
  // the peers isp1 actually has, read from its running config — not a list
  // written here that would drift the moment configs/ changes
  const neighbors = (
    process.env.ISP1_NEIGHBORS ||
    execSync(`docker exec ${ISP1} vtysh -c 'show running-config'`, { encoding: "utf8" })
      .split("\n")
      .map((l) => (l.match(/^\s*neighbor (\S+) remote-as /) || [])[1])
      .filter(Boolean)
      .filter((v, i, a) => a.indexOf(v) === i)
      .join(",")
  )
    .split(",")
    .map((n) => n.trim())
    .filter(Boolean);
  if (neighbors.length === 0) {
    throw new Error(`no neighbors found in ${ISP1}'s running config`);
  }
  const shut = (verb) =>
    vtysh(
      ["-c 'conf t'", `-c 'router bgp ${ISP1_ASN}'`]
        .concat(neighbors.map((n) => `-c '${verb}neighbor ${n} shutdown'`))
        .join(" ")
    );
  log(`administrative shutdown of ${ISP1}'s ${neighbors.length} sessions`);
  shut("");

  await waitFor("dashboard shows a non-Established session", 60, (s) => {
    return peersOf(s).some((p) => p.state && p.state !== "Established");
  });
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${OUT}/02-sessions-down.png`, fullPage: true });
  log(`wrote ${OUT}/02-sessions-down.png`);

  log(`releasing ${ISP1}'s sessions`);
  shut("no ");
  await waitFor("dashboard sessions recovered", 90, (s) => {
    const peers = peersOf(s);
    return peers.length > 0 && peers.every((p) => p.state === "Established");
  });
  // the blog's own demo, for the events pane: the clear is recorded even though
  // the outage above is what gets photographed
  log(`clear bgp * on ${ISP1} (the blog's demo; events only)`);
  vtysh("-c 'clear bgp *'");
  await page.waitForTimeout(1000);
  await page.screenshot({ path: `${OUT}/03-recovered.png`, fullPage: true });
  log(`wrote ${OUT}/03-recovered.png`);

  // A reload must not empty the pane. This is the other half of the same fix:
  // the history survives the page, so what the outage above recorded is still
  // readable afterwards.
  const beforeReload = await page.evaluate(
    () => document.querySelectorAll("#events li").length
  );
  await page.reload({ waitUntil: "load", timeout: 30000 });
  await page.waitForFunction(
    () => document.querySelectorAll("#events li").length > 0,
    { timeout: 30000 }
  );
  const afterReload = await page.evaluate(
    () => document.querySelectorAll("#events li").length
  );
  if (afterReload < beforeReload) {
    throw new Error(
      `a reload lost history: ${beforeReload} lines before, ${afterReload} after`
    );
  }
  log(`events survive a reload: ${beforeReload} before, ${afterReload} after`);
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${OUT}/04-history.png`, fullPage: true });
  log(`wrote ${OUT}/04-history.png`);

  await browser.close();
  fs.writeFileSync(LOG, lines.join("\n") + "\n");
})().catch((err) => {
  log(`FAILED ${err.message || err}`);
  try {
    fs.writeFileSync(LOG, lines.join("\n") + "\n");
  } catch (_) {
    /* ignore */
  }
  process.exit(1);
});
