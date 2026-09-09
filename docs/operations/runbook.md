# Runbook — day-2 operations

Everything here is grounded in the repo's lifecycle scripts, `docker-compose.yml`, and the backend code. Run all scripts from the repo root (each script does `cd "$(dirname "$0")"`, so they self-correct, but `docker compose ...` typed by hand must run from the repo root where `docker-compose.yml` and `.env` live).

## Start / stop / restart / decommission

| Action | Command | Preserves | Destroys |
|---|---|---|---|
| First-time setup | `./quick-setup.sh` (flags: `--key sk-ant-...`, `--redis`, `--no-redis`) | Existing `.env` values it does not touch | Nothing |
| Build images | `./build.sh` | Everything | Nothing (rebuilds `backend` + `frontend` images) |
| Start / resume | `./start.sh` | All data (named volumes `pgdata`, `workspace`) | Nothing |
| Stop | `./stop.sh` | Containers, network, volumes — `./start.sh` resumes with the same data | Nothing |
| Decommission | `./decom.sh` (prompts; `-y` / `--yes` to skip) | Docker images | Containers, network, **both data volumes** (Postgres data, `/workspace` files) |

Notes from the scripts themselves:

- `quick-setup.sh` creates `.env` from `.env.example`, prompts for `ANTHROPIC_API_KEY` (hidden input; `--key <value>` non-interactive), optionally provisions Redis (`--redis` writes `REDIS_URL=redis://redis:6379/0` and `COMPOSE_PROFILES=redis`; `--no-redis` blanks both), then runs `uv sync` (backend) and `npm install` (frontend) if those tools exist. Safe to re-run.
- `start.sh` is idempotent: `docker compose up -d` pulls/builds missing images, creates missing containers, restarts stopped ones, no-ops on running ones. It then polls `http://localhost:${BACKEND_PORT:-8000}/health` for up to ~120 s (60 × 2 s) and prints the frontend/API/health URLs. Migrations (`alembic upgrade head`) and idempotent seed load run inside backend startup — there is no separate migration step.
- `decom.sh` runs `docker compose down -v --remove-orphans`. The next `./start.sh` is a clean slate: fresh schema, seeds reloaded. Rebuild images with `./build.sh` only after code changes.
- Restart = `./stop.sh && ./start.sh` (or `docker compose restart backend` for the backend alone).

## Health checks

| Check | Command | Healthy looks like |
|---|---|---|
| Backend liveness | `curl -s http://localhost:8000/health` | `{"status":"ok"}` |
| DB container | `docker compose ps db` | `healthy` (compose healthcheck: `pg_isready -U concierge`, 3 s interval, 20 retries) |
| All containers | `docker compose ps` | `db` healthy, `backend` and `frontend` `Up` |
| Backend logs | `docker compose logs -f backend` | JSON structlog lines; `registry_cache_started`, `mcp_connected` per seeded server at startup |
| Metrics | `curl -s http://localhost:8000/metrics` | Prometheus text (`concierge_runs_total`, `concierge_steps_total`, …) |
| Cache | `curl -s http://localhost:8000/api/v1/cache/status` | `{"mode": "...", "registries": {tools|skills|sub_agents|settings: {records, generation, loaded_at, cached}}}` |
| MCP servers | `GET /api/v1/mcp-servers` | `status: "active"`, recent `last_connected_at`, `last_error: null` |

The `backend` compose service has no container-level healthcheck; `/health` is the probe. `frontend` is nginx serving the built SPA and proxying `/api/` and `/metrics` to `backend:8000` (see `frontend/nginx.conf`).

## Common operational tasks

All Settings changes are `PATCH /api/v1/settings` under the hood (Settings page in the UI). They apply to the **next run** — no restart; `log_level` and `otlp_endpoint` apply even sooner, the moment the PATCH returns (see `configuration.md`).

### Flip orchestrator mode

Settings → Orchestrator → mode toggle, or:

```bash
curl -X PATCH http://localhost:8000/api/v1/settings \
  -H 'content-type: application/json' -d '{"orchestrator_mode": "agentic"}'   # or "graph"
```

### Change models / effort

Settings → Models: `default_model`, `planner_model`, `aggregator_model` (`provider:model` strings; planner/aggregator `null` fall back to default) plus `*_model_params` (`{effort: none|low|medium|high, temperature, max_output_tokens}`). Validation is at save time: an unconfigured provider or an unsupported param combination returns 422. Setting `*_model_params` requires the matching model key to be set.

```bash
curl -X PATCH http://localhost:8000/api/v1/settings \
  -H 'content-type: application/json' \
  -d '{"planner_model": "openai:gpt-5.6-terra", "planner_model_params": {"effort": "high"}}'
```

### Flip registry cache mode (bypass ↔ memory ↔ redis)

Settings → Registry cache, or `PATCH /settings {"registry_cache_mode": "..."}`. Applied live — the settings write path invalidates the settings registry, and the cache re-reads its own mode (`registry_cache.py`, `_mark_dirty`).

- `bypass` (default): every read is a direct Postgres query. The rollback lever — flip here first when cache behavior is suspect.
- `memory`: per-process store, reload-on-dirty; flipping into it warm-loads all four registries.
- `redis`: requires `REDIS_URL` in env (else the save is rejected with 422) **and** the save pings Redis, rejecting with `redis unreachable: ...` if the ping fails. Provision with `./quick-setup.sh --redis` + restart the stack so the `redis` compose profile service starts.

When to flip: `memory`/`redis` cut per-model-call registry queries; `bypass` restores pre-cache byte-identical semantics if you suspect staleness.

### Force a cache refresh

UI refresh buttons (Tools/Skills/Sub Agents pages and Settings → Registry cache → Refresh all), or:

```bash
curl -X POST http://localhost:8000/api/v1/cache/refresh/all      # or /tools /skills /sub_agents /settings
curl -s http://localhost:8000/api/v1/cache/status
```

This is an operator override, never a correctness requirement — every write path already invalidates before returning.

### Handle a dead MCP server

Containment (by construction, `backend/app/factory/worker.py` `_make_mcp_proxy`): MCP tools are lazy proxies that resolve the live session at call time. A dead server surfaces as a **tool error inside the run** (error-edge semantics in workers; the process and other runs are unaffected). The health loop (`mcp_health_interval_s`, default 30 s) pings each connection; a failed ping tears the connection down and flips the server row to `status=error` with `last_error="health ping failed"`.

Recovery:

```bash
curl -X POST http://localhost:8000/api/v1/mcp-servers/<id>/reconnect      # retries connect + re-ingests tools
curl -X POST http://localhost:8000/api/v1/mcp-servers/<id>/refresh-tools  # re-runs tools/list on a live connection
```

(Both exist as buttons on the MCP Servers page.) A reconnected server's tools work on the next model call — no rebuild, no restart.

### Triage the HITL queue

Settings → HITL queue lists all runs with `status=paused_hitl` across every conversation (`GET /api/v1/hitl/pending`), resolvable inline. Resolve with:

```bash
curl -X POST http://localhost:8000/api/v1/runs/<run_id>/hitl \
  -H 'content-type: application/json' \
  -d '{"decision": "approve", "note": "", "answers": {"question_id": "value"}}'   # decision: approve | deny
```

`approve` resumes from checkpoint; `deny` routes the gated node to END with the note in state; `answers` feeds form gates. A 409 means the run is not (or no longer) `paused_hitl` — someone else resolved it, or it finished. One POST answers **one** gate; parallel dispatch can pause again immediately for the next gate (the backend re-emits `hitl_request` for the remaining ones).

### Cancel / retry runs

Row/detail buttons on the Runs page, or:

```bash
curl -X POST http://localhost:8000/api/v1/runs/<run_id>/cancel   # cooperative; also resolves a paused_hitl run as cancelled
curl -X POST http://localhost:8000/api/v1/runs/<run_id>/retry    # failed runs only; re-plans from the original message, returns a NEW run_id
```

Cancel is cooperative — the asyncio task is cancelled, in-flight steps are marked `cancelled`, the checkpoint is retained for inspection. Retry returns 409 unless the run's status is `failed`; cancel returns 409 for terminal runs.

### Purge run data

Both exist, for real:

```bash
curl -X DELETE http://localhost:8000/api/v1/runs/<run_id>   # one run (409 if still running — cancel first)
curl -X DELETE http://localhost:8000/api/v1/runs            # everything: all runs + steps (Settings → Data → purge, with confirm)
```

Purge deletes `runs`/`run_steps` rows and drops in-memory SSE event history. It does **not** touch LangGraph checkpoint tables — see `data-lifecycle.md`.

### Reload seed data

```bash
curl -X POST http://localhost:8000/api/v1/seed/reload   # idempotent; invalidates all cache registries
```

(Settings → Data → seed-reload button.)

## Incident quick-reference

| Symptom | First checks | Likely cause |
|---|---|---|
| `./start.sh` times out waiting for health | `docker compose logs backend` | Migration failure, DB not healthy yet, port conflict on `BACKEND_PORT` |
| Runs fail instantly with provider errors | Run detail error text; `GET /api/v1/providers` for configured status | Missing/exhausted API key, provider quota (see `troubleshooting.md`) |
| Run hangs at a tool call, then step errors | MCP Servers page: server `status=error`, `last_error` | Dead/never-connected MCP server (stdio command missing, npx cold start) |
| Registry edit not visible to runs | `GET /api/v1/cache/status` generations; `POST /api/v1/cache/refresh/all` | Should not happen (event invalidation); flip `registry_cache_mode` to `bypass` to rule the cache out |
| Settings save rejected 422 | Response detail string | Validation working as designed: bad `provider:model`, unsupported params, `redis` without `REDIS_URL`/reachable Redis |
| HITL resolve returns 409 | `GET /api/v1/hitl/pending`; run status | Gate already resolved elsewhere, or run reached a terminal state |
| Chat stream shows nothing after backend restart | Run status via `GET /api/v1/runs/{id}` | SSE history is in-memory; a restart loses replay for old runs (run rows/traces persist in the DB) |
| Everything gone after maintenance | Was `./decom.sh` run instead of `./stop.sh`? | `decom.sh` deletes the data volumes by design |
