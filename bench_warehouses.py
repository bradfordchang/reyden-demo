# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "requests",
# ]
# ///
"""Benchmark AI/BI dashboard query performance across SQL warehouses.

Replays the widget-shaped queries of the Cost Intelligence dashboard (page
defaults plus filtered variants) against two or more warehouses, with the
query result cache disabled, and reports wall-clock plus server-side timings.

Dataset SQL is pulled live from the deployed dashboard via the Lakeview API,
so the benchmark stays in sync with dashboard edits.

Configuration comes from env vars (put them in a repo-root .env and
`set -a; source .env` before running, or pass CLI flags):
  BENCH_HOST                workspace host, e.g. my-workspace.cloud.databricks.com
  BENCH_PROFILE             databricks CLI auth profile (default: DEFAULT)
  BENCH_DASHBOARD_ID        Lakeview dashboard whose datasets define the scenarios
  BENCH_WAREHOUSES          name=id pairs, comma-separated, e.g. reyden=abc,starter=def
  BENCH_FILTER_WORKSPACES   comma-separated workspace names for the workspace-filter
                            scenario (scenario is skipped when unset)

Usage:
  uv run bench_warehouses.py                          # env-configured warehouses, 2 runs
  uv run bench_warehouses.py --runs 3
  uv run bench_warehouses.py --warehouse other=<id>   # add another warehouse
"""
import argparse
import csv
import datetime
import json
import os
import pathlib
import statistics
import subprocess
import threading
import time
import uuid

import requests

DEFAULT_HOST = os.environ.get("BENCH_HOST")
DEFAULT_PROFILE = os.environ.get("BENCH_PROFILE", "DEFAULT")
DEFAULT_DASHBOARD_ID = os.environ.get("BENCH_DASHBOARD_ID")
DEFAULT_WAREHOUSES = dict(
    pair.split("=", 1) for pair in os.environ.get("BENCH_WAREHOUSES", "").split(",") if "=" in pair
)
FILTER_WORKSPACES = [w.strip() for w in os.environ.get("BENCH_FILTER_WORKSPACES", "").split(",") if w.strip()]

# Widget-shaped scenario templates. {ds} is replaced with the dataset SQL
# (with :params substituted). Wrappers mirror how Lakeview applies widget
# aggregations and filter predicates on top of a dataset.
SCENARIOS = [
    # -- page defaults -------------------------------------------------------
    ("overview", "kpi_counters", "ds_kpi", "SELECT * FROM ({ds}) t", {}),
    ("overview", "daily_area_by_product", "ds_daily",
     "SELECT usage_date, product, SUM(list_cost) AS c FROM ({ds}) t GROUP BY 1, 2", {}),
    ("overview", "product_pie", "ds_daily",
     "SELECT product, SUM(list_cost) AS c FROM ({ds}) t GROUP BY 1", {}),
    ("overview", "top_workspaces_bar", "ds_workspaces",
     "SELECT workspace, SUM(list_cost) AS c FROM ({ds}) t GROUP BY 1", {}),
    ("overview", "top_skus_bar", "ds_sku",
     "SELECT sku, compute_mode, SUM(list_cost) AS c FROM ({ds}) t GROUP BY 1, 2", {}),
    ("trends", "cost_by_grain_week", "ds_grain",
     "SELECT period, product, SUM(list_cost) AS c FROM ({ds}) t GROUP BY 1, 2", {"grain": "WEEK"}),
    ("trends", "dbu_line_week", "ds_grain",
     "SELECT period, SUM(dbus) AS d FROM ({ds}) t GROUP BY 1", {"grain": "WEEK"}),
    ("trends", "tag_status_monthly", "ds_tags",
     "SELECT month, tag_status, SUM(list_cost) AS c FROM ({ds}) t GROUP BY 1, 2", {}),
    ("trends", "mom_table", "ds_monthly_product", "SELECT * FROM ({ds}) t", {}),
    ("breakdowns", "workspaces_table", "ds_workspaces", "SELECT * FROM ({ds}) t", {}),
    ("breakdowns", "identities_table", "ds_run_as", "SELECT * FROM ({ds}) t", {}),
    ("breakdowns", "skus_table", "ds_sku", "SELECT * FROM ({ds}) t", {}),
    # -- filter variants -----------------------------------------------------
    ("filters", "daily_area_product_sql_apps", "ds_daily",
     "SELECT usage_date, product, SUM(list_cost) AS c FROM ({ds}) t "
     "WHERE product IN ('SQL', 'APPS') GROUP BY 1, 2", {}),
    ("filters", "daily_area_serverless_only", "ds_daily",
     "SELECT usage_date, compute_mode, SUM(list_cost) AS c FROM ({ds}) t "
     "WHERE compute_mode = 'Serverless' GROUP BY 1, 2", {}),
    ("filters", "daily_area_last_14d", "ds_daily",
     "SELECT usage_date, product, SUM(list_cost) AS c FROM ({ds}) t "
     "WHERE usage_date >= CURRENT_DATE - INTERVAL 14 DAYS GROUP BY 1, 2", {}),
    ("filters", "grain_day", "ds_grain",
     "SELECT period, product, SUM(list_cost) AS c FROM ({ds}) t GROUP BY 1, 2", {"grain": "DAY"}),
    ("filters", "grain_month", "ds_grain",
     "SELECT period, product, SUM(list_cost) AS c FROM ({ds}) t GROUP BY 1, 2", {"grain": "MONTH"}),
    ("filters", "grain_quarter_product_filter", "ds_grain",
     "SELECT period, product, SUM(list_cost) AS c FROM ({ds}) t "
     "WHERE product IN ('JOBS', 'MODEL_SERVING') GROUP BY 1, 2", {"grain": "QUARTER"}),
    # workspace-filter scenario is appended in build_queries() when
    # BENCH_FILTER_WORKSPACES is set (workspace names are account-specific).
    ("filters", "sku_table_classic_only", "ds_sku",
     "SELECT * FROM ({ds}) t WHERE compute_mode = 'Classic'", {}),
]


def get_token(profile: str) -> str:
    out = subprocess.run(["databricks", "auth", "token", "--profile", profile],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)["access_token"]


def fetch_datasets(host: str, token: str, dashboard_id: str) -> dict:
    r = requests.get(f"https://{host}/api/2.0/lakeview/dashboards/{dashboard_id}",
                     headers={"Authorization": f"Bearer {token}"}, timeout=60)
    r.raise_for_status()
    serialized = json.loads(r.json()["serialized_dashboard"])
    datasets = {}
    for ds in serialized["datasets"]:
        sql_text = "".join(l if l.endswith("\n") else l + "\n" for l in ds["queryLines"])
        defaults = {}
        for p in ds.get("parameters", []):
            vals = p.get("defaultSelection", {}).get("values", {}).get("values", [])
            if vals:
                defaults[p["keyword"]] = vals[0]["value"]
        datasets[ds["name"]] = {"sql": sql_text, "param_defaults": defaults}
    return datasets


def build_queries(datasets: dict) -> list[dict]:
    scenarios = list(SCENARIOS)
    if FILTER_WORKSPACES:
        in_list = ", ".join("'" + w.replace("'", "''") + "'" for w in FILTER_WORKSPACES)
        scenarios.append(
            ("filters", f"workspaces_table_{len(FILTER_WORKSPACES)}_selected", "ds_workspaces",
             f"SELECT * FROM ({{ds}}) t WHERE workspace IN ({in_list})", {}))
    queries = []
    for page, name, ds_name, template, params in scenarios:
        ds = datasets.get(ds_name)
        if ds is None:
            print(f"!! skipping {page}:{name} — dataset {ds_name} not in dashboard")
            continue
        ds_sql = ds["sql"]
        merged = {**ds["param_defaults"], **params}
        for kw, val in merged.items():
            ds_sql = ds_sql.replace(f":{kw}", "'" + str(val).replace("'", "''") + "'")
        queries.append({"page": page, "scenario": name, "dataset": ds_name,
                        "sql": template.replace("{ds}", ds_sql)})
    return queries


def exec_statement(host, token, wh_id, stmt):
    """Run a statement via the SQL Statements REST API (works on all warehouse
    types, incl. REYDEN, unlike the Thrift connector). Long-polls with
    wait_timeout so wall-clock is accurate for queries under 50s; falls back
    to tight polling beyond that. Returns (state, statement_id, rows, error)."""
    headers = {"Authorization": f"Bearer {token}"}
    body = {"warehouse_id": wh_id, "statement": stmt, "wait_timeout": "50s",
            "format": "JSON_ARRAY", "disposition": "INLINE", "row_limit": 100000}
    r = requests.post(f"https://{host}/api/2.0/sql/statements",
                      headers=headers, json=body, timeout=120).json()
    sid = r.get("statement_id")
    state = r.get("status", {}).get("state", "FAILED")
    while state in ("PENDING", "RUNNING"):
        time.sleep(0.25)
        r = requests.get(f"https://{host}/api/2.0/sql/statements/{sid}",
                         headers=headers, timeout=60).json()
        state = r.get("status", {}).get("state", "FAILED")
    rows = (r.get("manifest") or {}).get("total_row_count")
    error = (r.get("status", {}).get("error") or {}).get("message") if state != "SUCCEEDED" else None
    return state, sid, rows, error


def run_warehouse(host, token, wh_name, wh_id, queries, runs, results, lock):
    # Warm-up: spin up the warehouse and warm the data path; excluded from results.
    t0 = time.perf_counter()
    state, _, _, err = exec_statement(host, token, wh_id, "SELECT 1")
    if state != "SUCCEEDED":
        print(f"[{wh_name}] FATAL: warm-up failed: {err}")
        return
    print(f"[{wh_name}] warehouse up in {time.perf_counter() - t0:.1f}s; warming caches...")
    exec_statement(host, token, wh_id, f"/* bench-warmup {uuid.uuid4()} */ {queries[1]['sql']}")
    print(f"[{wh_name}] warm-up done, starting {runs} run(s) x {len(queries)} queries")

    for run in range(1, runs + 1):
        for q in queries:
            # Unique comment defeats text-keyed result caches; from_cache is
            # double-checked later via query-history metrics.
            stmt = f"/* bench {uuid.uuid4().hex[:12]} */ {q['sql']}"
            t0 = time.perf_counter()
            state, sid, rows, error = exec_statement(host, token, wh_id, stmt)
            wall_ms = (time.perf_counter() - t0) * 1000
            rec = {"warehouse": wh_name, "warehouse_id": wh_id, "run": run,
                   "page": q["page"], "scenario": q["scenario"], "dataset": q["dataset"],
                   "wall_ms": round(wall_ms, 1), "rows": rows,
                   "query_id": sid, "error": error}
            with lock:
                results.append(rec)
            print(f"[{wh_name}] run{run} {q['page']}:{q['scenario']}: "
                  f"{rec['wall_ms']:.0f}ms" + (f" ERROR: {error[:120]}" if error else ""))


def enrich_from_history(host, token, results, start_ms):
    """Attach server-side metrics from the query history API, matched by query_id."""
    by_id = {r["query_id"]: r for r in results if r.get("query_id")}
    if not by_id:
        return
    params = {
        "filter_by.query_start_time_range.start_time_ms": start_ms,
        "include_metrics": "true",
        "max_results": 100,
    }
    seen = 0
    for _ in range(50):  # pagination guard
        r = requests.get(f"https://{host}/api/2.0/sql/history/queries",
                         headers={"Authorization": f"Bearer {token}"}, params=params, timeout=60)
        if r.status_code != 200:
            print(f"!! query history fetch failed ({r.status_code}); server metrics omitted")
            return
        body = r.json()
        for q in body.get("res", []):
            rec = by_id.get(q.get("query_id"))
            if rec is not None:
                m = q.get("metrics", {}) or {}
                rec["server_total_ms"] = m.get("total_time_ms") or q.get("duration")
                rec["server_exec_ms"] = m.get("execution_time_ms")
                rec["server_compile_ms"] = m.get("compilation_time_ms")
                rec["read_bytes"] = m.get("read_bytes")
                rec["from_cache"] = m.get("result_from_cache")
                seen += 1
        if not body.get("has_next_page"):
            break
        params["page_token"] = body["next_page_token"]
        params.pop("filter_by.query_start_time_range.start_time_ms", None)
        params.pop("include_metrics", None)
        params["include_metrics"] = "true"
    print(f"server-side metrics matched for {seen}/{len(by_id)} queries")


def summarize(results, wh_names):
    def med(vals):
        vals = [v for v in vals if v is not None]
        return statistics.median(vals) if vals else None

    keys = []
    for r in results:
        k = (r["page"], r["scenario"])
        if k not in keys:
            keys.append(k)

    lines = ["| Page | Scenario | " + " | ".join(f"{w} (ms)" for w in wh_names) +
             " | speedup |",
             "|---|---|" + "---|" * (len(wh_names) + 1)]
    ratios = []
    for page, scen in keys:
        meds = {}
        for w in wh_names:
            meds[w] = med([r["wall_ms"] for r in results
                           if r["page"] == page and r["scenario"] == scen
                           and r["warehouse"] == w and not r["error"]])
        cells = [f"{meds[w]:.0f}" if meds[w] is not None else "ERR" for w in wh_names]
        ratio = None
        if len(wh_names) == 2 and meds[wh_names[0]] and meds[wh_names[1]]:
            ratio = meds[wh_names[1]] / meds[wh_names[0]]
            ratios.append(ratio)
        lines.append(f"| {page} | {scen} | " + " | ".join(cells) +
                     f" | {f'{ratio:.1f}x' if ratio else '-'} |")
    if ratios:
        geo = statistics.geometric_mean(ratios)
        lines.append(f"\nGeometric-mean speedup of `{wh_names[0]}` over `{wh_names[1]}`: "
                     f"**{geo:.1f}x** across {len(ratios)} scenarios "
                     f"(min {min(ratios):.1f}x, max {max(ratios):.1f}x). "
                     f"Medians of per-scenario wall-clock over all runs.")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--dashboard-id", default=DEFAULT_DASHBOARD_ID)
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--warehouse", action="append", metavar="NAME=ID",
                    help="warehouse to test (repeatable); default: reyden + starter")
    ap.add_argument("--out-dir", default="bench_results")
    args = ap.parse_args()

    warehouses = (dict(w.split("=", 1) for w in args.warehouse)
                  if args.warehouse else dict(DEFAULT_WAREHOUSES))
    missing = [(f, v) for f, v in [("BENCH_HOST/--host", args.host),
                                   ("BENCH_DASHBOARD_ID/--dashboard-id", args.dashboard_id),
                                   ("BENCH_WAREHOUSES/--warehouse", warehouses)] if not v]
    if missing:
        raise SystemExit("missing configuration: " + ", ".join(f for f, _ in missing) +
                         " (set env vars in .env or pass flags — see module docstring)")

    token = get_token(args.profile)
    datasets = fetch_datasets(args.host, token, args.dashboard_id)
    queries = build_queries(datasets)
    print(f"{len(queries)} scenarios x {args.runs} runs x {len(warehouses)} warehouses "
          f"({', '.join(f'{k}={v}' for k, v in warehouses.items())})\n")

    start_ms = int(time.time() * 1000)
    results, lock = [], threading.Lock()
    threads = [threading.Thread(target=run_warehouse, name=name,
                                args=(args.host, token, name, wid, queries, args.runs, results, lock))
               for name, wid in warehouses.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    time.sleep(10)  # let query history catch up
    enrich_from_history(args.host, get_token(args.profile), results, start_ms)

    out = pathlib.Path(args.out_dir) / datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(results, indent=2))
    fields = ["warehouse", "warehouse_id", "run", "page", "scenario", "dataset", "wall_ms",
              "rows", "query_id", "server_total_ms", "server_exec_ms", "server_compile_ms",
              "read_bytes", "from_cache", "error"]
    with open(out / "results.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(results)

    summary = summarize(results, list(warehouses.keys()))
    (out / "summary.md").write_text(summary + "\n")
    print("\n" + summary)
    print(f"\nwrote {out}/results.csv, results.json, summary.md")

    errors = [r for r in results if r["error"]]
    if errors:
        print(f"\n!! {len(errors)} queries errored; see results.json")


if __name__ == "__main__":
    main()
