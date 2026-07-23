"""Reyden Query Race — Databricks App backend.

Lets the signed-in user pick any AI/BI (Lakeview) dashboard they can access,
builds race scenarios live from that dashboard's dataset queries, and races
them head-to-head: a user-selected Reyden warehouse vs the warehouse the
dashboard is configured to run on.

Auth is on-behalf-of-user for warehouses and the race queries themselves:
those calls use the token Databricks Apps forwards in
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
import json
import os
import statistics
import threading
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

# reyden = the Reyden warehouse the user picked; baseline = the dashboard's own warehouse
LANES = ("reyden", "baseline")

app = FastAPI(title="Reyden Query Race")
_local = None
_local_lock = threading.Lock()

RACES: dict[str, dict] = {}
RACES_LOCK = threading.Lock()


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


def _lakeview_get(w: WorkspaceClient, path: str, **query) -> dict:
    """Lakeview REST as the user, falling back to the app SP.

    Apps user authorization offers no Lakeview scope yet, so the forwarded
    user token can't call /api/2.0/lakeview/*. Fall back to the app's SP,
    which sees only dashboards shared with the app. Once an account admin
    adds `dashboards.lakeview` to the app's OAuth integration (see README),
    the user-token call succeeds and this fallback stops triggering.
    Scope-error text varies: "Invalid scope, required scopes: dashboards" /
    "Provided OAuth token does not have required scopes: dashboards".
    """
    try:
        return _get(w, path, **query)
    except Exception as e:
        msg = str(e)
        sp = _sp_client()
        if w is not sp and "scope" in msg.lower() and "dashboards" in msg:
            return _get(sp, path, **query)
        raise


def _wh(raw: dict) -> dict:
    return {"id": raw.get("id"), "name": raw.get("name"), "size": raw.get("cluster_size"),
            "type": raw.get("warehouse_type"),
            "serverless": raw.get("enable_serverless_compute"), "state": raw.get("state")}


def warehouse_info(w: WorkspaceClient, wh_id: str) -> dict:
    """Warehouse details via raw REST (the SDK enum drops warehouse_type REYDEN)."""
    try:
        return _wh(_get(w, f"/api/2.0/sql/warehouses/{wh_id}"))
    except Exception:
        return {"id": wh_id, "name": f"warehouse {wh_id[:8]}…", "size": None,
                "type": None, "serverless": None, "state": None}


def load_dashboard(w: WorkspaceClient, dashboard_id: str) -> tuple[dict, list[dict]]:
    """Dashboard metadata plus one scenario per dataset, default params applied."""
    try:
        raw = _lakeview_get(w, f"/api/2.0/lakeview/dashboards/{dashboard_id}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(404, f"Cannot read dashboard {dashboard_id}: {e}")
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
            vals = ((p.get("defaultSelection") or {}).get("values") or {}).get("values") or []
            val = str(vals[0].get("value", "")) if vals else ""
            if kw:
                sql = sql.replace(f":{kw}", "'" + val.replace("'", "''") + "'")
        scenarios.append({"id": ds.get("name") or f"dataset-{len(scenarios) + 1}",
                          "label": ds.get("displayName") or ds.get("name") or "dataset",
                          "sql": sql})
    return meta, scenarios


def execute_timed(w: WorkspaceClient, warehouse_id: str, sql: str) -> dict:
    """Run one statement, cache-busted, and return wall-clock timing."""
    stmt = f"/* race {uuid.uuid4().hex[:12]} */ {sql}"
    t0 = time.perf_counter()
    resp = w.statement_execution.execute_statement(
        warehouse_id=warehouse_id,
        statement=stmt,
        wait_timeout="50s",
        format=Format.JSON_ARRAY,
        disposition=Disposition.INLINE,
        row_limit=1000,
    )
    state = resp.status.state if resp.status else None
    while state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(0.2)
        resp = w.statement_execution.get_statement(resp.statement_id)
        state = resp.status.state if resp.status else None
    wall_ms = (time.perf_counter() - t0) * 1000
    error = None
    if state != StatementState.SUCCEEDED:
        error = (resp.status.error.message if resp.status and resp.status.error else str(state))[:300]
    rows = resp.manifest.total_row_count if resp.manifest else None
    return {"wall_ms": round(wall_ms, 1), "rows": rows, "error": error,
            "statement_id": resp.statement_id}


def _lane_worker(race: dict, lane: str, scenarios: list[dict], runs: int):
    w = race["client"]
    wid = race["warehouses"][lane]["id"]
    try:
        # Warm-up (spins the warehouse up; excluded from results)
        execute_timed(w, wid, "SELECT 1")
        race["lanes"][lane]["ready"] = True
        race["barrier"].wait(timeout=300)  # start both lanes together
        for run in range(1, runs + 1):
            for sc in scenarios:
                race["lanes"][lane]["current"] = {
                    "scenario_id": sc["id"], "run": run, "started_at": time.time(),
                }
                r = execute_timed(w, wid, sc["sql"])
                with RACES_LOCK:
                    race["results"].append({
                        "lane": lane, "scenario_id": sc["id"], "run": run, **r,
                    })
                race["lanes"][lane]["current"] = None
    except Exception as e:  # surface lane-level failures to the UI
        race["lanes"][lane]["error"] = str(e)[:300]
    finally:
        race["lanes"][lane]["done"] = True
        if all(l["done"] for l in race["lanes"].values()):
            race["summary"] = _summarize(race)
            race["status"] = "done"


def _summarize(race: dict) -> dict:
    med = {}  # (scenario_id, lane) -> median wall
    for r in race["results"]:
        if not r["error"]:
            med.setdefault((r["scenario_id"], r["lane"]), []).append(r["wall_ms"])
    ratios, per_scenario = [], []
    for sc_id in race["scenario_ids"]:
        a = statistics.median(med.get((sc_id, "reyden"), [0]))
        b = statistics.median(med.get((sc_id, "baseline"), [0]))
        entry = {"scenario_id": sc_id, "reyden_ms": a or None, "baseline_ms": b or None}
        if a and b:
            entry["speedup"] = round(b / a, 2)
            ratios.append(b / a)
        per_scenario.append(entry)
    totals = {lane: round(sum(r["wall_ms"] for r in race["results"]
                              if r["lane"] == lane and not r["error"]), 1)
              for lane in LANES}
    wins = sum(1 for e in per_scenario if e.get("speedup", 0) > 1)
    return {
        "geomean_speedup": round(statistics.geometric_mean(ratios), 2) if ratios else None,
        "min_speedup": round(min(ratios), 2) if ratios else None,
        "max_speedup": round(max(ratios), 2) if ratios else None,
        "reyden_wins": wins,
        "scenario_count": len(per_scenario),
        "total_ms": totals,
        "per_scenario": per_scenario,
    }


class RaceRequest(BaseModel):
    dashboard_id: str
    reyden_warehouse_id: str
    scenario_ids: list[str] | None = None
    runs: int = 1


@app.get("/api/dashboards")
def dashboards(request: Request):
    """AI/BI dashboards the signed-in user can access (with a warehouse set)."""
    w = client_for(request)
    items, page_token = [], None
    while len(items) < 1000:
        page = _lakeview_get(w, "/api/2.0/lakeview/dashboards", page_size=100, page_token=page_token)
        for d in page.get("dashboards", []):
            if d.get("lifecycle_state") == "ACTIVE" and d.get("warehouse_id"):
                items.append({"id": d["dashboard_id"],
                              "name": d.get("display_name") or d["dashboard_id"],
                              "warehouse_id": d["warehouse_id"],
                              "updated": d.get("update_time") or d.get("create_time")})
        page_token = page.get("next_page_token")
        if not page_token:
            break
    items.sort(key=lambda d: d["updated"] or "", reverse=True)
    return {"dashboards": items, "user": request.headers.get("x-forwarded-email")}


@app.get("/api/dashboards/{dashboard_id}")
def dashboard_detail(dashboard_id: str, request: Request):
    w = client_for(request)
    meta, scenarios = load_dashboard(w, dashboard_id)
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


@app.post("/api/race")
def start_race(req: RaceRequest, request: Request):
    with RACES_LOCK:
        if any(r["status"] == "running" for r in RACES.values()):
            raise HTTPException(409, "A race is already running — wait for it to finish.")
    w = client_for(request)
    meta, scenarios = load_dashboard(w, req.dashboard_id)
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
    runs = max(1, min(req.runs, 3))

    reyden = warehouse_info(w, req.reyden_warehouse_id)
    if reyden.get("type") and reyden["type"] != "REYDEN":
        raise HTTPException(400, f"Warehouse '{reyden['name']}' is not a Reyden warehouse.")
    baseline = warehouse_info(w, meta["warehouse_id"])

    race_id = uuid.uuid4().hex[:10]
    race = {
        "id": race_id, "status": "running", "created": time.time(),
        "runs": runs, "scenario_ids": [s["id"] for s in scenarios],
        "dashboard": {"id": meta["id"], "name": meta["name"]},
        "warehouses": {"reyden": reyden, "baseline": baseline},
        "client": w, "results": [], "summary": None,
        "lanes": {lane: {"current": None, "done": False, "ready": False, "error": None}
                  for lane in LANES},
        "barrier": threading.Barrier(len(LANES)),
    }
    with RACES_LOCK:
        RACES[race_id] = race
        for rid in sorted(RACES, key=lambda r: RACES[r]["created"])[:-10]:
            del RACES[rid]
    for lane in LANES:
        threading.Thread(target=_lane_worker, args=(race, lane, scenarios, runs),
                         daemon=True, name=f"race-{race_id}-{lane}").start()
    return {"race_id": race_id, "scenario_ids": race["scenario_ids"], "runs": runs}


@app.get("/api/race/{race_id}")
def race_state(race_id: str):
    race = RACES.get(race_id)
    if race is None:
        raise HTTPException(404, "No such race")
    now = time.time()
    lanes = {}
    for lane, st in race["lanes"].items():
        cur = st["current"]
        lanes[lane] = {
            "ready": st["ready"], "done": st["done"], "error": st["error"],
            "current": None if cur is None else {
                **cur, "elapsed_ms": round((now - cur["started_at"]) * 1000, 1),
            },
        }
    with RACES_LOCK:
        results = list(race["results"])
    return {"id": race_id, "status": race["status"], "runs": race["runs"],
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
