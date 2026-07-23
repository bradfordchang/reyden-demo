/* Reyden Batch Profiler — frontend */
"use strict";

const $ = (id) => document.getElementById(id);
const fmtMs = (ms) => ms == null ? "–" : ms >= 10000 ? (ms / 1000).toFixed(1) + "s" : Math.round(ms).toLocaleString() + "ms";
const fmtS = (ms) => ms == null ? "–" : (ms / 1000).toFixed(1) + "s";
const geomean = (a) => Math.exp(a.reduce((t, x) => t + Math.log(x), 0) / a.length);
const esc = (s) => { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; };
const escAttr = (s) => esc(s).replace(/"/g, "&quot;");

const LANES = ["reyden", "baseline"];

const state = {
  reyden: [], reyId: null,
  dashboards: [],            // full catalog from /api/dashboards
  selected: new Set(),       // dashboard ids picked for the next batch
  filter: "",
  rowById: {},               // dashboard id -> result-row index (current batch only)
  maxBatch: 25,
  runs: 1,
  profile: null, poll: null, raf: null, lastSnap: null, lastSnapAt: 0,
  pollFails: 0,
};

async function getJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

/* ---------- selection picker ---------- */

function whMeta(wh, suffix) {
  if (!wh) return null;
  return [wh.size, wh.serverless ? "serverless" : null, suffix].filter(Boolean).join(" · ");
}

function filteredDashboards() {
  const q = state.filter.trim().toLowerCase();
  return q ? state.dashboards.filter((d) => d.name.toLowerCase().includes(q)) : state.dashboards;
}

function renderPicker() {
  const list = $("picker-list");
  list.innerHTML = "";
  const shown = filteredDashboards();
  for (const d of shown) {
    const row = document.createElement("label");
    row.className = "pick-row";
    row.innerHTML = `
      <input type="checkbox" ${state.selected.has(d.id) ? "checked" : ""}>
      <span class="pick-name">${esc(d.name)}</span>
      <span class="pick-date">${d.updated ? new Date(d.updated).toLocaleDateString() : ""}</span>`;
    row.querySelector("input").onchange = (e) => {
      if (e.target.checked) state.selected.add(d.id); else state.selected.delete(d.id);
      updateSelection();
    };
    list.appendChild(row);
  }
  if (!shown.length) {
    const empty = document.createElement("div");
    empty.className = "pick-empty";
    empty.textContent = state.dashboards.length ? "No dashboards match the filter." : "No dashboards found.";
    list.appendChild(empty);
  }
  updateSelection();
}

function updateSelection() {
  const n = state.selected.size;
  const rey = state.reyden.find((w) => w.id === state.reyId);
  $("rey-name").textContent = rey ? rey.name : "Reyden";
  $("rey-meta").textContent = rey ? whMeta(rey, "Reyden") : "pick a warehouse";
  $("dash-count").textContent = state.dashboards.length
    ? `${n} of ${state.dashboards.length} dashboards selected` : "no dashboards found";
  $("picker-count").textContent = state.dashboards.length
    ? `${n} selected · max ${state.maxBatch} per batch` : "";

  const shown = filteredDashboards();
  const all = $("sel-all");
  const shownSelected = shown.filter((d) => state.selected.has(d.id)).length;
  all.checked = shown.length > 0 && shownSelected === shown.length;
  all.indeterminate = shownSelected > 0 && shownSelected < shown.length;

  const over = n > state.maxBatch;
  const line = $("status-line");
  if (over) line.textContent = `Batches are limited to ${state.maxBatch} dashboards — deselect ${n - state.maxBatch}.`;
  else if (line.textContent.startsWith("Batches are limited")) {
    line.textContent = `${n} dashboard${n === 1 ? "" : "s"} selected — ready to validate & profile.`;
  }
  $("go").innerHTML = n
    ? `▶&nbsp; Validate &amp; profile ${n} dashboard${n === 1 ? "" : "s"}`
    : "▶&nbsp; Validate &amp; profile";
  $("go").disabled = !(n && !over && state.reyId);
}

function showPicker() {
  $("picker-card").hidden = false;
  $("track").hidden = true;
  $("kpis").hidden = true;
  $("banner").hidden = true;
  $("edit-sel").hidden = true;
  renderPicker();
}

/* ---------- results track (selected dashboards only) ---------- */

function chipHTML(text, cls, title) {
  return `<span class="chip ${cls || ""}" title="${escAttr(title || "")}">${esc(text)}</span>`;
}

function buildTrack(picked) {
  const track = $("track");
  track.innerHTML = "";
  state.rowById = {};
  const h = document.createElement("div");
  h.className = "page-header";
  h.textContent = `this batch — ${picked.length} dashboard${picked.length === 1 ? "" : "s"}, validated first, then profiled`;
  track.appendChild(h);
  picked.forEach((d, i) => {
    state.rowById[d.id] = i;
    const row = document.createElement("div");
    row.className = "row dash-row";
    row.id = `row-${i}`;
    row.innerHTML = `
      <div class="label">${esc(d.name)}<small id="meta-${i}"></small></div>
      <div class="lanes">
        <div class="lane reyden"><span class="who">REY</span>
          <div class="bar-wrap"><div class="bar" id="bar-${i}-reyden"></div></div>
          <span class="ms" id="ms-${i}-reyden">–</span></div>
        <div class="lane baseline"><span class="who">BASE</span>
          <div class="bar-wrap"><div class="bar" id="bar-${i}-baseline"></div></div>
          <span class="ms" id="ms-${i}-baseline">–</span></div>
      </div>
      <div class="verdict" id="v-${i}"><span class="chip">queued</span></div>`;
    track.appendChild(row);
  });
  track.hidden = false;
}

function render(snap, extrapolate = 0) {
  // One scale across the whole batch so bar lengths are comparable between
  // dashboards; it tracks the longest load time seen (including in-flight).
  let trackMax = 1;
  for (const d of snap.dashboards) {
    for (const lane of LANES) {
      const med = d.summary && d.summary.load_ms ? d.summary.load_ms[lane] : null;
      if (med) trackMax = Math.max(trackMax, med);
      if (d.status === "profiling" && d.lanes) {
        for (const c of d.lanes[lane].inflight || []) {
          trackMax = Math.max(trackMax, c.elapsed_ms + extrapolate);
        }
      }
    }
  }

  for (const d of snap.dashboards) {
    const i = state.rowById[d.id];
    if (i == null) continue;
    const row = $(`row-${i}`), meta = $(`meta-${i}`), v = $(`v-${i}`);
    const s = d.summary;

    // meta line: baseline warehouse + dataset validation outcome (or skip reason)
    if (d.status === "skipped") {
      meta.textContent = d.reason || "skipped";
      meta.title = d.reason || "";
    } else if (d.baseline) {
      const total = d.datasets ? d.datasets.length : null;
      const ok = (d.scenario_ids || []).length;
      const bits = [`vs ${d.baseline.name}`];
      if (total != null) bits.push(`${ok}/${total} datasets validated`);
      if (s && s.pair_count) bits.push(`${s.reyden_wins}/${s.pair_count} dataset wins`);
      meta.textContent = bits.join(" · ");
      const bad = (d.datasets || []).filter((x) => x.error);
      meta.title = bad.map((x) => `${x.label}: ${x.error}`).join("\n");
    }

    // verdict / status chip — the ratio shown is the *load-time* ratio, so it
    // always matches the two times rendered next to it.
    const ratio = s ? (s.load_speedup || s.geomean_speedup) : null;
    if (d.status === "done" && ratio) {
      v.innerHTML = `<span class="flash">${ratio.toFixed(1)}×</span><small>${ratio >= 1 ? "REYDEN FASTER" : "BASELINE FASTER"}</small>`;
      row.classList.toggle("winner-rey", ratio > 1);
    } else if (d.status === "done") {
      v.innerHTML = chipHTML("done", "ok");
    } else if (d.status === "pending") {
      v.innerHTML = chipHTML("queued", "");
    } else if (d.status === "validating") {
      v.innerHTML = chipHTML("validating…", "warn");
    } else if (d.status === "ready") {
      v.innerHTML = chipHTML("validated ✓", "ok");
    } else if (d.status === "skipped") {
      v.innerHTML = chipHTML("skipped", "warn", d.reason);
    } else if (d.status === "error") {
      v.innerHTML = chipHTML("failed", "err", d.reason);
    } else if (d.status === "profiling") {
      v.innerHTML = chipHTML(`racing ${d.queries_done}/${d.queries_total}`, "live");
    }

    // lanes
    for (const lane of LANES) {
      const bar = $(`bar-${i}-${lane}`), ms = $(`ms-${i}-${lane}`);
      const st = d.lanes ? d.lanes[lane] : null;
      const med = s && s.load_ms ? s.load_ms[lane] : null;
      let live = null;
      if (d.status === "profiling" && st && st.inflight && st.inflight.length) {
        live = Math.max(...st.inflight.map((c) => c.elapsed_ms)) + extrapolate;
      }
      if (d.status === "profiling" && st && !st.ready && !st.done) {
        bar.style.width = "0%"; bar.classList.add("running");
        ms.textContent = "warming…"; ms.className = "ms live";
      } else if (live != null) {
        bar.style.width = `${Math.min(98, (live / trackMax) * 100)}%`;
        bar.classList.add("running");
        ms.textContent = fmtMs(live); ms.className = "ms live";
      } else if (med != null) {
        bar.style.width = `${(med / trackMax) * 100}%`;
        bar.classList.remove("running");
        ms.textContent = fmtMs(med); ms.className = "ms";
      } else if (st && st.error) {
        bar.style.width = "100%"; bar.classList.remove("running");
        ms.textContent = "error"; ms.className = "ms err"; ms.title = st.error;
      }
    }
  }

  // KPIs across the batch — headline speedup is load-based (end-to-end),
  // dataset wins are counted per query pair.
  const loadRatios = [], totals = { reyden: 0, baseline: 0 };
  let wins = 0, pairs = 0, qDone = 0, qTotal = 0;
  for (const d of snap.dashboards) {
    qDone += d.queries_done || 0; qTotal += d.queries_total || 0;
    const s = d.summary;
    if (!s) continue;
    if (s.load_speedup) loadRatios.push(s.load_speedup);
    wins += s.reyden_wins || 0;
    pairs += s.pair_count || 0;
    totals.reyden += s.total_ms.reyden || 0;
    totals.baseline += s.total_ms.baseline || 0;
  }
  $("kpis").hidden = false;
  $("k-speedup").textContent = loadRatios.length ? geomean(loadRatios).toFixed(1) + "×" : "–";
  $("k-wins").textContent = pairs ? `${wins}/${pairs}` : "–";
  $("k-progress").textContent = qTotal ? `${qDone}/${qTotal}` : "–";
  const tMax = Math.max(totals.reyden, totals.baseline, 1);
  $("t-rey").style.width = `${(totals.reyden / tMax) * 100}%`;
  $("t-base").style.width = `${(totals.baseline / tMax) * 100}%`;
  $("tv-rey").textContent = fmtS(totals.reyden || null);
  $("tv-base").textContent = fmtS(totals.baseline || null);

  // status line + banner
  const line = $("status-line");
  if (snap.status === "validating") {
    const checked = snap.dashboards.filter((d) => !["pending", "validating"].includes(d.status)).length;
    line.textContent = `Validating permissions (${checked}/${snap.dashboards.length}) — warehouse access + DESCRIBE QUERY compile of every dataset query. No dashboard queries have run yet.`;
    line.className = "status-line warming";
  } else if (snap.status === "running") {
    const cur = snap.dashboards.find((d) => d.status === "profiling");
    const finished = snap.dashboards.filter((d) => ["done", "error"].includes(d.status)).length;
    const runnable = snap.dashboards.filter((d) => !["skipped"].includes(d.status)).length;
    line.textContent = cur
      ? `Profiling ${finished + 1}/${runnable}: “${cur.name}” — all its dataset queries in flight on both warehouses…`
      : `Profiling ${finished}/${runnable}…`;
    line.className = "status-line";
  } else if (snap.status === "failed") {
    line.textContent = `Batch failed: ${snap.error || "unknown error"}`;
    line.className = "status-line";
  } else if (snap.status === "done") {
    const s = snap.summary;
    const banner = $("banner");
    banner.hidden = false;
    const g = s && s.load_geomean_speedup;
    banner.innerHTML = g
      ? `🏁 <b>${esc(snap.reyden.name)} loaded ${s.dashboards_profiled} dashboard${s.dashboards_profiled === 1 ? "" : "s"}
         ${g >= 1 ? g.toFixed(1) + "× faster" : (1 / g).toFixed(1) + "× slower"}</b> (median end-to-end load) —
         winning ${s.reyden_wins} of ${s.pair_count} individual dataset queries` +
        (s.best && s.best.load_speedup ? `; best: “${esc(s.best.name)}” at ${s.best.load_speedup.toFixed(1)}×` : "") +
        (s.dashboards_skipped ? ` <small>(${s.dashboards_skipped} skipped in validation)</small>` : "") + "."
      : "Batch complete.";
    line.textContent = "Batch complete — edit the selection or run it again.";
    line.className = "status-line";
  }
}

/* ---------- profile loop ---------- */

async function poll() {
  if (!state.profile) return;
  try {
    const snap = await getJSON(`/api/profile/${state.profile}`);
    state.pollFails = 0;
    state.lastSnap = snap;
    state.lastSnapAt = performance.now();
    render(snap);
    if (snap.status === "done" || snap.status === "failed") stopProfile();
  } catch (e) {
    // Transient blips are fine, but a gone profile (app restarted, profile
    // evicted) would otherwise leave the controls locked forever.
    if (/No such profile/i.test(e.message) || ++state.pollFails >= 8) {
      $("status-line").textContent = `Lost contact with the batch (${e.message}) — reload the page to start over.`;
      stopProfile();
    }
  }
}

function animate() {
  if (state.lastSnap && state.lastSnap.status === "running") {
    render(state.lastSnap, performance.now() - state.lastSnapAt);
  }
  state.raf = requestAnimationFrame(animate);
}

function setBusy(busy) {
  $("go").disabled = busy;
  $("edit-sel").disabled = busy;
  document.querySelectorAll(".picker select, .stepper button, .pick-row input, #sel-all, #dash-filter")
    .forEach((b) => (b.disabled = busy));
}

function stopProfile() {
  clearInterval(state.poll);
  cancelAnimationFrame(state.raf);
  state.raf = null;
  setBusy(false);
  $("edit-sel").hidden = false;
  updateSelection();
}

async function startProfile() {
  const ids = state.dashboards.filter((d) => state.selected.has(d.id)).map((d) => d.id);
  if (!ids.length || !state.reyId) return;
  const picked = state.dashboards.filter((d) => state.selected.has(d.id));
  setBusy(true);
  $("picker-card").hidden = true;
  $("edit-sel").hidden = true;
  $("banner").hidden = true;
  $("kpis").hidden = true;
  buildTrack(picked);
  $("status-line").textContent = "Starting…";
  try {
    const resp = await getJSON("/api/profile", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        reyden_warehouse_id: state.reyId,
        dashboard_ids: ids,
        runs: state.runs,
      }),
    });
    state.profile = resp.profile_id;
    state.poll = setInterval(poll, 600);
    if (!state.raf) animate();
  } catch (e) {
    $("status-line").textContent = `Could not start: ${e.message}`;
    stopProfile();
    showPicker();
  }
}

/* ---------- init ---------- */

async function init() {
  $("runs-dec").onclick = () => { state.runs = Math.max(1, state.runs - 1); $("runs-val").textContent = state.runs; };
  $("runs-inc").onclick = () => { state.runs = Math.min(3, state.runs + 1); $("runs-val").textContent = state.runs; };
  $("go").onclick = startProfile;
  $("edit-sel").onclick = showPicker;
  $("dash-filter").oninput = () => { state.filter = $("dash-filter").value; renderPicker(); };
  $("sel-all").onchange = () => {
    const shown = filteredDashboards();
    if ($("sel-all").checked) shown.forEach((d) => state.selected.add(d.id));
    else shown.forEach((d) => state.selected.delete(d.id));
    renderPicker();
  };

  // Load the two lists independently so one failure doesn't blank the other.
  const [dl, wl] = await Promise.allSettled([getJSON("/api/dashboards"), getJSON("/api/warehouses")]);

  if (dl.status === "fulfilled") {
    state.dashboards = dl.value.dashboards;
    state.maxBatch = dl.value.max_batch || state.maxBatch;
    // Sensible default: everything when it fits in one batch, else start
    // empty and let the filter + "select all shown" do the picking.
    if (state.dashboards.length <= state.maxBatch) {
      state.dashboards.forEach((d) => state.selected.add(d.id));
    }
  }

  const rs = $("rey-select");
  rs.innerHTML = "";
  if (wl.status === "fulfilled") {
    state.reyden = wl.value.reyden;
    if (!state.reyden.length) rs.appendChild(new Option("no Reyden warehouses available", ""));
    for (const w of state.reyden) rs.appendChild(new Option(`${w.name} (${w.size || "?"})`, w.id));
    state.reyId = state.reyden.length ? state.reyden[0].id : null;
    rs.onchange = () => { state.reyId = rs.value || null; updateSelection(); };
  } else rs.appendChild(new Option("failed to load warehouses", ""));

  $("status-line").textContent =
    wl.status === "rejected" ? `Warehouses: ${wl.reason.message}`
    : dl.status === "rejected" ? `Dashboards: ${dl.reason.message}`
    : !state.reyden.length ? "No Reyden warehouses are visible to you — ask an admin for CAN USE on one."
    : !state.dashboards.length ? "No dashboards found — share a dashboard with the app's service principal, then reload."
    : state.dashboards.length > state.maxBatch
      ? `${state.dashboards.length} dashboards${dl.value.user ? ` visible to ${dl.value.user}` : ""} — filter and pick up to ${state.maxBatch} to profile.`
      : `${state.dashboards.length} dashboards${dl.value.user ? ` visible to ${dl.value.user}` : ""} — all selected; deselect any you don't want.`;
  showPicker();
}

init();
