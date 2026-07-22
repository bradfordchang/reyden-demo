/* Reyden Query Race — frontend */
"use strict";

const $ = (id) => document.getElementById(id);
const fmtMs = (ms) => ms == null ? "–" : ms >= 10000 ? (ms / 1000).toFixed(1) + "s" : Math.round(ms).toLocaleString() + "ms";
const fmtS = (ms) => ms == null ? "–" : (ms / 1000).toFixed(1) + "s";
const median = (a) => { const s = [...a].sort((x, y) => x - y); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
const geomean = (a) => Math.exp(a.reduce((t, x) => t + Math.log(x), 0) / a.length);

const PRESETS = [
  { key: "quick", label: "Quick race" },
  { key: "all", label: "Full suite" },
  { key: "overview", label: "Overview" },
  { key: "trends", label: "Trends" },
  { key: "breakdowns", label: "Breakdowns" },
  { key: "filters", label: "Filters" },
];

const state = {
  scenarios: [], preset: "quick", runs: 1,
  race: null, poll: null, raf: null, lastSnap: null, lastSnapAt: 0,
};

function scenariosFor(preset) {
  if (preset === "all") return state.scenarios;
  if (preset === "quick") return state.scenarios.filter((s) => s.quick);
  return state.scenarios.filter((s) => s.page === preset);
}

/* ---------- rendering ---------- */

function buildTrack() {
  const track = $("track");
  track.innerHTML = "";
  let lastPage = null;
  for (const sc of scenariosFor(state.preset)) {
    if (sc.page !== lastPage) {
      const h = document.createElement("div");
      h.className = "page-header";
      h.textContent = sc.page === "filters" ? "Filter & parameter interactions" : `${sc.page} page`;
      track.appendChild(h);
      lastPage = sc.page;
    }
    const row = document.createElement("div");
    row.className = "row";
    row.id = `row-${sc.id}`;
    row.innerHTML = `
      <div class="label">${sc.label}<small>${sc.id}</small></div>
      <div class="lanes">
        <div class="lane reyden"><span class="who">REY</span>
          <div class="bar-wrap"><div class="bar" id="bar-${sc.id}-reyden"></div></div>
          <span class="ms" id="ms-${sc.id}-reyden">–</span></div>
        <div class="lane starter"><span class="who">STR</span>
          <div class="bar-wrap"><div class="bar" id="bar-${sc.id}-starter"></div></div>
          <span class="ms" id="ms-${sc.id}-starter">–</span></div>
      </div>
      <div class="verdict" id="v-${sc.id}"></div>`;
    track.appendChild(row);
  }
}

function laneTimes(results, scId, lane) {
  return results.filter((r) => r.scenario_id === scId && r.lane === lane && !r.error).map((r) => r.wall_ms);
}

function render(snap, extrapolate = 0) {
  const scenarios = scenariosFor(state.preset);
  const results = snap.results;
  const inflight = {};
  for (const lane of ["reyden", "starter"]) {
    const cur = snap.lanes[lane].current;
    if (cur) inflight[`${cur.scenario_id}|${lane}`] = cur.elapsed_ms + extrapolate;
  }

  const ratios = [], totals = { reyden: 0, starter: 0 };
  let wins = 0, pairs = 0;

  for (const sc of scenarios) {
    const rey = laneTimes(results, sc.id, "reyden");
    const st = laneTimes(results, sc.id, "starter");
    const reyMed = rey.length ? median(rey) : null;
    const stMed = st.length ? median(st) : null;
    const reyLive = inflight[`${sc.id}|reyden`];
    const stLive = inflight[`${sc.id}|starter`];
    const rowMax = Math.max(reyMed || 0, stMed || 0, reyLive || 0, stLive || 0, 1);

    for (const [lane, med, live] of [["reyden", reyMed, reyLive], ["starter", stMed, stLive]]) {
      const bar = $(`bar-${sc.id}-${lane}`), ms = $(`ms-${sc.id}-${lane}`);
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

    const v = $(`v-${sc.id}`);
    if (reyMed && stMed) {
      const ratio = stMed / reyMed;
      ratios.push(ratio); pairs++;
      if (ratio > 1) wins++;
      v.innerHTML = `<span class="flash">${ratio.toFixed(1)}×</span><small>${ratio >= 1 ? "REYDEN FASTER" : "STARTER FASTER"}</small>`;
      $(`row-${sc.id}`).classList.toggle("winner-rey", ratio > 1);
    } else v.innerHTML = "";
    totals.reyden += rey.reduce((a, b) => a + b, 0);
    totals.starter += st.reduce((a, b) => a + b, 0);
  }

  // KPIs
  $("kpis").hidden = false;
  $("k-speedup").textContent = ratios.length ? geomean(ratios).toFixed(1) + "×" : "–";
  $("k-wins").textContent = pairs ? `${wins}/${pairs}` : "–";
  const done = results.filter((r) => !r.error).length + results.filter((r) => r.error).length;
  const totalQ = snap.scenario_ids.length * snap.runs * 2;
  $("k-progress").textContent = `${done}/${totalQ}`;
  const tMax = Math.max(totals.reyden, totals.starter, 1);
  $("t-rey").style.width = `${(totals.reyden / tMax) * 100}%`;
  $("t-st").style.width = `${(totals.starter / tMax) * 100}%`;
  $("tv-rey").textContent = fmtS(totals.reyden || null);
  $("tv-st").textContent = fmtS(totals.starter || null);

  // status
  const line = $("status-line");
  if (snap.status === "running") {
    const ready = Object.values(snap.lanes).every((l) => l.ready);
    line.textContent = ready ? "Racing — identical queries in flight on both warehouses…" : "Warming up both warehouses (excluded from timings)…";
    line.className = ready ? "status-line" : "status-line warming";
  }

  // finished
  if (snap.status === "done" && snap.summary) {
    const s = snap.summary;
    const banner = $("banner");
    banner.hidden = false;
    banner.innerHTML = s.geomean_speedup
      ? `🏁 <b>Reyden ran the same dashboard ${s.geomean_speedup.toFixed(1)}× faster</b> —
         winning ${s.reyden_wins} of ${s.scenario_count} scenarios (range ${s.min_speedup.toFixed(1)}×–${s.max_speedup.toFixed(1)}×),
         on a <b>Small</b> vs an <b>X-Large Pro</b>.`
      : "Race complete.";
    line.textContent = "Race complete — run it again or try another preset.";
    line.className = "status-line";
    const laneErr = Object.entries(snap.lanes).find(([, l]) => l.error);
    if (laneErr) line.textContent = `Lane ${laneErr[0]} failed: ${laneErr[1].error}`;
  }
}

/* ---------- race loop ---------- */

async function poll() {
  if (!state.race) return;
  try {
    const snap = await (await fetch(`/api/race/${state.race}`)).json();
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

function stopRace(clear = true) {
  clearInterval(state.poll);
  if (clear) { cancelAnimationFrame(state.raf); state.raf = null; }
  $("go").disabled = false;
  document.querySelectorAll(".seg button, .stepper button").forEach((b) => (b.disabled = false));
}

async function startRace() {
  const ids = scenariosFor(state.preset).map((s) => s.id);
  $("go").disabled = true;
  document.querySelectorAll(".seg button, .stepper button").forEach((b) => (b.disabled = true));
  $("banner").hidden = true;
  buildTrack();
  $("status-line").textContent = "Starting…";
  try {
    const resp = await fetch("/api/race", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scenario_ids: ids, runs: state.runs }),
    });
    if (!resp.ok) throw new Error((await resp.json()).detail || resp.statusText);
    state.race = (await resp.json()).race_id;
    state.poll = setInterval(poll, 500);
    if (!state.raf) animate();
  } catch (e) {
    $("status-line").textContent = `Could not start: ${e.message}`;
    stopRace();
  }
}

/* ---------- init ---------- */

async function init() {
  const cfg = await (await fetch("/api/config")).json();
  state.scenarios = cfg.scenarios;
  $("rey-meta").textContent = `${cfg.warehouses.reyden.size} · serverless`;
  $("st-meta").textContent = `${cfg.warehouses.starter.size} · serverless`;
  const links = $("dash-links");
  for (const [lane, url] of Object.entries(cfg.dashboards || {})) {
    if (!url) continue;
    const a = document.createElement("a");
    a.href = url; a.target = "_blank";
    a.textContent = `${cfg.warehouses[lane].label} dashboard ↗`;
    links.appendChild(a);
  }

  const seg = $("preset-seg");
  for (const p of PRESETS) {
    const b = document.createElement("button");
    b.textContent = p.label;
    b.className = p.key === state.preset ? "active" : "";
    b.onclick = () => {
      state.preset = p.key;
      seg.querySelectorAll("button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      buildTrack();
    };
    seg.appendChild(b);
  }
  $("runs-dec").onclick = () => { state.runs = Math.max(1, state.runs - 1); $("runs-val").textContent = state.runs; };
  $("runs-inc").onclick = () => { state.runs = Math.min(3, state.runs + 1); $("runs-val").textContent = state.runs; };
  $("go").onclick = startRace;
  buildTrack();
}

init();
