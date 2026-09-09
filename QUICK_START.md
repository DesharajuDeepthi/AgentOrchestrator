# Quick Start

Everything below is driven by five idempotent scripts at the repo root — you should rarely need raw `docker compose` commands. Full operational detail lives in [docs/operations/runbook.md](./docs/operations/runbook.md).

## Prerequisites

- **Docker + Docker Compose** (the whole stack runs in containers)
- An API key for **at least one** provider — Anthropic, Google, or OpenAI, any combination works (or none: see keyless mode below)
- For local development only (not needed to just run the app): Python 3.12 + [uv](https://docs.astral.sh/uv/), Node 20+

## 1. Set up

```bash
git clone <repo-url> && cd concierge-agent
./quick-setup.sh
```

What it does (safe to re-run any time — **re-running is how you update**: every prompt defaults to "keep what I have", so you can Enter through everything and change only what you answer differently, e.g. rotate one API key a month later by answering `y` at that key's "Replace it?" prompt and Enter everywhere else):

- creates `.env` from `.env.example` if missing
- asks **which model provider(s) you want**: Anthropic, Google, or OpenAI alone; any pair; all three; or none (keyless demo mode)
- prompts for each selected provider's API key (hidden input; offers to overwrite an existing value), then **verifies the key with a free list-models API call** before saving — a rejected or unreachable key gets a clear warning and a "save it anyway?" choice
- asks whether to provision the **optional Redis cache backend** upfront (whether the app *uses* it stays a runtime Settings decision — the default cache mode never touches Redis)
- installs local dev dependencies (backend `uv sync`, frontend `npm install`)

A first run looks like this — provider menu, hidden key input, live verification, and the closing configuration summary (key tail redacted here; the deps warnings appear only on machines without local `uv`/`npm`, which Docker-only users can ignore):

![First run: provider menu, key verified, configuration summary](./docs/assets/quick-start/qs-02-fresh-run.png)

If a key is wrong or expired, verification catches it before it's saved — you decide what happens:

![A bad key is rejected by live verification with a save-anyway escape](./docs/assets/quick-start/qs-03-key-rejected.png)

No keys at all? Option 8 provisions the keyless demo mode:

![Keyless demo mode via option 8](./docs/assets/quick-start/qs-05-keyless.png)

Only providers with a key appear in the UI's model selects (with their effort options), and **first boot picks the default model from whatever you configured** — Anthropic's Sonnet if its key exists, else Gemini Flash, else GPT-5.6 Luna, else the fake provider. You can re-mix models per role (orchestrator / planner / aggregator / sub agents) in Settings at any time.

Re-running later to update is the same command — every prompt defaults to "keep what I have" (note the menu pre-selecting the current setup and Enter keeping the existing key):

![Update re-run: Enter keeps the current setup end to end](./docs/assets/quick-start/qs-04-update-rerun.png)

`./quick-setup.sh --help` prints the full reference, including what each interactive step does:

![quick-setup.sh --help](./docs/assets/quick-start/qs-01-help.png)

Non-interactive flags:

```bash
./quick-setup.sh --providers anthropic,google       # or: openai / all / none
./quick-setup.sh --anthropic-key sk-ant-...         # implies its provider
./quick-setup.sh --google-key AIza... --openai-key sk-...
./quick-setup.sh --key sk-ant-...                   # legacy alias for --anthropic-key
./quick-setup.sh --redis | --no-redis               # Redis without asking
```

**No API key at all?** Choose option 8 (or `--providers none`): it sets `FAKE_LLM_ENABLED=1`, and first boot selects the scriptable `fake:scripted` model automatically — runs, SSE streaming, HITL, and both orchestrator modes all work without any provider key.

## 2. Build

```bash
./build.sh
```

Builds the backend and frontend Docker images. Re-run after pulling code changes.

## 3. Start

```bash
./start.sh
```

- errors out early if Docker isn't running
- creates or resumes the stack (`db`, `backend`, `frontend`); missing images are pulled/built
- **first run**: creates the database schema and loads seed data automatically (two MCP servers, native skills and tools, the `research-concierge` sub agent)
- **later runs**: resumes with all your data intact (named volumes)
- waits for backend health, then prints the URLs

![start.sh: containers healthy, URLs printed](./docs/assets/quick-start/qs-06-start.png)

Then open:

| What | Where |
|---|---|
| Admin UI | `http://localhost:${FRONTEND_PORT}` (default **5173**) |
| API | `http://localhost:${BACKEND_PORT}` (default **8000**) |
| Interactive API docs | `http://localhost:8000/docs` |

First things to try: send a prompt in **Chat**, watch the live run trace, then walk the ten-step acceptance script in [spec.md §14](./spec.md). A task-oriented tour of every page is in [docs/user-guide.md](./docs/user-guide.md).

## 4. Stop

```bash
./stop.sh
```

Stops the containers. **All data is preserved** — registries, runs, settings, checkpoints. `./start.sh` picks up exactly where you left off.

## 5. Decommission (destructive)

```bash
./decom.sh        # asks for confirmation
./decom.sh -y     # skips the confirmation
```

Dismantles everything: containers, network, **and the data volumes** — registries, run history, checkpoints, workspace files. The next `./start.sh` is a clean slate (fresh schema, seeds reloaded). Images are kept — rebuild with `./build.sh` only after code changes.

## Optional: Redis cache backend

The registry cache defaults to `bypass` (direct DB reads) and can be flipped live in Settings between `bypass`, `memory`, and `redis` — no restart. To make `redis` selectable:

1. provision it (`./quick-setup.sh --redis`, or set `REDIS_URL` in `.env` yourself)
2. start the profile: `docker compose --profile redis up -d`
3. flip **Settings → Registry cache → redis** (the save pings Redis and rejects if unreachable)

## Common issues

| Symptom | Fix |
|---|---|
| `./start.sh` says Docker isn't running | Start Docker Desktop / the docker daemon, re-run |
| Port already in use | Change `BACKEND_PORT` / `FRONTEND_PORT` in `.env`, re-run `./start.sh` |
| Runs fail with provider/credit errors | Check the key in `.env`; error text is shown verbatim on the run in the Runs page |
| Selecting redis cache mode is rejected | `REDIS_URL` unset or Redis not running — see the Redis section above |
| Something else | [docs/operations/troubleshooting.md](./docs/operations/troubleshooting.md) |

## Where to go next

- **Use the app**: [docs/user-guide.md](./docs/user-guide.md)
- **Operate it**: [docs/operations/runbook.md](./docs/operations/runbook.md) · [configuration reference](./docs/operations/configuration.md)
- **Understand it**: [docs/architecture/overview.md](./docs/architecture/overview.md)
- **Develop on it**: [docs/development/local-development.md](./docs/development/local-development.md) · [contributing](./docs/development/contributing.md)
- **Everything**: [docs/README.md](./docs/README.md)
