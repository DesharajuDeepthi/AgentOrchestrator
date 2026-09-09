# Data lifecycle

What lives where, how it gets there, what deletes it, and what to back up.

## Where the data lives

Two named Docker volumes (`docker-compose.yml`):

| Volume | Mounted at | Contents |
|---|---|---|
| `pgdata` | `db:/var/lib/postgresql/data` | Everything Postgres: the four registries (`mcp_servers`, `tools`, `skills`, `sub_agents`), `conversations` / `runs` / `run_steps`, `app_settings`, Alembic version table, and the LangGraph checkpointer tables |
| `workspace` | `backend:/workspace` | The sandbox root of the seeded `filesystem` MCP server — files that runs read/write via file-ops live here, and only here |

Nothing else in the stack is stateful. The frontend container holds only the built SPA; Redis (optional profile) holds disposable cache blobs (`concierge:cache:*`) that are rebuilt read-through from Postgres.

## stop vs decom

- `./stop.sh` → `docker compose stop`: containers, network, and both volumes stay. `./start.sh` resumes with the same data.
- `./decom.sh` → `docker compose down -v --remove-orphans`: containers, network, **and both data volumes** are destroyed — registries, run history, checkpoints, and every workspace file. Confirmation is prompted (`-y` / `--yes` skips). Images survive.

Losses that are in-memory even without decom: the SSE event history (`RunEventBus`) and the MCP client sessions vanish on any backend restart; run rows, steps, and checkpoints in Postgres do not.

## First start and re-seeding

On every backend startup (`backend/app/main.py` lifespan), in order:

1. `alembic upgrade head` — creates or migrates the schema. First start on an empty volume builds it from scratch; no manual step.
2. `seed_all(session)` — idempotent static seed (spec §9, `backend/app/seed/loader.py`): upserts match on `(source='static', name)` (or `tool_key` for tools), so **ids stay stable across reloads**. Seeds: the `fetch` (`uvx --with 'mcp<2' mcp-server-fetch`) and `filesystem` (`npx -y @modelcontextprotocol/server-filesystem /workspace`) stdio MCP servers, the native skills from `backend/app/native/skills/*.skill.md` (`web-research`, `file-ops`), the native tools (including `summarize-and-structure` and `render_chart`), and the `research-concierge` sub agent.
3. `AsyncPostgresSaver.setup()` — creates the LangGraph checkpoint tables if absent.
4. Registry cache startup (mode read + warm load if stateful), MCP manager connect-all, embeddings backfill — the last two as background tasks that don't block readiness.

Re-seeding on demand: `POST /api/v1/seed/reload` (Settings → Data → seed-reload). On a **fresh slate** (after `decom.sh`) the startup seed does the same thing automatically. Re-seeding an existing DB restores static record *definitions* to their shipped state (upsert), reconciles native tool/skill registrations, and re-resolves skill→tool bindings that were waiting on MCP ingest; it never touches dynamic (UI-created) records and it invalidates all four cache registries when called via the endpoint. Dynamic MCP servers, skills, and sub agents survive restarts and reseeds — the DB is the source of truth, not memory.

## Run / trace / event growth

Every chat message creates one `runs` row and, per executed step, a `run_steps` row carrying input/output payloads, tokens, and timings — this is the always-on trace store, and it grows without bound: there is no retention job, no TTL, no automatic pruning.

Purging (the real, existing surfaces — nothing else):

| Action | Surface | Deletes |
|---|---|---|
| Delete one run | Runs page delete, or `DELETE /api/v1/runs/{run_id}` | That run + its steps + its in-memory SSE history + its LangGraph checkpoint rows (orchestrator thread `run_id` and worker threads `run_id:*`). 409 while `running` — cancel first |
| Purge all history | Settings → Data → run-history purge (confirm), or `DELETE /api/v1/runs` | All `runs` and `run_steps` rows + all in-memory SSE history + all LangGraph checkpoint rows |

Conversations are not deleted by the purge (the rows in `conversations` remain; there is no conversation-delete endpoint). SSE event history is memory-only and additionally disappears on backend restart regardless of purging.

## Checkpoints (LangGraph)

- **Where**: Postgres, in the tables created by `AsyncPostgresSaver.setup()` (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`) inside the same `pgdata` volume — configured in `backend/app/db.py` over a psycopg pool derived from `DATABASE_URL`.
- **When written**: continuously during graph execution. The orchestrator thread is keyed `thread_id = run_id`; dispatched workers checkpoint under their own worker thread ids (the HITL interrupt payload carries `worker_thread` so the runner can inspect gate state on resume).
- **HITL resume**: `POST /runs/{id}/hitl` restarts the run task with a LangGraph `Command(resume=...)` against the same `thread_id` — the checkpoint **is** the pause state. This is why HITL survives a backend restart: a `paused_hitl` run can be resumed after reboot because nothing about the pause lives in process memory.
- **Retry**: `POST /runs/{id}/retry` does *not* resume a checkpoint — it creates a brand-new run (new id, new thread) and re-plans from the original message.
- **Cancellation**: the checkpoint is deliberately retained for inspection after cancel.
- **Cleanup**: checkpoint rows ride the run lifecycle. `DELETE /api/v1/runs/{id}` removes the run's checkpoint rows (thread `run_id` plus worker threads `run_id:*`), and the full purge empties all three saver tables (`app/api/runs.py::_purge_checkpoints`). A cancelled-but-undeleted run keeps its checkpoint for inspection until the run itself is deleted.

## Backup

The database is the whole application state — back it up with `pg_dump` against the volume-backed DB:

```bash
# logical dump (compose defaults: user/db "concierge")
docker compose exec db pg_dump -U concierge -d concierge -Fc > concierge-$(date +%F).dump

# restore into a fresh stack (after ./start.sh has created the schema, or into an empty db)
docker compose exec -T db pg_restore -U concierge -d concierge --clean --if-exists < concierge-YYYY-MM-DD.dump
```

Also copy the `workspace` volume if run-produced files matter:

```bash
docker compose cp backend:/workspace ./workspace-backup
```

**What is NOT in the DB** — a dump alone does not restore a working system:

- **Env secrets and wiring**: provider API keys (`ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `OPENAI_API_KEY`, `LANGSMITH_API_KEY`), `REDIS_URL`, `DATABASE_URL`, ports — all env-only by policy. Back up `.env` separately and store it like the secret it is (it is git-ignored; never commit it).
- **Native skill files**: `backend/app/native/skills/*.skill.md` and the `@native_tool` / `@native_sub_agent` code registrations — these live in the repo/image and are re-registered at startup.
- **Prompts**: `backend/app/prompts/*.md` — repo files, never DB rows.
- **Redis cache contents**: disposable by contract; rebuilt read-through.

Restore procedure on a new host: check out the repo, restore `.env`, `./build.sh`, `./start.sh`, then `pg_restore` over the seeded database and restart the backend (`docker compose restart backend`) so the MCP manager and cache warm up against the restored registries.
