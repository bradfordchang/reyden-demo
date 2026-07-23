/* Reyden Query Race — frontend */
"use strict";

const $ = (id) => document.getElementById(id);
const fmtMs = (ms) => ms == null ? "–" : ms >= 10000 ? (ms / 1000).toFixed(1) + "s" : Math.round(ms).toLocaleString() + "ms";
const fmtS = (ms) => ms == null ? "–" : (ms / 1000).toFixed(1) + "s";
const median = (a) => { const s = [...a].sort((x, y) => x - y); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
const geomean = (a) => Math.exp(a.reduce((t, x) => t + Math.log(x), 0) / a.length);
const esc = (s) => { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; };

const state = {
  dashboards: [], detail: null,       // detail: /api/dashboards/{id} response
  reyden: [], reyId: null,
  runs: 1,
  race: null, poll: null, raf: null, lastSnap: null, lastSnapAt: 0,
};

const scenarios = () => (state.detail ? state.detail.scenarios : []);

async function getJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

/* ---------- rendering ---------- */

function whMeta(wh, suffix) {
  if (!wh) return null;
  return [wh.size, wh.serverless ? "serverless" : null, suffix].filter(Boolean).join(" · ");
}

function updateContenders() {
  const rey = state.reyden.find((w) => w.id === state.reyId);
  $("rey-name").textContent = rey ? rey.name : "Reyden";
  $("rey-meta").textContent = rey ? whMeta(rey, "Reyden") : "pick a warehouse";
  const wh = state.detail && state.detail.warehouse;
  $("base-name").textContent = wh ? wh.name : "Dashboard warehouse";
  $("base-meta").textContent = wh ? whMeta(wh, "dashboard's warehouse") : "pick a dashboard";
  $("go").disabled = !(state.detail && scenarios().length && state.reyId);
}

function buildTrack() {
  const track = $("track");
  track.innerHTML = "";
  if (!scenarios().length) return;
  const h = document.createElement("div");
  h.className = "page-header";
  h.textContent = `${state.detail.name} — dataset queries`;
  track.appendChild(h);
  // Dataset names can contain characters that are invalid in DOM ids,
  // so rows are keyed by scenario index instead.
  scenarios().forEach((sc, i) => {
    const row = document.createElement("div");
    row.className = "row";
    row.id = `row-${i}`;
    row.innerHTML = `
      <div class="label">${esc(sc.label)}<small>${esc(sc.id)}</small></div>
      <div class="lanes">
        <div class="lane reyden"><span class="who">REY</span>
          <div class="bar-wrap"><div class="bar" id="bar-${i}-reyden"></div></div>
          <span class="ms" id="ms-${i}-reyden">–</span></div>
        <div class="lane baseline"><span class="who">BASE</span>
          <div class="bar-wrap"><div class="bar" id="bar-${i}-baseline"></div></div>
          <span class="ms" id="ms-${i}-baseline">–</span></div>
      </div>
      <div class="verdict" id="v-${i}"></div>`;
    track.appendChild(row);
  });
}

function laneTimes(results, scId, lane) {
  return results.filter((r) => r.scenario_id === scId && r.lane === lane && !r.error).map((r) => r.wall_ms);
}

function render(snap, extrapolate = 0) {
  const results = snap.results;
  const inflight = {};
  for (const lane of ["reyden", "baseline"]) {
    for (const cur of snap.lanes[lane].inflight || []) {
      inflight[`${cur.scenario_id}|${lane}`] = cur.elapsed_ms + extrapolate;
    }
  }

  const ratios = [], totals = { reyden: 0, baseline: 0 };
  let wins = 0, pairs = 0;

  scenarios().forEach((sc, i) => {
    const rey = laneTimes(results, sc.id, "reyden");
    const base = laneTimes(results, sc.id, "baseline");
    const reyMed = rey.length ? median(rey) : null;
    const baseMed = base.length ? median(base) : null;
    const reyLive = inflight[`${sc.id}|reyden`];
    const baseLive = inflight[`${sc.id}|baseline`];
    const rowMax = Math.max(reyMed || 0, baseMed || 0, reyLive || 0, baseLive || 0, 1);

    for (const [lane, med, live] of [["reyden", reyMed, reyLive], ["baseline", baseMed, baseLive]]) {
      const bar = $(`bar-${i}-${lane}`), ms = $(`ms-${i}-${lane}`);
      if (!bar) continue;
      const err = results.find((r) => r.scenario_id === sc.id && r.lane === lane && r.error);
      if (live != null) {
        bar.style.width = `${Math.min(98, (live / rowMax) * 100)}%`;
        bar.classList.add("running");
        ms.textContent = fmtMs(live);
        ms.className = "ms live";
      } else if (med != null) {
        bar.style.width = `${(med / rowMax) * 100}%`;
        bar.classList.remove("running");
        ms.textContent = fmtMs(med);
        ms.className = "ms";
      } else if (err) {
        bar.style.width = "100%";
        bar.classList.remove("running");
        ms.textContent = "error";
        ms.className = "ms err";
        ms.title = err.error;
      }
    }

    const v = $(`v-${i}`);
    if (reyMed && baseMed) {
      const ratio = baseMed / reyMed;
      ratios.push(ratio); pairs++;
      if (ratio > 1) wins++;
      v.innerHTML = `<span class="flash">${ratio.toFixed(1)}×</span><small>${ratio >= 1 ? "REYDEN FASTER" : "BASELINE FASTER"}</small>`;
      $(`row-${i}`).classList.toggle("winner-rey", ratio > 1);
    } else v.innerHTML = "";
    totals.reyden += rey.reduce((a, b) => a + b, 0);
    totals.baseline += base.reduce((a, b) => a + b, 0);
  });

  // KPIs
  $("kpis").hidden = false;
  $("k-speedup").textContent = ratios.length ? geomean(ratios).toFixed(1) + "×" : "–";
  $("k-wins").textContent = pairs ? `${wins}/${pairs}` : "–";
  const done = results.length;
  const totalQ = snap.scenario_ids.length * snap.runs * 2;
  $("k-progress").textContent = `${done}/${totalQ}`;
  const tMax = Math.max(totals.reyden, totals.baseline, 1);
  $("t-rey").style.width = `${(totals.reyden / tMax) * 100}%`;
  $("t-base").style.width = `${(totals.baseline / tMax) * 100}%`;
  $("tv-rey").textContent = fmtS(totals.reyden || null);
  $("tv-base").textContent = fmtS(totals.baseline || null);

  // status
  const line = $("status-line");
  if (snap.status === "running") {
    const ready = Object.values(snap.lanes).every((l) => l.ready);
    line.textContent = ready ? `Racing — all ${snap.scenario_ids.length} dataset queries in flight at once on both warehouses…` : "Warming up both warehouses (excluded from timings)…";
    line.className = ready ? "status-line" : "status-line warming";
  }

  // finished
  if (snap.status === "done" && snap.summary) {
    const s = snap.summary;
    const rey = snap.warehouses.reyden, base = snap.warehouses.baseline;
    const banner = $("banner");
    banner.hidden = false;
    banner.innerHTML = s.geomean_speedup
      ? `🏁 <b>${esc(rey.name)} ran “${esc(snap.dashboard.name)}” ${s.geomean_speedup.toFixed(1)}× faster</b> —
         winning ${s.reyden_wins} of ${s.scenario_count} datasets (range ${s.min_speedup.toFixed(1)}×–${s.max_speedup.toFixed(1)}×),
         on a <b>${esc(rey.size || "?")}</b> Reyden vs the dashboard's <b>${esc(base.size || "?")}</b> ${esc(base.name)}.` +
        (s.load_ms && s.load_ms.reyden && s.load_ms.baseline
          ? ` Full dashboard load, all queries at once: <b>${fmtS(s.load_ms.reyden)}</b> vs <b>${fmtS(s.load_ms.baseline)}</b>.`
          : "")
      : "Race complete.";
    line.textContent = "Race complete — run it again or pick another dashboard.";
    line.className = "status-line";
    const laneErr = Object.entries(snap.lanes).find(([, l]) => l.error);
    if (laneErr) line.textContent = `Lane ${laneErr[0]} failed: ${laneErr[1].error}`;
  }
}

/* ---------- race loop ---------- */

async function poll() {
  if (!state.race) return;
  try {
    const snap = await getJSON(`/api/race/${state.race}`);
    state.lastSnap = snap;
    state.lastSnapAt = performance.now();
    render(snap);
    if (snap.status === "done") stopRace(false);
  } catch (e) { /* transient — keep polling */ }
}

function animate() {
  if (state.lastSnap && state.lastSnap.status === "running") {
    render(state.lastSnap, performance.now() - state.lastSnapAt);
  }
  state.raf = requestAnimationFrame(animate);
}

function setBusy(busy) {
  $("go").disabled = busy;
  document.querySelectorAll(".picker select, .stepper button").forEach((b) => (b.disabled = busy));
}

function stopRace(clear = true) {
  clearInterval(state.poll);
  if (clear) { cancelAnimationFrame(state.raf); state.raf = null; }
  setBusy(false);
  updateContenders();
}

async function startRace() {
  setBusy(true);
  $("banner").hidden = true;
  buildTrack();
  $("status-line").textContent = "Starting…";
  try {
    const resp = await getJSON("/api/race", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        dashboard_id: state.detail.id,
        reyden_warehouse_id: state.reyId,
        runs: state.runs,
      }),
    });
    state.race = resp.race_id;
    state.poll = setInterval(poll, 500);
    if (!state.raf) animate();
  } catch (e) {
    $("status-line").textContent = `Could not start: ${e.message}`;
    stopRace();
  }
}

/* ---------- init ---------- */

async function pickDashboard(id) {
  state.detail = null;
  updateContenders();
  $("dash-links").innerHTML = "";
  $("banner").hidden = true;
  $("kpis").hidden = true;
  $("status-line").textContent = "Loading dashboard datasets…";
  try {
    state.detail = await getJSON(`/api/dashboards/${encodeURIComponent(id)}`);
    const a = document.createElement("a");
    a.href = state.detail.url; a.target = "_blank";
    a.textContent = `${state.detail.name} ↗`;
    $("dash-links").appendChild(a);
    $("status-line").textContent = state.detail.scenarios.length
      ? `${state.detail.scenarios.length} dataset queries ready — hit start to race them.`
      : "This dashboard has no dataset queries to race — pick another one.";
  } catch (e) {
    $("status-line").textContent = `Could not load dashboard: ${e.message}`;
  }
  buildTrack();
  updateContenders();
}

async function init() {
  $("runs-dec").onclick = () => { state.runs = Math.max(1, state.runs - 1); $("runs-val").textContent = state.runs; };
  $("runs-inc").onclick = () => { state.runs = Math.min(3, state.runs + 1); $("runs-val").textContent = state.runs; };
  $("go").onclick = startRace;

  // Load the two dropdowns independently so one failure doesn't blank the other.
  const [dl, wl] = await Promise.allSettled([getJSON("/api/dashboards"), getJSON("/api/warehouses")]);

  const ds = $("dash-select");
  ds.innerHTML = "";
  if (dl.status === "fulfilled") {
    state.dashboards = dl.value.dashboards;
    ds.appendChild(new Option("— pick a dashboard —", ""));
    for (const d of state.dashboards) ds.appendChild(new Option(d.name, d.id));
    ds.onchange = () => { if (ds.value) pickDashboard(ds.value); };
  } else ds.appendChild(new Option("failed to load dashboards", ""));

  const rs = $("rey-select");
  rs.innerHTML = "";
  if (wl.status === "fulfilled") {
    state.reyden = wl.value.reyden;
    if (!state.reyden.length) rs.appendChild(new Option("no Reyden warehouses available", ""));
    for (const w of state.reyden) rs.appendChild(new Option(`${w.name} (${w.size || "?"})`, w.id));
    state.reyId = state.reyden.length ? state.reyden[0].id : null;
    rs.onchange = () => { state.reyId = rs.value || null; updateContenders(); };
  } else rs.appendChild(new Option("failed to load warehouses", ""));

  $("status-line").textContent =
    wl.status === "rejected" ? `Warehouses: ${wl.reason.message}`
    : dl.status === "rejected" ? `Dashboards: ${dl.reason.message}`
    : !state.reyden.length ? "No Reyden warehouses are visible to you — ask an admin for CAN USE on one."
    : !state.dashboards.length ? "No dashboards found — share a dashboard with the app's service principal, then reload."
    : `${state.dashboards.length} dashboards${dl.value.user ? ` visible to ${dl.value.user}` : ""} — pick one to race.`;
  updateContenders();
}

init();
