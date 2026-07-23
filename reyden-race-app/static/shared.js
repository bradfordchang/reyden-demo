/* Shared UI helpers — loaded before app.js / race.js */
"use strict";

const $ = (id) => document.getElementById(id);
const fmtMs = (ms) => ms == null ? "–" : ms >= 10000 ? (ms / 1000).toFixed(1) + "s" : Math.round(ms).toLocaleString() + "ms";
const fmtS = (ms) => ms == null ? "–" : (ms / 1000).toFixed(1) + "s";
const median = (a) => { const s = [...a].sort((x, y) => x - y); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
const geomean = (a) => Math.exp(a.reduce((t, x) => t + Math.log(x), 0) / a.length);
const esc = (s) => { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; };
const escAttr = (s) => esc(s).replace(/"/g, "&quot;");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function getJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

// Saved picks (selection, warehouse, runs) survive reloads; the one storage
// key is shared between both pages. Storage can be unavailable — fail silently.
const STORE_KEY = "reyden-lab:v1";
const loadPrefs = () => { try { return JSON.parse(localStorage.getItem(STORE_KEY)) || {}; } catch { return {}; } };
const savePrefs = (patch) => { try { localStorage.setItem(STORE_KEY, JSON.stringify({ ...loadPrefs(), ...patch })); } catch { /* no-op */ } };

function whMeta(wh, suffix) {
  if (!wh) return null;
  return [wh.size, wh.serverless ? "serverless" : null, suffix].filter(Boolean).join(" · ");
}
