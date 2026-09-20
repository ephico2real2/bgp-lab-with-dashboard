// ci/walk.js — Playwright walk of the dashboard for CI (the runner has no Chrome app).
// Opens the dashboard (DASHBOARD_URL, or 127.0.0.1:$DASHBOARD_PORT, default 8089),
// waits for four graph nodes, screenshots, runs
// `clear bgp *` on isp1, waits for a non-Established session, screenshots, waits
// for recovery, screenshots. Timings go to ci/out/walk.log.
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

  await page.waitForTimeout(1000);
  await page.screenshot({ path: `${OUT}/01-steady.png`, fullPage: true });
  log(`wrote ${OUT}/01-steady.png`);

  log(`clear bgp * on ${ISP1}`);
  execSync(`docker exec ${ISP1} vtysh -c 'clear bgp *'`, { stdio: "inherit" });

  await waitFor("dashboard shows a non-Established session", 60, (s) => {
    return peersOf(s).some((p) => p.state && p.state !== "Established");
  });
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${OUT}/02-clear-bgp.png`, fullPage: true });
  log(`wrote ${OUT}/02-clear-bgp.png`);

  await waitFor("dashboard sessions recovered", 90, (s) => {
    const peers = peersOf(s);
    return peers.length > 0 && peers.every((p) => p.state === "Established");
  });
  await page.waitForTimeout(1000);
  await page.screenshot({ path: `${OUT}/03-recovered.png`, fullPage: true });
  log(`wrote ${OUT}/03-recovered.png`);

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
