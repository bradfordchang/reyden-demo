# Reyden Performance Demo

Tools for demonstrating the query performance of [Reyden](https://docs.databricks.com/aws/en/compute/sql-warehouse/real-time),
the engine behind Databricks' real-time SQL warehouses (Lakehouse//RT), against
a classic serverless SQL warehouse — using the queries of a real AI/BI dashboard
rather than synthetic benchmarks.

## What's here

- **[`reyden-race-app/`](reyden-race-app/)** — a Databricks App that live-races
  any AI/BI dashboard you can access: its dataset queries run cache-busted,
  head-to-head, on a Reyden warehouse vs the dashboard's own warehouse, with
  animated timings, on-behalf-of user authorization, and a final speedup verdict.
- **[`bench_warehouses.py`](bench_warehouses.py)** — a scriptable benchmark that
  replays a dashboard's widget-shaped queries (page defaults plus filter and
  time-grain-parameter variants) across any set of warehouses via the SQL
  Statement Execution API, with result caches defeated and server-side metrics
  pulled from the query history API. Configured entirely via env vars — see the
  module docstring.

## Methodology notes

Both tools measure wall-clock time to results-ready for statements shaped
exactly like the queries Lakeview dashboards execute: aggregation wrappers over
dataset SQL, with default parameter values applied. Every statement carries a
unique tag so no result cache is hit; warehouses are warmed up before timing
begins so cold starts never skew a lap. Reported speedups are medians across
runs and geometric means across scenarios.
