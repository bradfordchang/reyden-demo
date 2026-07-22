"""Reyden Query Race — Databricks App backend.

Races identical dashboard queries on two SQL warehouses (Reyden vs Serverless
Starter) via the SQL Statement Execution API and exposes live timings for the
frontend to animate. Auth: WorkspaceClient() picks up the app's injected M2M
OAuth in Databricks Apps, or a CLI profile locally (DATABRICKS_CONFIG_PROFILE).
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
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

APP_DIR = Path(__file__).parent
STATIC_DIR = APP_DIR.parent / "static"

# All environment-specific values come from env vars: in Databricks Apps via
# app.yaml (warehouse ids via resource valueFrom), locally via `--env-file .env`.
def _required(var: str) -> str:
    val = os.environ.get(var)
    if not val:
        raise RuntimeError(f"missing required env var {var} — set it in app.yaml (deployed) or .env (local)")
    return val


WAREHOUSES = {
    "reyden": {
        "id": _required("REYDEN_WAREHOUSE_ID"),
        "label": os.environ.get("REYDEN_LABEL", "Reyden"),
        "size": os.environ.get("REYDEN_SIZE", "Small"),
        "accent": "reyden",
    },
    "starter": {
        "id": _required("STARTER_WAREHOUSE_ID"),
        "label": os.environ.get("STARTER_LABEL", "Serverless Starter"),
        "size": os.environ.get("STARTER_SIZE", "X-Large Pro"),
        "accent": "starter",
    },
}

DASHBOARDS = {  # optional deep links shown in the frontend header
    "reyden": os.environ.get("REYDEN_DASHBOARD_URL"),
    "starter": os.environ.get("STARTER_DASHBOARD_URL"),
}


def _load_scenarios() -> list[dict]:
    """Load scenarios.json and resolve {{PLACEHOLDER}} tokens from env.
    Scenarios with unresolved placeholders are dropped (e.g. the workspace
    filter scenario when FILTER_WORKSPACES is unset)."""
    metric_view = _required("METRIC_VIEW")
    ws = [w.strip() for w in os.environ.get("FILTER_WORKSPACES", "").split(",") if w.strip()]
    ws_in = ", ".join("'" + w.replace("'", "''") + "'" for w in ws)
    out = []
    for s in json.loads((APP_DIR / "scenarios.json").read_text()):
        sql = s["sql"].replace("{{METRIC_VIEW}}", metric_view)
        if ws_in:
            sql = sql.replace("{{FILTER_WORKSPACES_IN}}", ws_in)
        if "{{" in sql:
            continue
        out.append({**s, "sql": sql})
    return out


SCENARIOS = _load_scenarios()
SCENARIOS_BY_ID = {s["id"]: s for s in SCENARIOS}

app = FastAPI(title="Reyden Query Race")
_w = None
_w_lock = threading.Lock()

RACES: dict[str, dict] = {}
RACES_LOCK = threading.Lock()


def wc() -> WorkspaceClient:
    global _w
    with _w_lock:
        if _w is None:
            _w = WorkspaceClient()
    return _w


def execute_timed(warehouse_id: str, sql: str) -> dict:
    """Run one statement, cache-busted, and return wall-clock timing."""
    stmt = f"/* race {uuid.uuid4().hex[:12]} */ {sql}"
    t0 = time.perf_counter()
    resp = wc().statement_execution.execute_statement(
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
        resp = wc().statement_execution.get_statement(resp.statement_id)
        state = resp.status.state if resp.status else None
    wall_ms = (time.perf_counter() - t0) * 1000
    error = None
    if state != StatementState.SUCCEEDED:
        error = (resp.status.error.message if resp.status and resp.status.error else str(state))[:300]
    rows = resp.manifest.total_row_count if resp.manifest else None
    return {"wall_ms": round(wall_ms, 1), "rows": rows, "error": error,
            "statement_id": resp.statement_id}


def _lane_worker(race: dict, lane: str, scenarios: list[dict], runs: int):
    wid = WAREHOUSES[lane]["id"]
    try:
        # Warm-up (spins the warehouse up; excluded from results)
        execute_timed(wid, "SELECT 1")
        race["lanes"][lane]["ready"] = True
        race["barrier"].wait(timeout=300)  # start both lanes together
        for run in range(1, runs + 1):
            for sc in scenarios:
                race["lanes"][lane]["current"] = {
                    "scenario_id": sc["id"], "run": run, "started_at": time.time(),
                }
                r = execute_timed(wid, sc["sql"])
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
        b = statistics.median(med.get((sc_id, "starter"), [0]))
        entry = {"scenario_id": sc_id, "reyden_ms": a or None, "starter_ms": b or None}
        if a and b:
            entry["speedup"] = round(b / a, 2)
            ratios.append(b / a)
        per_scenario.append(entry)
    totals = {lane: round(sum(r["wall_ms"] for r in race["results"]
                              if r["lane"] == lane and not r["error"]), 1)
              for lane in WAREHOUSES}
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
    scenario_ids: list[str] | None = None
    runs: int = 1


@app.get("/api/config")
def config():
    return {
        "warehouses": WAREHOUSES,
        "dashboards": DASHBOARDS,
        "scenarios": [{k: s[k] for k in ("id", "page", "label", "quick")} for s in SCENARIOS],
    }


@app.post("/api/race")
def start_race(req: RaceRequest):
    with RACES_LOCK:
        if any(r["status"] == "running" for r in RACES.values()):
            raise HTTPException(409, "A race is already running — wait for it to finish.")
    ids = req.scenario_ids or [s["id"] for s in SCENARIOS if s["quick"]]
    unknown = [i for i in ids if i not in SCENARIOS_BY_ID]
    if unknown:
        raise HTTPException(400, f"Unknown scenarios: {unknown}")
    runs = max(1, min(req.runs, 3))
    scenarios = [SCENARIOS_BY_ID[i] for i in ids]

    race_id = uuid.uuid4().hex[:10]
    race = {
        "id": race_id, "status": "running", "created": time.time(),
        "runs": runs, "scenario_ids": ids, "results": [], "summary": None,
        "lanes": {lane: {"current": None, "done": False, "ready": False, "error": None}
                  for lane in WAREHOUSES},
        "barrier": threading.Barrier(len(WAREHOUSES)),
    }
    with RACES_LOCK:
        RACES[race_id] = race
        for rid in sorted(RACES, key=lambda r: RACES[r]["created"])[:-10]:
            del RACES[rid]
    for lane in WAREHOUSES:
        threading.Thread(target=_lane_worker, args=(race, lane, scenarios, runs),
                         daemon=True, name=f"race-{race_id}-{lane}").start()
    return {"race_id": race_id, "scenario_ids": ids, "runs": runs}


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
            "scenario_ids": race["scenario_ids"], "lanes": lanes,
            "results": results, "summary": race["summary"]}


@app.get("/healthz")
def healthz():
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
