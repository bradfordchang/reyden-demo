# Reyden Query Race

Databricks App that live-races identical AI/BI dashboard queries on two SQL
warehouses (Reyden vs Serverless Starter) and animates the results.

- **Backend**: FastAPI + databricks-sdk. Queries run via the SQL Statement
  Execution API, cache-busted, both lanes synchronized with a start barrier
  after warm-up. In Databricks Apps, auth uses the injected M2M OAuth service
  principal; warehouse IDs come from app resources via `valueFrom` in app.yaml.
- **Frontend**: static HTML/CSS/JS (no build step), polls `/api/race/{id}`
  every 500ms and extrapolates in-flight timers client-side.
- **Scenarios**: `app/scenarios.json`, generated from the live dashboard's
  datasets. Regenerate after dashboard changes with the snippet in the repo
  root (see `bench_warehouses.py` — `fetch_datasets` + `build_queries`).

## Configuration

All environment-specific values (warehouse IDs, metric view, dashboard URLs,
workspace filter values) come from env vars — see `app.yaml.example`. Copy it
to `app.yaml` (gitignored) for deployment, and create a `.env` (gitignored)
with the same values plus `DATABRICKS_CONFIG_PROFILE` for local dev.

## Local dev

```bash
set -a; source .env; set +a
uv run uvicorn app.main:app --reload --port 8321
```

## Deploy

```bash
databricks apps create <app-name> --profile <profile> --json '{...sql_warehouse resources named reyden-warehouse/starter-warehouse...}'   # once
databricks sync . /Workspace/Users/<you>/reyden-race-app --profile <profile>
# sync skips gitignored files, so upload the real app.yaml explicitly:
databricks workspace import /Workspace/Users/<you>/reyden-race-app/app.yaml --file app.yaml --format AUTO --overwrite --profile <profile>
databricks apps deploy <app-name> --source-code-path /Workspace/Users/<you>/reyden-race-app --profile <profile>
```

The app service principal needs `USE CATALOG`, `USE SCHEMA`, and `SELECT` on
the metric view's catalog/schema, and `CAN_USE` on both warehouses (granted
automatically via app resources).

Dependencies: uv path only — `pyproject.toml` + `uv.lock`, no `requirements.txt`
(uv-based apps get no pre-installed packages, so everything is declared here).
This workspace's PyPI proxy drops connections intermittently; app.yaml sets
UV_HTTP_TIMEOUT/UV_CONCURRENT_DOWNLOADS/UV_HTTP_RETRIES to ride through it —
if a deploy still fails on a download, simply retry the deploy.
