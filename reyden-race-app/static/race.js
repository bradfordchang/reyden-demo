/* Reyden Query Race — frontend */
"use strict";

// $ / fmtMs / median / esc / getJSON / prefs helpers come from shared.js;
// errClass / errMsg / ERROR_HINTS / explainError come from errors.js.

const state = {
  dashboards: [], detail: null,       // detail: /api/dashboards/{id} response
  reyden: [], reyId: null,
  runs: 1,
  race: null, poll: null, raf: null, lastSnap: null, lastSnapAt: 0,
  pollFails: 0,
};

const scenarios = () => (state.detail ? state.detail.scenarios : []);

/* ---------- rendering ---------- */

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
      <div class="verdict" id="v-${i}"></div>
      <div class="err-detail" id="err-${i}" hidden></div>`;
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

  // All rows share one scale so bar lengths are comparable across datasets;
  // it tracks the longest time seen (including in-flight), so every bar
  // rescales as the leader grows.
  const rows = scenarios().map((sc) => {
    const rey = laneTimes(results, sc.id, "reyden");
    const base = laneTimes(results, sc.id, "baseline");
    return {
      rey, base,
      reyMed: rey.length ? median(rey) : null,
      baseMed: base.length ? median(base) : null,
      reyLive: inflight[`${sc.id}|reyden`],
      baseLive: inflight[`${sc.id}|baseline`],
    };
  });
  const trackMax = Math.max(1, ...rows.flatMap((r) => [r.reyMed || 0, r.baseMed || 0, r.reyLive || 0, r.baseLive || 0]));

  scenarios().forEach((sc, i) => {
    const { rey, base, reyMed, baseMed, reyLive, baseLive } = rows[i];

    for (const [lane, med, live] of [["reyden", reyMed, reyLive], ["baseline", baseMed, baseLive]]) {
      const bar = $(`bar-${i}-${lane}`), ms = $(`ms-${i}-${lane}`);
      if (!bar) continue;
      const err = results.find((r) => r.scenario_id === sc.id && r.lane === lane && r.error);
      if (live != null) {
        bar.style.width = `${Math.min(98, (live / trackMax) * 100)}%`;
        bar.classList.add("running");
        ms.textContent = fmtMs(live);
        ms.className = "ms live";
      } else if (med != null) {
        bar.style.width = `${(med / trackMax) * 100}%`;
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

    const errRow = $(`err-${i}`);
    if (errRow) {
      const reyErr = results.find((r) => r.scenario_id === sc.id && r.lane === "reyden" && r.error);
      const baseErr = results.find((r) => r.scenario_id === sc.id && r.lane === "baseline" && r.error);
      if (reyErr || baseErr) {
        const lines = [["REY", reyErr], ["BASE", baseErr]].filter(([, e]) => e).map(([who, e]) => {
          const cls = errClass(e.error) || e.error_code;
          return `<div class="err-line"><span class="who">${who}</span>${cls ? `<code>${esc(cls)}</code>` : ""}<span>${esc(errMsg(e.error))}</span></div>`;
        }).join("");
        const html = lines + `<div class="err-why">${esc(explainError(reyErr, baseErr))}</div>`;
        if (errRow.dataset.sig !== html) { errRow.innerHTML = html; errRow.dataset.sig = html; }
        errRow.hidden = false;
      } else errRow.hidden = true;
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

  // KPIs — once the race is done the headline is the end-to-end load ratio
  // (it matches the seconds shown next to it); while running it tracks the
  // live per-dataset ratios, which include queueing and can run far higher.
  $("kpis").hidden = false;
  const sum = snap.status === "done" ? snap.summary : null;
  const sub = $("k-speedup-sub");
  if (sum && sum.load_speedup) {
    $("k-speedup").textContent = sum.load_speedup.toFixed(1) + "×";
    $("k-speedup-label").textContent = "load speedup";
    sub.textContent = `${fmtS(sum.load_ms.reyden)} vs ${fmtS(sum.load_ms.baseline)} end-to-end`;
    sub.hidden = false;
  } else {
    $("k-speedup").textContent = ratios.length ? geomean(ratios).toFixed(1) + "×" : "–";
    $("k-speedup-label").textContent = snap.status === "running" ? "per-dataset ratio (live)" : "per-dataset geomean";
    sub.hidden = true;
  }
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
  } else if (snap.status === "failed") {
    line.textContent = `Race failed: ${snap.error || "unknown error"}`;
    line.className = "status-line";
    freezeBars();
  }

  // finished — the banner leads with the end-to-end load verdict (the number
  // that matches the two wall-clock times); per-dataset wins/geomean follow.
  if (snap.status === "done" && snap.summary) {
    const s = snap.summary;
    const rey = snap.warehouses.reyden, base = snap.warehouses.baseline;
    const banner = $("banner");
    banner.hidden = false;
    const ls = s.load_speedup;
    const perDs = s.pair_count
      ? ` Per dataset: ${s.reyden_wins} of ${s.pair_count} wins, geomean ${s.geomean_speedup.toFixed(1)}×
         (range ${s.min_speedup.toFixed(1)}×–${s.max_speedup.toFixed(1)}×).`
      : "";
    banner.innerHTML = ls
      ? `🏁 <b>${esc(rey.name)} loaded “${esc(snap.dashboard.name)}”
         ${ls >= 1 ? ls.toFixed(1) + "× faster" : (1 / ls).toFixed(1) + "× slower"}</b> —
         end-to-end with all queries at once, <b>${fmtS(s.load_ms.reyden)}</b> vs <b>${fmtS(s.load_ms.baseline)}</b>,
         on a <b>${esc(rey.size || "?")}</b> Reyden vs the dashboard's <b>${esc(base.size || "?")}</b> ${esc(base.name)}.` + perDs
      : perDs
        ? `🏁 <b>Race complete</b> — no full load comparison available.` + perDs
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
    state.pollFails = 0;
    state.lastSnap = snap;
    state.lastSnapAt = performance.now();
    render(snap);
    if (snap.status === "done" || snap.status === "failed") stopRace(false);
  } catch (e) {
    // Transient blips are fine, but a gone race (app restarted, race
    // evicted) would otherwise leave the controls locked forever.
    if (/No such race/i.test(e.message) || ++state.pollFails >= 8) {
      $("status-line").textContent = `Lost contact with the race (${e.message}) — reload the page to start over.`;
      freezeBars();
      stopRace();
    }
  }
}

// Strip the in-flight shimmer when a race ends without finishing cleanly, so
// abandoned bars stop animating.
function freezeBars() {
  document.querySelectorAll("#track .bar.running").forEach((b) => b.classList.remove("running"));
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
    state.pollFails = 0;
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
  $("runs-dec").onclick = () => { state.runs = Math.max(1, state.runs - 1); $("runs-val").textContent = state.runs; savePrefs({ runs: state.runs }); };
  $("runs-inc").onclick = () => { state.runs = Math.min(3, state.runs + 1); $("runs-val").textContent = state.runs; savePrefs({ runs: state.runs }); };
  $("go").onclick = startRace;

  // Restore last visit's picks from the key shared with the batch-profiler
  // page; anything stale falls back silently to the defaults.
  const saved = loadPrefs();
  if (Number.isFinite(saved.runs)) {
    state.runs = Math.min(3, Math.max(1, Math.round(saved.runs)));
    $("runs-val").textContent = state.runs;
  }

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
    if (saved.reyId && state.reyden.some((w) => w.id === saved.reyId)) {
      state.reyId = saved.reyId;
      rs.value = saved.reyId;
    }
    rs.onchange = () => { state.reyId = rs.value || null; savePrefs({ reyId: state.reyId }); updateContenders(); };
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
