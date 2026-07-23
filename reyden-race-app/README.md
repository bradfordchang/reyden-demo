# Reyden Query Race

Databricks App that races any AI/BI dashboard's queries live on two SQL
warehouses: a Reyden warehouse you pick vs the warehouse the dashboard is
configured to run on.

- **Backend**: FastAPI + databricks-sdk. Lists the AI/BI (Lakeview) dashboards
  the signed-in user can access, pulls the chosen dashboard's dataset queries
  live (default parameter values applied), and races them cache-busted via the
  SQL Statement Execution API — both lanes synchronized with a start barrier
  after warm-up. The Reyden warehouse picker shows every `warehouse_type:
  REYDEN` warehouse visible to the user.
- **Auth**: on-behalf-of-user for warehouses and the race queries (the user
  token Apps forwards in `x-forwarded-access-token`), so query results reflect
  the user's own permissions and the SP needs no warehouse or data access.
  **Interim**: Lakeview reads (dashboard list/detail) fall back to the app's
  service principal because Apps user authorization has no Lakeview scope yet
  — share dashboards with the app SP to make them raceable (see Known
  limitation below). Locally the default SDK auth chain is used
  (`DATABRICKS_CONFIG_PROFILE`) — you are the user.
- **Frontend**: static HTML/CSS/JS (no build step), polls `/api/race/{id}`
  every 500ms and extrapolates in-flight timers client-side.

## Configuration

None. Dashboards, warehouses, and scenarios are all discovered at runtime as
the signed-in user — `app.yaml` is just the uvicorn command.

## Local dev

```bash
DATABRICKS_CONFIG_PROFILE=<profile> uv run uvicorn app.main:app --reload --port 8321
```

## Deploy

Deploy straight from this public Git repository — no workspace sync needed:

```bash
databricks apps create <app-name> --profile <profile>   # once — no resources needed

# once: link the app to the repo
databricks apps update <app-name> --profile <profile> --json '{
  "git_repository": {"provider": "gitHub", "url": "https://github.com/bradfordchang/reyden-demo"},
  "user_api_scopes": ["sql"]
}'

# every deploy: snapshot of main, app source in the reyden-race-app/ subdirectory
databricks apps deploy <app-name> --profile <profile> --json '{
  "mode": "SNAPSHOT",
  "git_source": {"branch": "main", "source_code_path": "reyden-race-app"}
}'
```

(Workspace-path deploys still work too: `databricks sync . /Workspace/Users/<you>/reyden-race-app`
then `databricks apps deploy <app-name> --source-code-path ...`.)

Then enable **user authorization** on the app with the `sql` API scope
(Compute → Apps → the app → Edit → Authorization → add scope), or via the CLI:

```bash
# NOTE: `apps update` is a full replace — this intentionally clears any
# leftover resources from older versions (the SP no longer needs them).
databricks apps update <app-name> --profile <profile> --json '{"user_api_scopes": ["sql"]}'
```

Each app user consents to the scopes on first visit; races run with their
identity, so they need CAN USE on the warehouses involved and SELECT on
whatever the dashboard queries — exactly what they'd need to view the
dashboard itself.

### Known limitation: Lakeview scope needs an account admin (as of Jul 2026)

The Apps user-authorization scope list does not yet include a scope for the
Lakeview API, even though `sql` covers warehouses and query execution.
Neither `dashboards.lakeview` nor `dashboards` is accepted as a
`user_api_scopes` value, and the workspace setting
`allowedAppsUserApiScopes` (even set to `"*"`) cannot widen the hardcoded set.

**Interim behavior**: the app falls back to its service principal for
Lakeview reads, so the dashboard picker shows dashboards **shared with the
app** (grant the SP `CAN_READ` per dashboard, via the dashboard Share dialog
or `databricks api patch /api/2.0/permissions/dashboards/<id>`). The race
itself still runs as the user. The fallback disables itself automatically
once the user token carries the Lakeview scope — to get there:

One-time fix, **account admin** required: add `dashboards.lakeview` to the
app's OAuth integration — every app has one; find its id in
`oauth2_app_integration_id` from `databricks apps get <app-name>`:

```bash
databricks account custom-app-integration get <integration-id>       # note existing scopes
databricks account custom-app-integration update <integration-id> \
  --json '{"scopes": [<existing scopes...>, "dashboards.lakeview"]}'  # update OVERWRITES — include existing
```

After the change, users must sign out of the app
(`https://<app-host>/.auth/sign_out`) or use a fresh browser session to pick
up the new scope. Beware: a later `databricks apps update` that touches
`user_api_scopes` may regenerate the integration scopes and drop the custom
one — re-apply if dashboards 403 again.

Dependencies: uv path only — `pyproject.toml` + `uv.lock`, no `requirements.txt`
(uv-based apps get no pre-installed packages, so everything is declared here).
If a deploy fails on a package download (flaky workspace PyPI proxy), retry
the deploy.
