# Local Development

Two dev loops exist. Use the containerized stack when you want the real thing (acceptance walks, MCP stdio servers, seeds, demos); use the fast loop when you are iterating on backend or frontend code.

## Loop A — full containerized stack

The lifecycle scripts at the repo root are the supported path (all idempotent):

```bash
./quick-setup.sh   # .env from .env.example + ANTHROPIC_API_KEY prompt (--key <value> non-interactive)
                   # + optional Redis cache-profile provisioning (--redis / --no-redis)
                   # + backend `uv sync` and frontend `npm install` for local tooling
./build.sh         # build both docker images
./start.sh         # start db/backend/frontend; waits for backend health, prints URLs
./stop.sh          # stop containers, keep data (named volumes)
./decom.sh         # tear down everything incl. data volumes (asks; -y to skip)
```

Equivalent manual path: `cp .env.example .env && docker compose up`.

- Compose runs exactly three services — `db` (Postgres 16), `backend`, `frontend` (`docker-compose.yml`). A fourth, `redis`, exists behind the `redis` profile only (`docker compose --profile redis up` + `REDIS_URL=redis://redis:6379/0`) and is never required.
- First start runs Alembic migrations and loads seed data automatically (the FastAPI lifespan in `backend/app/main.py` does both); later starts resume with the same data.
- Frontend at `http://localhost:${FRONTEND_PORT}` (default **5173**, nginx-served build), API at `http://localhost:${BACKEND_PORT}` (default **8000**).
- Containers do **not** hot-reload: after code changes, `./build.sh && ./start.sh` (images are rebuilt; data volumes survive).

## Loop B — fast loop (uvicorn + vite)

### Environment

Copy `.env.example` to `.env` once. Everything is documented inline there; the keys that matter for local work: `ANTHROPIC_API_KEY` (or none — see keyless mode below), `DATABASE_URL` (use host `localhost` instead of `db` when running outside compose), `BACKEND_PORT` / `FRONTEND_PORT`, `WORKSPACE_DIR`, optional `GOOGLE_API_KEY` / `OPENAI_API_KEY` / `REDIS_URL` / `LANGSMITH_API_KEY` / `OTEL_EXPORTER_OTLP_ENDPOINT`. Blank values mean "unset" — `backend/app/config.py` treats empty strings as defaults, so a sparse `.env` is fine.

### Backend

```bash
cd backend
uv sync                          # Python 3.12, installs runtime + dev groups (pyproject.toml)
```

The test suite needs a Postgres it can own. The convention (see `backend/tests/conftest.py`) is a throwaway instance on **port 5433**:

```bash
docker run -d -p 5433:5432 -e POSTGRES_PASSWORD=postgres postgres:16
docker exec <container> createdb -U postgres concierge_test
```

or point `TEST_DATABASE_URL` at any instance you control — the default is
`postgresql+asyncpg://postgres:postgres@localhost:5433/concierge_test`. Tests drop/create all tables and `TRUNCATE` between tests, so never point it at a database you care about.

To run the app itself against a local Postgres:

```bash
export DATABASE_URL=postgresql+asyncpg://concierge:concierge@localhost:5432/concierge
uv run uvicorn app.main:app --reload --port 8000
```

Migrations and seeds run automatically at startup (`lifespan` in `backend/app/main.py` calls `alembic upgrade head` then `seed_all`). You can also run `uv run alembic upgrade head` manually from `backend/` when authoring a migration. Handy while iterating:

```bash
uv run pytest                       # full suite
uv run ruff check . && uv run mypy app
```

### Frontend

```bash
cd frontend
npm install
npm run dev                      # vite dev server, default port 5173
```

`frontend/vite.config.ts` is the truth here: the dev server proxies **`/api` → `http://localhost:8000`** by default, listens on `FRONTEND_PORT` (env, default 5173), and the proxy target is overridable with `VITE_API_BASE_URL`. The frontend only ever calls relative `/api/v1/...` paths (`frontend/src/api/client.ts`), so the proxy is the single switch for where the backend lives.

**Pointing the dev frontend at a containerized backend** — run only `db` + `backend` in compose and vite on the host:

```bash
docker compose up db backend            # backend published on localhost:8000
cd frontend && npm run dev              # default proxy target already matches
# or, against a non-default port/host:
VITE_API_BASE_URL=http://localhost:9000 npm run dev
```

Vite gives you HMR for all frontend code; uvicorn `--reload` restarts the backend on save (note: prompt files are `@cache`d in `backend/app/prompts/__init__.py`, so a prompt edit needs the reload/restart to take effect).

## Keyless mode: the fake provider

With no provider keys at all, the whole stack still runs deterministically:

1. Set `FAKE_LLM_ENABLED=1` in `.env` (or the environment).
2. In Settings, pick `fake:scripted` as the default model (the fake provider registers through the same port as real adapters — `backend/app/llm/fake.py`).
3. Script exact model behavior via the control endpoint, which is mounted 404-invisible unless the flag is set (`backend/app/api/fake_llm.py`):

```bash
curl -X POST localhost:8000/api/v1/_fake/script \
  -H 'content-type: application/json' \
  -d '{"calls": [{"content": "scripted answer"}]}'
curl -X POST localhost:8000/api/v1/_fake/clear
```

Each queued call is consumed FIFO by the next fake-model invocation anywhere in the system; a call may carry `tool_calls`, an `error` (the model call raises), or `delay_s` (slow streaming, useful for exercising Stop/queue UI). This is exactly how the pytest suite scripts LLMs in-process and how the acceptance walk drove deterministic runs.

## Where things live

| Thing | Location / port |
|---|---|
| Backend API | `localhost:8000` (`BACKEND_PORT`), routes under `/api/v1`, plus `/health` and `/metrics` |
| Frontend | `localhost:5173` (`FRONTEND_PORT`) — vite dev server or nginx container |
| Compose Postgres | service `db`, internal `5432`, volume `pgdata` |
| Test Postgres | `localhost:5433` by convention, or `TEST_DATABASE_URL` |
| Optional Redis | compose profile `redis`, `127.0.0.1:6379` |
| Filesystem MCP sandbox | `/workspace` volume (`WORKSPACE_DIR`) |
| SSE stream | `GET /api/v1/chat/stream/{run_id}` (plain HTTP, consumed via `EventSource` in `frontend/src/api/client.ts`) |
