# Troubleshooting

Symptom-indexed. Each entry: symptom / cause / fix. Paths and endpoints are real; `jq` optional.

## Backend won't start

**Symptom**: `./start.sh` prints dots until "backend did not become healthy in time".

- **Cause: DB not ready.** Backend depends on the `db` healthcheck (`pg_isready`), but a corrupted volume or a port clash can stall it.
  **Fix**: `docker compose ps db` (want `healthy`); `docker compose logs db`. If the volume is wrecked and the data is disposable: `./decom.sh -y && ./start.sh`.
- **Cause: migration failure.** Migrations run inside backend startup (`main.py` lifespan → `alembic upgrade head`); a failure aborts boot.
  **Fix**: `docker compose logs backend` — the alembic traceback is at the top. After code changes, rebuild first: `./build.sh`. A schema that predates a squashed/edited migration only recovers via `./decom.sh` (data loss) or manual `alembic` surgery inside the container.
- **Cause: stale image.** Code changed but images weren't rebuilt.
  **Fix**: `./build.sh && ./start.sh`.

## Provider errors surfaced in runs

Run detail (Runs page or `GET /api/v1/runs/{id}`) stores the error verbatim; retry with `POST /api/v1/runs/{id}/retry` once the cause is fixed.

- **Symptom**: runs fail with authentication/credit errors from Anthropic (seen in the acceptance campaign as "credit exhaustion" mid-run).
  **Cause**: `ANTHROPIC_API_KEY` missing, invalid, or the account is out of credits.
  **Fix**: check `GET /api/v1/providers` (`configured: true/false`); fix the key in `.env` (`./quick-setup.sh --key sk-ant-...`), `docker compose up -d backend` to reload env, retry the run. Keys are env-only — there is nothing to fix in the UI or DB.
- **Symptom**: OpenAI reasoning model + tools returns a 400 from `/v1/chat/completions`.
  **Cause**: current OpenAI reasoning models reject function tools combined with `reasoning_effort` on the Chat Completions API.
  **Fix**: already handled — the adapter routes any run with `effort` set through the **Responses API** (`use_responses_api=True`, `backend/app/llm/adapters.py`, commit `2fc0615`). If you see this error, you are on a stale image: `./build.sh`. Selecting `effort` on `gpt-4o` is rejected at save (422) because that model declares `supports_effort=False`.
- **Symptom**: Gemini runs die mid-campaign with quota errors.
  **Cause**: Google free-tier keys carry hard daily caps (observed: 20 requests/day/model — see `docs/acceptance/README.md`, stage 19). One multi-step run can burn several requests.
  **Fix**: wait for the daily reset, use a paid-tier key, or point the affected role (planner/aggregator/default) at another provider in Settings → Models.

## MCP server stuck connecting or dead

- **Symptom**: server row shows `status=error`, `last_error: "connection timed out"` right after registration.
  **Cause**: stdio command missing in the backend image (`command` must exist inside the container), or `npx`/`uvx` cold start exceeding the 25 s connect timeout (`CONNECT_TIMEOUT_S`, `backend/app/mcp/manager.py`) while it downloads the package.
  **Fix**: `docker compose exec backend which npx uvx` to confirm launchers exist; hit `POST /api/v1/mcp-servers/{id}/reconnect` (UI button) — a second attempt usually succeeds once the package is in npx's cache. Behind a proxy, note the manager passes `HTTP(S)_PROXY`/CA env through to stdio children.
- **Symptom**: server was fine, now `status=error`, `last_error: "health ping failed"`; runs using its tools fail at the tool call.
  **Cause**: the ping loop (interval `mcp_health_interval_s`, default 30 s) detected a dead session and tore it down. Tool calls are lazy proxies, so they raise "MCP server ... is not connected" as a tool error inside the run — the process and other runs are contained.
  **Fix**: `POST /api/v1/mcp-servers/{id}/reconnect`; then `POST /api/v1/mcp-servers/{id}/refresh-tools` if the server's tool list may have changed. Reconnected tools are usable on the next model call, no restart.

## Redis cache mode rejected

- **Symptom**: `PATCH /settings {"registry_cache_mode": "redis"}` → 422 `registry_cache_mode 'redis' requires REDIS_URL to be set`.
  **Cause**: `REDIS_URL` not in the backend's environment (the UI hides the redis option in this state; the API tells you outright).
  **Fix**: `./quick-setup.sh --redis`, then restart the stack so the env var and the `redis` compose profile take effect.
- **Symptom**: 422 `redis unreachable: ...` on save.
  **Cause**: the save pings Redis (`settings_store._ping_redis`) and refuses if it fails — the redis service isn't running (profile not enabled) or the URL is wrong.
  **Fix**: `docker compose --profile redis up -d redis`; verify `docker compose exec redis redis-cli ping` → `PONG`; save again. The mode is never half-applied — a rejected save leaves the previous mode in place.

## Cache looks stale

- **Symptom**: a registry edit isn't reflected in planner catalogs / agent tools.
  **Cause**: should not happen — every write path invalidates before returning (event-invalidated contract, spec §7.3). Suspect a missed path or cross-replica notify loss.
  **Fix**: check `GET /api/v1/cache/status` — the registry's `generation` counter increments on every invalidation and `loaded_at` shows the last reload. Force it: `POST /api/v1/cache/refresh/{tools|skills|sub_agents|settings|all}` (same as the UI refresh buttons). To rule the cache out entirely, flip `registry_cache_mode` to `bypass` (direct DB reads) and compare. If bypass fixes it, capture the write path that failed to invalidate and file it as a bug.

## SSE stream drops

- **Symptom**: the chat stream goes quiet, browser reconnects, or replay shows nothing.
  **Cause**: keepalive pings are only sent after 120 s of silence (`api/chat.py`) — an intermediary with a shorter idle timeout kills the connection. The shipped nginx is already configured for SSE (`proxy_buffering off`, `proxy_read_timeout 3600s` in `frontend/nginx.conf`); other proxies in front of the stack may not be. Also: event history is **in-memory** — a backend restart erases replay for existing runs, and after a restart the stream for an old run will hang (no history, no terminal event) rather than replay.
  **Fix**: configure any extra proxy with buffering off and a read timeout ≥ 120 s. After a backend restart, read the run's outcome from the Runs page (DB-backed) instead of the stream. Run state is authoritative in Postgres; the stream is presentation.

## HITL card / queue disagreement (409)

- **Symptom**: resolving a HITL card returns 409 `run is <status>, not paused_hitl`, or a card in chat looks armed while Settings → HITL queue is empty.
  **Cause**: exactly one resolution per gate — the gate was already consumed (resolved from the other surface, or the run was cancelled/finished). 409 is the designed answer, not a fault. A UI bug that left cards armed after their gate was consumed was fixed in commit `f686735`.
  **Fix**: refresh; trust `GET /api/v1/hitl/pending` as the source of truth. With parallel dispatch, one approve can immediately pause the run again on the **next** gate — that's a new card, answer it separately.

## Overlap-guard dialog on save

- **Symptom**: saving a skill or sub agent pops a "possible duplicate" confirm dialog; automated flows appear to stall.
  **Cause**: by design — the UI calls `POST /api/v1/skills/check-overlap` / `POST /api/v1/sub-agents/check-overlap` (LLM-as-judge, threshold 70%) before saving. Advisory only; it fails **open** if the judge model is unavailable, and tools are exempt.
  **Fix**: read the verdict (match name + reasoning) and either confirm the save or reuse the existing record. Scripted flows should call the API directly (`POST /skills`, `POST /sub-agents`) — the guard lives in the UI flow, not the save endpoint.

## Port conflicts

- **Symptom**: `docker compose up` fails with "port is already allocated", or `start.sh` health-polls the wrong service.
  **Cause**: host 8000 or 5173 is taken (another dev server, a previous stack), or Redis profile clashing on 127.0.0.1:6379.
  **Fix**: set `BACKEND_PORT` / `FRONTEND_PORT` in `.env` and rerun `./start.sh` (it reads those for the health poll and URLs). The redis port binding is fixed at `127.0.0.1:6379` in `docker-compose.yml` — stop the conflicting local Redis or edit the compose file.

## docker compose run from the wrong directory

- **Symptom**: `no configuration file provided: not found`, or the stack comes up with default env values (keys missing, wrong ports).
  **Cause**: `docker compose` resolves `docker-compose.yml` and `.env` from the current directory. The lifecycle scripts are immune (each does `cd "$(dirname "$0")"`), but hand-typed compose commands are not.
  **Fix**: `cd` to the repo root before any manual `docker compose ...`, or prefer the scripts. If a stack was accidentally created elsewhere with defaults, take it down from that same directory before starting the real one.

## macOS bash 3.2 script issues (fixed)

- **Symptom (historical)**: on macOS, `./decom.sh` always aborted at the confirm prompt and `quick-setup.sh` misbehaved on the replace-key / redis prompts.
  **Cause**: macOS ships bash 3.2; the scripts used `${var,,}` lowercase expansion and negative substring offsets (`${var: -4}`), both bash 4+.
  **Fix**: already fixed in commit `b9829db` — the scripts now use POSIX `case` matching (the `is_yes` helper) and `tail -c 4` for the key suffix, and are safe on stock macOS bash. If you still see it, you are on a checkout older than that commit: `git pull`.
