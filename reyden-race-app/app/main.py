"""Reyden Query Lab — Databricks App backend with two modes behind one nav.

* `/` — Batch Profiler: profiles every AI/BI (Lakeview) dashboard the
  signed-in user has permission to run. Each dashboard's dataset queries
  race head-to-head — a user-picked Reyden warehouse vs the warehouse the
  dashboard is configured to run on — one dashboard at a time, with an
  aggregate scoreboard.
* `/race` — Single Race: the original mode; pick one dashboard and race its
  dataset queries live on the two warehouses.

Both modes share the helpers below and a single "one thing running at a
time" slot, since they compete for the same warehouses.

In the Batch Profiler, no dataset query executes before a validation phase
proves the user may run it:

* Warehouses — the Reyden pick and each dashboard's own warehouse must be
  visible to the user, and both must accept a statement from them
  (submitting a statement requires CAN USE; the probes read no table data).
* Data — every dataset query is compiled with DESCRIBE QUERY on the
  dashboard's warehouse. Analysis resolves each table/view and enforces
  Unity Catalog privileges, so a missing SELECT grant (or a dropped table)
  blocks that dataset up front, before profiling starts.

Dashboards that fail validation are skipped (reason shown in the UI);
datasets that fail are excluded from their dashboard's profile. Profiling
starts only after every dashboard in the batch has been validated.

Auth is on-behalf-of-user for warehouses, validation, and the profiled
queries themselves: those calls use the token Databricks Apps forwards in
`x-forwarded-access-token` (enable user authorization on the app with the
`sql` scope), so results reflect the user's own permissions and the app's
service principal needs no data or warehouse access.

Interim exception — Lakeview reads: Apps user authorization has no Lakeview
scope yet, so dashboard listing/reading falls back to the app's service
principal, which sees only dashboards shared with the app. Once an account
admin adds `dashboards.lakeview` to the app's OAuth integration (see README),
the user token starts working and the fallback goes dormant.

Locally there is no forwarded token, so the default SDK auth chain is used
(DATABRICKS_CONFIG_PROFILE) — you are both the user and the "SP".
"""
import hashlib
import json
import os
import re
import statistics
import threading
from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import time
import uuid
from pathlib import Path

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import Disposition, Format, StatementState
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

APP_DIR = Path(__file__).parent
STATIC_DIR = APP_DIR.parent / "static"

# reyden = the Reyden warehouse the user picked; baseline = each dashboard's own warehouse
LANES = ("reyden", "baseline")
MAX_DASHBOARDS = 25  # keeps one batch to a sane runtime
EXPLAIN_POOL = 6     # concurrent validation compiles per dashboard

app = FastAPI(title="Reyden Query Lab")
_local = None
_local_lock = threading.Lock()

# STATE_LOCK guards both registries plus every mutable results/inflight dict.
PROFILES: dict[str, dict] = {}  # batch profiles ("/" page)
RACES: dict[str, dict] = {}     # single-dashboard races ("/race" page)
STATE_LOCK = threading.Lock()


def _claim_slot(entry: dict, registry: dict):
    """Register `entry` iff no race or batch is active anywhere — atomically.

    Both modes share the same warehouses, so timings are only meaningful with
    one thing running at a time. Evicts only finished entries, never a live one.
    """
    with STATE_LOCK:
        if any(p["status"] in ("validating", "running") for p in PROFILES.values()) \
                or any(r["status"] == "running" for r in RACES.values()):
            raise HTTPException(409, "Another race or batch profile is already running — "
                                     "wait for it to finish.")
        registry[entry["id"]] = entry
        finished = [k for k in sorted(registry, key=lambda k: registry[k]["created"])
                    if registry[k]["status"] in ("done", "failed")]
        for k in finished[:-4]:
            del registry[k]


def _sp_client() -> WorkspaceClient:
    """The app's own identity: the SP (M2M env) in Apps, your CLI profile locally."""
    global _local
    with _local_lock:
        if _local is None:
            _local = WorkspaceClient()
    return _local


def client_for(request: Request) -> WorkspaceClient:
    """Client acting as the signed-in user (OBO in Apps, CLI profile locally)."""
    token = request.headers.get("x-forwarded-access-token")
    if token:
        return WorkspaceClient(host=os.environ["DATABRICKS_HOST"], token=token,
                               auth_type="pat")
    if os.environ.get("DATABRICKS_CLIENT_ID"):
        # Deployed, but no forwarded user token: user authorization is off.
        # The SP deliberately has no warehouse/data permissions, so stop here.
        raise HTTPException(401, "User authorization is not enabled for this app. "
                                 "Add the 'sql' user API scope in the app's Authorization settings.")
    return _sp_client()


def _get(w: WorkspaceClient, path: str, **query) -> dict:
    return w.api_client.do("GET", path,
                           query={k: v for k, v in query.items() if v is not None}) or {}


# Per-user-token memo of tokens already proven to lack the Lakeview scope, so
# a scope-less token skips its guaranteed-failing user attempt and goes
# straight to the SP. Entries are short, non-reversible fingerprints (never the
# token itself). Deliberately PER-TOKEN, not global: this preserves the
# self-disabling behavior — once an account admin adds the scope and the user
# re-logs-in, they get a NEW token whose fingerprint isn't here, so it
# re-probes and (now succeeding) never falls back again. Capped so a long-lived
# process can't grow it without bound.
_SCOPELESS_TOKENS: set[str] = set()
_SCOPELESS_CAP = 500


def _token_fp(token: str) -> str:
    """Short, non-reversible fingerprint of a user token for the scope memo."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _lakeview_get(w: WorkspaceClient, path: str, **query) -> dict:
    """Lakeview REST as the user, falling back to the app SP.

    Apps user authorization offers no Lakeview scope yet, so the forwarded
    user token can't call /api/2.0/lakeview/*. Fall back to the app's SP,
    which sees only dashboards shared with the app. Once an account admin
    adds `dashboards.lakeview` to the app's OAuth integration (see README),
    the user-token call succeeds and this fallback stops triggering.
    Scope-error text varies: "Invalid scope, required scopes: dashboards" /
    "Provided OAuth token does not have required scopes: dashboards".

    A user token that has already demonstrated it lacks the scope is memoized
    (by fingerprint) and thereafter skips straight to the SP — saving one
    guaranteed-failing round-trip per Lakeview call. The memo only decides
    whether to *attempt* the user token; the SP fallback and every permission
    check are unchanged, and any non-scope error still propagates untouched
    (and is not memoized). Locally there is one client (the SP) and no
    forwarded token, so the memo path is skipped entirely — behavior is
    identical to a plain user-then-SP try.
    """
    sp = _sp_client()
    # Only a distinct user client carrying a token participates in the memo.
    token = None if w is sp else getattr(w.config, "token", None)
    fp = _token_fp(token) if token else None

    if fp is not None and fp in _SCOPELESS_TOKENS:
        return _get(sp, path, **query)

    try:
        return _get(w, path, **query)
    except Exception as e:
        msg = str(e)
        if w is not sp and "scope" in msg.lower() and "dashboards" in msg:
            if fp is not None:
                if len(_SCOPELESS_TOKENS) >= _SCOPELESS_CAP:
                    _SCOPELESS_TOKENS.clear()
                _SCOPELESS_TOKENS.add(fp)
            return _get(sp, path, **query)
        raise


def _wh(raw: dict) -> dict:
    return {"id": raw.get("id"), "name": raw.get("name"), "size": raw.get("cluster_size"),
            "type": raw.get("warehouse_type"),
            "serverless": raw.get("enable_serverless_compute"), "state": raw.get("state")}


def check_warehouse(w: WorkspaceClient, wh_id: str) -> tuple[dict | None, str | None]:
    """(warehouse, None) if the user can see the warehouse, else (None, reason).

    Visibility means at least CAN VIEW; the ability to *run* on it (CAN USE)
    is proven later by the EXPLAIN probes, which submit real statements.
    Uses raw REST because the SDK enum drops warehouse_type REYDEN.
    """
    try:
        return _wh(_get(w, f"/api/2.0/sql/warehouses/{wh_id}")), None
    except Exception as e:
        return None, f"no access to warehouse {wh_id}: {str(e)[:200]}"


def warehouse_info(w: WorkspaceClient, wh_id: str) -> dict:
    """check_warehouse for display purposes: falls back to a stub on failure."""
    wh, _ = check_warehouse(w, wh_id)
    return wh or {"id": wh_id, "name": f"warehouse {wh_id[:8]}…", "size": None,
                  "type": None, "serverless": None, "state": None}


# Lakeview stores dynamic date defaults as date-math: "now", an optional
# signed offset and an optional unit to round to ("now-90d/d", "now-1h/h").
_DATE_MATH = re.compile(r"now(?:([+-]\d+)([smhdwMy]))?(?:/([smhdwMy]))?")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def _range_bound(bound: dict | None, data_type: str, end: bool) -> str:
    """A DATE/DATETIME default ({"value": …}) as an unquoted literal.

    Date-math resolves against now; the min bound rounds down to the start
    of the rounding unit and the max up to its end, matching how the UI
    stores absolute ranges (max lands on T23:59:59.999). Absolute defaults
    pass through unchanged; an absent default yields "" (caller skips it).
    Value (non-range) defaults resolve with end=False.
    """
    val = str((bound or {}).get("value", ""))
    m = _DATE_MATH.fullmatch(val)
    if not m:
        return val
    off, unit, rnd = m.groups()
    t = datetime.now()
    if off:
        n = int(off)
        if unit in ("M", "y"):
            months = t.year * 12 + t.month - 1 + n * (12 if unit == "y" else 1)
            y, mo = divmod(months, 12)
            t = t.replace(year=y, month=mo + 1, day=min(t.day, monthrange(y, mo + 1)[1]))
        else:
            t += timedelta(**{_UNITS[unit]: n})
    if rnd == "w":
        t += timedelta(days=6 - t.weekday() if end else -t.weekday())
    elif rnd == "M":
        t = t.replace(day=monthrange(t.year, t.month)[1] if end else 1)
    elif rnd == "y":
        t = t.replace(month=12, day=31) if end else t.replace(month=1, day=1)
    if data_type == "DATETIME":
        if rnd in ("d", "w", "M", "y"):
            t = t.replace(hour=23, minute=59, second=59) if end \
                else t.replace(hour=0, minute=0, second=0)
        elif rnd == "h":
            t = t.replace(minute=59, second=59) if end else t.replace(minute=0, second=0)
        elif rnd == "m":
            t = t.replace(second=59) if end else t.replace(second=0)
        return t.strftime("%Y-%m-%dT%H:%M:%S")
    return t.strftime("%Y-%m-%d")


def load_dashboard(w: WorkspaceClient, dashboard_id: str) -> tuple[dict, list[dict]]:
    """Dashboard metadata plus one scenario per dataset, default params applied."""
    raw = _lakeview_get(w, f"/api/2.0/lakeview/dashboards/{dashboard_id}")
    meta = {"id": raw.get("dashboard_id", dashboard_id), "name": raw.get("display_name"),
            "warehouse_id": raw.get("warehouse_id")}
    serialized = json.loads(raw.get("serialized_dashboard") or "{}")
    scenarios = []
    for ds in serialized.get("datasets", []):
        lines = ds.get("queryLines") or ([ds["query"]] if ds.get("query") else [])
        sql = "".join(l if l.endswith("\n") else l + "\n" for l in lines).strip()
        if not sql:
            continue
        # Longest keyword first so :date_range is not clobbered by :date.
        for p in sorted(ds.get("parameters", []), key=lambda p: -len(p.get("keyword", ""))):
            kw = p.get("keyword")
            if not kw:
                continue
            dsel = p.get("defaultSelection") or {}
            if p.get("complexType") == "RANGE" or "range" in dsel:
                # Queries reference range params as :kw.min / :kw.max only.
                rng = dsel.get("range") or {}
                for side, end in (("min", False), ("max", True)):
                    lit = _range_bound(rng.get(side), p.get("dataType"), end)
                    if lit:
                        sql = sql.replace(f":{kw}.{side}", "'" + lit.replace("'", "''") + "'")
                continue
            vals = (dsel.get("values") or {}).get("values") or []
            if vals and p.get("dataType") in ("DATE", "DATETIME"):
                # Value defaults can be date-math too ("now-1h/h"): floor like a min.
                val = _range_bound(vals[0], p["dataType"], False)
            else:
                val = str(vals[0].get("value", "")) if vals else ""
            sql = sql.replace(f":{kw}", "'" + val.replace("'", "''") + "'")
        scenarios.append({"id": ds.get("name") or f"dataset-{len(scenarios) + 1}",
                          "label": ds.get("displayName") or ds.get("name") or "dataset",
                          "sql": sql})
    return meta, scenarios


def _warehouse_catalog(w: WorkspaceClient) -> tuple[set[str], dict[str, dict]]:
    """One warehouses fetch, two views (both empty on failure): the ids of the
    Reyden warehouses visible to the user, and an id -> {name, size} map of
    every visible warehouse for display."""
    try:
        all_wh = _get(w, "/api/2.0/sql/warehouses").get("warehouses", [])
    except Exception:
        return set(), {}
    return ({x["id"] for x in all_wh if x.get("warehouse_type") == "REYDEN"},
            {x["id"]: {"name": x.get("name"), "size": x.get("cluster_size")}
             for x in all_wh if x.get("id")})


def _list_dashboards(w: WorkspaceClient) -> list[dict]:
    """Active AI/BI dashboards visible to `w`, newest first, warehouse required.

    Dashboards configured to run *on* a Reyden warehouse are excluded: their
    baseline lane would be the Reyden warehouse itself, so a race against it
    is meaningless. Each item carries the baseline warehouse's name/size so
    the pickers can show what the dashboard would race against (null when the
    warehouse is not visible to the user).
    """
    reyden_ids, wh_by_id = _warehouse_catalog(w)
    items, page_token = [], None
    while len(items) < 1000:
        page = _lakeview_get(w, "/api/2.0/lakeview/dashboards", page_size=100, page_token=page_token)
        for d in page.get("dashboards", []):
            if (d.get("lifecycle_state") == "ACTIVE" and d.get("warehouse_id")
                    and d["warehouse_id"] not in reyden_ids):
                wh = wh_by_id.get(d["warehouse_id"], {})
                items.append({"id": d["dashboard_id"],
                              "name": d.get("display_name") or d["dashboard_id"],
                              "warehouse_id": d["warehouse_id"],
                              "warehouse_name": wh.get("name"),
                              "warehouse_size": wh.get("size"),
                              "updated": d.get("update_time") or d.get("create_time")})
        page_token = page.get("next_page_token")
        if not page_token:
            break
    items.sort(key=lambda d: d["updated"] or "", reverse=True)
    return items


def _run_statement(w: WorkspaceClient, warehouse_id: str, stmt: str, row_limit: int = 1000):
    """Execute one statement and poll it to a terminal state."""
    resp = w.statement_execution.execute_statement(
        warehouse_id=warehouse_id,
        statement=stmt,
        wait_timeout="50s",
        format=Format.JSON_ARRAY,
        disposition=Disposition.INLINE,
        row_limit=row_limit,
    )
    state = resp.status.state if resp.status else None
    while state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(0.2)
        resp = w.statement_execution.get_statement(resp.statement_id)
        state = resp.status.state if resp.status else None
    return resp


def execute_timed(w: WorkspaceClient, warehouse_id: str, sql: str) -> dict:
    """Run one statement, cache-busted, and return wall-clock timing."""
    stmt = f"/* race {uuid.uuid4().hex[:12]} */ {sql}"
    t0 = time.perf_counter()
    resp = _run_statement(w, warehouse_id, stmt)
    wall_ms = (time.perf_counter() - t0) * 1000
    state = resp.status.state if resp.status else None
    error = error_code = None
    if state != StatementState.SUCCEEDED:
        err = resp.status.error if resp.status else None
        error = ((err.message if err else None) or str(state))[:300]
        code = err.error_code if err else None
        error_code = getattr(code, "value", None) or (str(code) if code else None)
    rows = resp.manifest.total_row_count if resp.manifest else None
    return {"wall_ms": round(wall_ms, 1), "rows": rows, "error": error,
            "error_code": error_code, "statement_id": resp.statement_id}


# The Reyden preview engine cannot serialize metadata result sets (DESCRIBE
# QUERY output): INLINE fetches die with `merge_json_arrays` and
# EXTERNAL_LINKS is unimplemented. Analysis errors still FAIL the statement
# properly *before* any result exists, so hitting one of these means the
# query itself compiled fine — the probe's answer is already known.
_RESULT_FETCH_QUIRKS = ("merge_json_arrays",
                        "ExternalLinks disposition is not yet implemented")


def _probe(w: WorkspaceClient, warehouse_id: str, sql: str) -> str | None:
    """Run a validation statement; returns a failure reason or None."""
    try:
        resp = _run_statement(w, warehouse_id,
                              f"/* preflight {uuid.uuid4().hex[:12]} */ {sql}",
                              row_limit=10)
    except Exception as e:
        msg = str(e)
        if any(q in msg for q in _RESULT_FETCH_QUIRKS):
            return None
        return msg[:300]
    state = resp.status.state if resp.status else None
    if state != StatementState.SUCCEEDED:
        return (resp.status.error.message if resp.status and resp.status.error
                else str(state))[:300]
    return None


def check_can_use(w: WorkspaceClient, warehouse_id: str) -> str | None:
    """Prove the user can run statements on the warehouse (requires CAN USE).

    SELECT 1 touches no table, so it validates the warehouse permission
    without reading any data (and warms the warehouse up as a side effect).
    """
    return _probe(w, warehouse_id, "SELECT 1")


def check_query_compiles(w: WorkspaceClient, warehouse_id: str, sql: str) -> str | None:
    """Prove the user can run `sql` on `warehouse_id` without reading data.

    DESCRIBE QUERY only analyzes: it resolves every table/view and enforces
    Unity Catalog privileges — PERMISSION_DENIED, TABLE_OR_VIEW_NOT_FOUND and
    friends fail the statement — and submitting it at all requires CAN USE on
    the warehouse. Returns a failure reason, or None when the query is safe
    to profile.
    """
    return _probe(w, warehouse_id, f"DESCRIBE QUERY {sql}")


def _skip(dash: dict, reason: str):
    dash["status"] = "skipped"
    dash["reason"] = reason


def _stopped(profile: dict, awaiting: str) -> bool:
    """True when the user asked the batch to stop; skips the stragglers.

    Both worker loops call this at the top of each iteration: validation
    with awaiting="pending", profiling with awaiting="ready". Every
    dashboard still in that state is skipped so the batch winds down to
    "done" with partial results — the slot frees when the worker finishes.
    """
    with STATE_LOCK:
        if not profile["stop"]:
            return False
    for dash in profile["dashboards"]:
        if dash["status"] == awaiting:
            _skip(dash, "stopped by user")
    return True


def _validate_all(profile: dict):
    """Phase 1 — runs to completion before any dataset query executes."""
    w = profile["client"]
    err = check_can_use(w, profile["reyden"]["id"])
    if err:
        profile["error"] = (f"You cannot run statements on Reyden warehouse "
                            f"'{profile['reyden']['name']}' (need CAN USE): {err}")
        profile["status"] = "failed"
        return
    for dash in profile["dashboards"]:
        if _stopped(profile, "pending"):
            break
        dash["status"] = "validating"
        try:
            meta, scenarios = load_dashboard(w, dash["id"])
        except Exception as e:
            _skip(dash, f"cannot read dashboard: {str(e)[:200]}")
            continue
        dash["name"] = meta["name"] or dash["name"]
        if not meta["warehouse_id"]:
            _skip(dash, "no warehouse configured")
            continue
        if not scenarios:
            _skip(dash, "no dataset queries")
            continue
        baseline, werr = check_warehouse(w, meta["warehouse_id"])
        if werr:
            _skip(dash, werr)
            continue
        if baseline.get("type") == "REYDEN":
            _skip(dash, f"runs on Reyden warehouse '{baseline['name']}' — "
                        f"no baseline to race against")
            continue
        dash["warehouses"] = {"reyden": profile["reyden"], "baseline": baseline}
        # Compile every dataset on the dashboard's own warehouse: proves CAN USE
        # there plus SELECT on everything the query touches, without reading data.
        with ThreadPoolExecutor(max_workers=min(len(scenarios), EXPLAIN_POOL),
                                thread_name_prefix=f"preflight-{dash['id'][:8]}") as pool:
            futures = {sc["id"]: pool.submit(check_query_compiles, w, baseline["id"], sc["sql"])
                       for sc in scenarios}
        checks = {sc_id: f.result() for sc_id, f in futures.items()}
        dash["dataset_checks"] = [{"id": sc["id"], "label": sc["label"],
                                   "error": checks[sc["id"]]} for sc in scenarios]
        ok = [sc for sc in scenarios if checks[sc["id"]] is None]
        if not ok:
            _skip(dash, "you lack access to the data behind every dataset query")
            continue
        dash["scenarios"] = ok
        dash["scenario_ids"] = [sc["id"] for sc in ok]
        dash["lanes"] = {lane: {"inflight": {}, "run_wall_ms": [], "done": False,
                                "ready": False, "error": None} for lane in LANES}
        dash["status"] = "ready"


def _lane_worker(profile: dict, dash: dict, lane: str):
    w = profile["client"]
    wid = dash["warehouses"][lane]["id"]
    st = dash["lanes"][lane]
    scenarios = dash["scenarios"]
    runs = profile["runs"]

    def one_query(sc: dict, run: int) -> bool:
        """Run one dataset query; returns True if it errored on this lane."""
        with STATE_LOCK:
            st["inflight"][sc["id"]] = {"scenario_id": sc["id"], "run": run,
                                        "started_at": time.time()}
        try:
            r = execute_timed(w, wid, sc["sql"])
        finally:
            with STATE_LOCK:
                st["inflight"].pop(sc["id"], None)
        with STATE_LOCK:
            dash["results"].append({"lane": lane, "scenario_id": sc["id"], "run": run, **r})
        return r["error"] is not None

    try:
        # Warm-up (spins the warehouse up; excluded from results)
        execute_timed(w, wid, "SELECT 1")
        st["ready"] = True
        dash["barrier"].wait(timeout=300)  # start both lanes together
        # Fire every dataset query of a run concurrently — the same burst a
        # dashboard sends when it loads. Runs stay sequential so each run's
        # lane wall-clock is a clean "dashboard load time".
        with ThreadPoolExecutor(max_workers=max(len(scenarios), 1),
                                thread_name_prefix=f"race-{lane}") as pool:
            for run in range(1, runs + 1):
                t0 = time.perf_counter()
                errs = [f.result() for f in
                        [pool.submit(one_query, sc, run) for sc in scenarios]]
                wall = round((time.perf_counter() - t0) * 1000, 1)
                if not any(errs):  # a load wall only counts if every query was clean
                    st["run_wall_ms"].append(wall)
    except Exception as e:  # surface lane-level failures to the UI
        st["error"] = (str(e) or type(e).__name__)[:300]
        dash["barrier"].abort()  # free the peer lane instead of a 300s wait
    finally:
        st["done"] = True


def _summarize(dash: dict) -> dict:
    med = {}  # (scenario_id, lane) -> median wall
    for r in dash["results"]:
        if not r["error"]:
            med.setdefault((r["scenario_id"], r["lane"]), []).append(r["wall_ms"])
    ratios, per_scenario = [], []
    for sc_id in dash["scenario_ids"]:
        a = statistics.median(med.get((sc_id, "reyden"), [0]))
        b = statistics.median(med.get((sc_id, "baseline"), [0]))
        entry = {"scenario_id": sc_id, "reyden_ms": a or None, "baseline_ms": b or None}
        if a and b:
            entry["speedup"] = round(b / a, 2)
            ratios.append(b / a)
        per_scenario.append(entry)
    totals = {lane: round(sum(r["wall_ms"] for r in dash["results"]
                              if r["lane"] == lane and not r["error"]), 1)
              for lane in LANES}
    load = {lane: (round(statistics.median(dash["lanes"][lane]["run_wall_ms"]), 1)
                   if dash["lanes"][lane]["run_wall_ms"] else None) for lane in LANES}
    wins = sum(1 for e in per_scenario if e.get("speedup", 0) > 1)
    return {
        # Two different statistics, deliberately kept apart: `load_speedup`
        # compares end-to-end dashboard load walls (what the UI verdicts show,
        # since it matches the seconds next to them), while `geomean_speedup`
        # aggregates per-dataset ratios — those include queueing, so on a
        # saturated warehouse they can far exceed the load ratio.
        "load_speedup": (round(load["baseline"] / load["reyden"], 2)
                         if load["reyden"] and load["baseline"] else None),
        "geomean_speedup": round(statistics.geometric_mean(ratios), 2) if ratios else None,
        "min_speedup": round(min(ratios), 2) if ratios else None,
        "max_speedup": round(max(ratios), 2) if ratios else None,
        "reyden_wins": wins,
        "pair_count": len(ratios),
        "scenario_count": len(per_scenario),
        "total_ms": totals,
        "load_ms": load,
        "per_scenario": per_scenario,
    }


def _overall(profile: dict) -> dict:
    ratios, per_dashboard = [], []
    wins = pairs = 0
    totals = {lane: 0.0 for lane in LANES}
    for dash in profile["dashboards"]:
        s = dash.get("summary")
        if not s:
            continue
        for e in s["per_scenario"]:
            if e.get("speedup"):
                ratios.append(e["speedup"])
                pairs += 1
                wins += e["speedup"] > 1
        for lane in LANES:
            totals[lane] = round(totals[lane] + s["total_ms"][lane], 1)
        per_dashboard.append({"id": dash["id"], "name": dash["name"],
                              "speedup": s["geomean_speedup"],
                              "load_speedup": s.get("load_speedup")})
    load_ratios = [d["load_speedup"] for d in per_dashboard if d["load_speedup"]]
    ranked = sorted((d for d in per_dashboard if d["load_speedup"]),
                    key=lambda d: d["load_speedup"], reverse=True)
    return {
        "load_geomean_speedup": (round(statistics.geometric_mean(load_ratios), 2)
                                 if load_ratios else None),
        "geomean_speedup": round(statistics.geometric_mean(ratios), 2) if ratios else None,
        "dashboards_profiled": sum(1 for d in profile["dashboards"] if d["status"] == "done"),
        "dashboards_skipped": sum(1 for d in profile["dashboards"] if d["status"] == "skipped"),
        "dashboards_failed": sum(1 for d in profile["dashboards"] if d["status"] == "error"),
        "reyden_wins": wins,
        "pair_count": pairs,
        "total_ms": totals,
        "per_dashboard": per_dashboard,
        "best": ranked[0] if ranked else None,
    }


def _run_dashboard(profile: dict, dash: dict):
    dash["barrier"] = threading.Barrier(len(LANES))
    threads = [threading.Thread(target=_lane_worker, args=(profile, dash, lane),
                                daemon=True, name=f"profile-{dash['id'][:8]}-{lane}")
               for lane in LANES]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    dash["summary"] = _summarize(dash)


def _batch_worker(profile: dict):
    try:
        _validate_all(profile)
        if profile["status"] == "failed":
            return
        profile["status"] = "running"
        for dash in profile["dashboards"]:
            if _stopped(profile, "ready"):
                break
            if dash["status"] != "ready":
                continue
            dash["status"] = "profiling"
            _run_dashboard(profile, dash)
            lane_errors = [st["error"] for st in dash["lanes"].values() if st["error"]]
            # No successful query means there's no honest verdict to show —
            # mark it failed even when no lane-level exception was raised (every
            # dataset can error at the SQL level and still "complete").
            if not any(not r["error"] for r in dash["results"]):
                dash["status"] = "error"
                dash["reason"] = (lane_errors[0] if lane_errors
                                  else "all dataset queries failed")
            else:
                dash["status"] = "done"
        profile["summary"] = _overall(profile)
        profile["status"] = "done"
    except Exception as e:
        profile["error"] = str(e)[:300]
        profile["status"] = "failed"


def _snapshot(profile: dict) -> dict:
    now = time.time()
    with STATE_LOCK:
        stopping = profile["stop"]
        dashboards = []
        for d in profile["dashboards"]:
            lanes = None
            if d.get("lanes"):
                lanes = {lane: {"ready": st["ready"], "done": st["done"], "error": st["error"],
                                "inflight": [{**c, "elapsed_ms": round((now - c["started_at"]) * 1000, 1)}
                                             for c in st["inflight"].values()]}
                         for lane, st in d["lanes"].items()}
            seen, failures = set(), []  # failed queries, one per (scenario, lane)
            for r in d.get("results") or []:
                key = (r["scenario_id"], r["lane"])
                if r["error"] and key not in seen and len(failures) < 40:
                    seen.add(key)
                    failures.append({"scenario_id": r["scenario_id"], "lane": r["lane"],
                                     "error_code": r.get("error_code"), "error": r["error"]})
            dashboards.append({
                "id": d["id"], "name": d["name"], "status": d["status"],
                "reason": d.get("reason"),
                "baseline": (d.get("warehouses") or {}).get("baseline"),
                "datasets": d.get("dataset_checks"),
                "failures": failures,
                "scenario_ids": d.get("scenario_ids") or [],
                "queries_done": len(d.get("results") or []),
                "queries_total": len(d.get("scenario_ids") or []) * profile["runs"] * len(LANES),
                "lanes": lanes,
                "summary": d.get("summary"),
            })
    return {"id": profile["id"], "status": profile["status"], "error": profile["error"],
            "runs": profile["runs"], "reyden": profile["reyden"], "stopping": stopping,
            "dashboards": dashboards, "summary": profile["summary"]}


class ProfileRequest(BaseModel):
    reyden_warehouse_id: str
    dashboard_ids: list[str] | None = None  # None -> every dashboard the user can see
    runs: int = 1


@app.get("/api/dashboards")
def dashboards(request: Request):
    """AI/BI dashboards the signed-in user can access (with a warehouse set)."""
    w = client_for(request)
    return {"dashboards": _list_dashboards(w),
            "user": request.headers.get("x-forwarded-email"),
            "max_batch": MAX_DASHBOARDS}


@app.get("/api/dashboards/{dashboard_id}")
def dashboard_detail(dashboard_id: str, request: Request):
    """Single-race page: dashboard metadata + its raceable dataset scenarios."""
    w = client_for(request)
    try:
        meta, scenarios = load_dashboard(w, dashboard_id)
    except Exception as e:
        raise HTTPException(404, f"Cannot read dashboard {dashboard_id}: {e}")
    warehouse = warehouse_info(w, meta["warehouse_id"]) if meta["warehouse_id"] else None
    return {"id": meta["id"], "name": meta["name"],
            "url": f"{w.config.host}/sql/dashboardsv3/{meta['id']}",
            "warehouse": warehouse,
            "scenarios": [{"id": s["id"], "label": s["label"]} for s in scenarios]}


@app.get("/api/warehouses")
def warehouses(request: Request):
    """Reyden warehouses the signed-in user can see."""
    w = client_for(request)
    try:
        all_wh = _get(w, "/api/2.0/sql/warehouses").get("warehouses", [])
    except Exception as e:
        # A session created before the app's scopes were configured lacks
        # `sql` until the user re-authenticates.
        if "scope" in str(e).lower():
            raise HTTPException(403, "Your session is missing the SQL scope — open "
                                     "/.auth/sign_out on this app's URL, then sign back in "
                                     "and approve the consent prompt.")
        raise
    reyden = sorted((_wh(x) for x in all_wh if x.get("warehouse_type") == "REYDEN"),
                    key=lambda x: (x["name"] or "").lower())
    return {"reyden": reyden}


@app.post("/api/profile")
def start_profile(req: ProfileRequest, request: Request):
    profile = {
        "id": uuid.uuid4().hex[:10], "status": "validating", "error": None,
        "created": time.time(), "runs": max(1, min(req.runs, 3)), "stop": False,
        "reyden": None, "client": None, "summary": None, "dashboards": [],
    }
    _claim_slot(profile, PROFILES)
    try:
        w = client_for(request)
        reyden, err = check_warehouse(w, req.reyden_warehouse_id)
        if err:
            raise HTTPException(403, f"Reyden warehouse: {err}")
        if reyden.get("type") and reyden["type"] != "REYDEN":
            raise HTTPException(400, f"Warehouse '{reyden['name']}' is not a Reyden warehouse.")

        if req.dashboard_ids:
            ids = list(dict.fromkeys(req.dashboard_ids))  # dedupe, keep order
            picked = [{"id": i, "name": i} for i in ids]
        else:
            picked = [{"id": d["id"], "name": d["name"]} for d in _list_dashboards(w)]
        if not picked:
            raise HTTPException(400, "No dashboards to profile — share one with the app or pass dashboard_ids.")
        if len(picked) > MAX_DASHBOARDS:
            raise HTTPException(400, f"Batch limited to {MAX_DASHBOARDS} dashboards per run "
                                     f"({len(picked)} requested) — pass a dashboard_ids subset.")
        profile["reyden"] = reyden
        profile["client"] = w
        profile["dashboards"] = [{"id": d["id"], "name": d["name"], "status": "pending",
                                  "reason": None, "results": []} for d in picked]
    except BaseException:
        with STATE_LOCK:  # release the slot on any setup failure
            PROFILES.pop(profile["id"], None)
        raise

    threading.Thread(target=_batch_worker, args=(profile,), daemon=True,
                     name=f"batch-{profile['id']}").start()
    return {"profile_id": profile["id"], "dashboard_ids": [d["id"] for d in picked],
            "runs": profile["runs"]}


@app.get("/api/profile/active")
def active_profile():
    """Id of the batch currently validating/running, or null.

    Lets a page reattach to a live batch it has no saved id for (e.g. a
    second browser). Registered before /api/profile/{profile_id} so the
    literal path wins.
    """
    with STATE_LOCK:
        for p in PROFILES.values():
            if p["status"] in ("validating", "running"):
                return {"profile_id": p["id"]}
    return {"profile_id": None}


@app.get("/api/profile/{profile_id}")
def profile_state(profile_id: str):
    profile = PROFILES.get(profile_id)
    if profile is None:
        raise HTTPException(404, "No such profile")
    return _snapshot(profile)


@app.post("/api/profile/{profile_id}/stop")
def stop_profile(profile_id: str):
    """Ask a running batch to stop after the dashboard currently profiling.

    Cooperative and idempotent: sets a flag the worker loops check between
    dashboards, so the batch still finishes with partial results and frees
    the slot itself. Statements already in flight are not cancelled; a
    profile that already ended is unaffected.
    """
    profile = PROFILES.get(profile_id)
    if profile is None:
        raise HTTPException(404, "No such profile")
    with STATE_LOCK:
        profile["stop"] = True
    return {"stopping": True}


# ---------- single-dashboard race (the original mode, served at /race) ----------
# A race is one profile-dashboard rolled into a single dict: it carries both
# the "profile" keys (client, runs) and the "dash" keys (scenarios, lanes,
# warehouses, results), so _run_dashboard/_lane_worker/_summarize run it as-is.

class RaceRequest(BaseModel):
    dashboard_id: str
    reyden_warehouse_id: str
    scenario_ids: list[str] | None = None
    runs: int = 1


def _race_worker(race: dict):
    try:
        _run_dashboard(race, race)
        race["status"] = "done"
    except Exception as e:
        race["error"] = str(e)[:300]
        race["status"] = "failed"


@app.post("/api/race")
def start_race(req: RaceRequest, request: Request):
    race = {
        "id": uuid.uuid4().hex[:10], "status": "running", "error": None,
        "created": time.time(), "runs": max(1, min(req.runs, 3)),
        "summary": None, "results": [],
    }
    _claim_slot(race, RACES)
    try:
        w = client_for(request)
        try:
            meta, scenarios = load_dashboard(w, req.dashboard_id)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(404, f"Cannot read dashboard {req.dashboard_id}: {e}")
        if not meta["warehouse_id"]:
            raise HTTPException(400, "This dashboard has no warehouse configured — pick another one.")
        if not scenarios:
            raise HTTPException(400, "This dashboard has no dataset queries to race.")
        if req.scenario_ids:
            by_id = {s["id"]: s for s in scenarios}
            unknown = [i for i in req.scenario_ids if i not in by_id]
            if unknown:
                raise HTTPException(400, f"Unknown scenarios: {unknown}")
            scenarios = [by_id[i] for i in req.scenario_ids]

        reyden = warehouse_info(w, req.reyden_warehouse_id)
        if reyden.get("type") and reyden["type"] != "REYDEN":
            raise HTTPException(400, f"Warehouse '{reyden['name']}' is not a Reyden warehouse.")
        baseline = warehouse_info(w, meta["warehouse_id"])
        if baseline.get("type") == "REYDEN":
            raise HTTPException(400, f"“{meta['name']}” runs on Reyden warehouse "
                                     f"'{baseline['name']}' — there is no baseline to race against.")
        race.update({
            "client": w,
            "dashboard": {"id": meta["id"], "name": meta["name"]},
            "warehouses": {"reyden": reyden, "baseline": baseline},
            "scenarios": scenarios,
            "scenario_ids": [s["id"] for s in scenarios],
            "lanes": {lane: {"inflight": {}, "run_wall_ms": [], "done": False,
                             "ready": False, "error": None} for lane in LANES},
        })
    except BaseException:
        with STATE_LOCK:  # release the slot on any setup failure
            RACES.pop(race["id"], None)
        raise
    threading.Thread(target=_race_worker, args=(race,), daemon=True,
                     name=f"race-{race['id']}").start()
    return {"race_id": race["id"], "scenario_ids": race["scenario_ids"], "runs": race["runs"]}


@app.get("/api/race/{race_id}")
def race_state(race_id: str):
    race = RACES.get(race_id)
    if race is None:
        raise HTTPException(404, "No such race")
    now = time.time()
    with STATE_LOCK:
        lanes = {lane: {"ready": st["ready"], "done": st["done"], "error": st["error"],
                        "inflight": [{**c, "elapsed_ms": round((now - c["started_at"]) * 1000, 1)}
                                     for c in st["inflight"].values()]}
                 for lane, st in race["lanes"].items()}
        results = list(race["results"])
    return {"id": race_id, "status": race["status"], "error": race["error"],
            "runs": race["runs"],
            "scenario_ids": race["scenario_ids"], "dashboard": race["dashboard"],
            "warehouses": race["warehouses"], "lanes": lanes,
            "results": results, "summary": race["summary"]}


@app.get("/healthz")
def healthz():
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/race")
def race_page():
    return FileResponse(STATIC_DIR / "race.html")
