# Security Posture (POC)

This document describes the security posture of the Concierge Agent proof of concept honestly: what is protected, what is deliberately not, and what that means for how you may run it. The spec is explicit ([spec.md §1](../spec.md)): authentication/authorization, multi-tenancy, production hardening, rate limiting, and secrets management beyond environment variables are **non-goals**.

## Explicit non-goals

- **No authentication or authorization.** Every API endpoint under `/api/v1` and every admin page is open to anyone who can reach the frontend or backend port. There are no users, sessions, tokens, or roles anywhere in the codebase.
- **No multi-tenancy.** One database, one registry set, one shared conversation history. Every operator sees and controls everything.
- **Trusted-operator assumption.** The admin UI is a command center for a trusted operator on a trusted network. Anyone with UI access can register MCP servers — including **stdio servers that spawn an arbitrary `command args` subprocess inside the backend container** (`backend/app/mcp/manager.py`, spec §5). UI access is therefore equivalent to code execution in the backend container. This is by design for a POC and is the single most important fact on this page.

**Deployment implication: never expose this stack to the public internet as-is.** Run it on localhost or a private, access-controlled network segment. The backend also ships with permissive CORS (`allow_origins=["*"]` in `backend/app/main.py`), and `/metrics` is unauthenticated — both fine for a lab, unacceptable for anything public.

## Secrets

**Provider API keys are env-only — never in the database, never in the UI, never logged.** Enforcement points:

- `backend/app/config.py` — the only place keys are read (`ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `OPENAI_API_KEY`, `LANGSMITH_API_KEY`, `REDIS_URL`), via pydantic-settings from the environment. Its module docstring states the rule.
- `backend/app/settings_store.py` — the `app_settings` key-value store has no key-shaped setting; the `DEFAULTS` dict deliberately omits anything secret. The LangSmith *enable/endpoint/project* live here; the *key* does not.
- `frontend/src/pages/SettingsPage.tsx` — the page subtitle says it outright: "API keys stay env-only, never here." The Providers panel shows only `configured` / `no api key` status, never a value.
- `backend/app/obs.py` — the per-run LangSmith tracer reads `LANGSMITH_API_KEY` from config, never from settings.
- `REDIS_URL` (which may embed credentials) follows the same rule: env-only, and selecting the `redis` cache mode merely pings it (`settings_store._ping_redis`).

**What IS stored in the database** (Postgres is the single stateful service):

- The three registries (tools, skills, sub agents) plus `mcp_servers` records. Note: **stdio env vars and HTTP headers for MCP servers are stored in the DB** (`env`/`headers` jsonb columns, spec §3.1) — these may contain credentials *for those servers*. The UI renders them masked with reveal-on-click (`MaskedValue` in `frontend/src/pages/McpServersPage.tsx`), but they are plaintext at rest. Do not point the POC at MCP servers whose credentials you cannot afford to have in an unencrypted dev database.
- Conversations, runs, and run steps — including **full step inputs and outputs** (tool arguments and results, model outputs). Anything a tool reads ends up in the trace store.
- LangGraph checkpoints (HITL pause/resume state), app settings, and retrieval embeddings.

## Trust boundaries

```
operator ──► admin UI ──► backend ──► MCP servers (subprocess / remote HTTP)
                              │             │
                              ▼             ▼
                          Postgres    tool results ──► model context ──► answers/actions
```

- **MCP servers are the largest boundary.** A stdio server is a subprocess in the backend container; an HTTP server is a remote endpoint you chose to trust. A malicious or compromised MCP server can: return poisoned tool results (which **feed directly into model context** and can steer the run — classic indirect prompt injection), lie about its tool list (`tools/list` output becomes registry records, and its descriptions are read by the planner), exfiltrate whatever arguments the model passes it, and — for stdio — do anything the backend container can do.
- **Prompt-injection exposure is real and unmitigated by design.** The seeded `fetch` MCP server pulls arbitrary live web content; the `web-research` skill feeds it into the model. Fetched pages and MCP tool outputs are untrusted input to the LLM. There is no content sanitization layer; the mitigations that exist are structural: skills see only their bound tools (strict isolation, spec §3.3/§7.0), the filesystem MCP server is sandboxed to the `/workspace` volume, and consequential actions can be gated.
- **HITL gates are the human control point.** A `hitl` node in a sub agent workflow pauses the run at a checkpoint until a human approves, denies, or answers a form gate (`POST /runs/{id}/hitl`). If you want a human between "model decided" and "tool acted" (e.g. before writing files), put a gate in the workflow — nothing else stands there.
- **The overlap judge is advisory only** (spec §4). It is an LLM-as-judge duplicate check on registry saves, it fails open on any error, and the save endpoints are unguarded. It is a hygiene feature, not a security control — do not mistake it for one.

## Model output handling in the UI

Model output is treated as data, never as markup:

- **Markdown answers** render through `react-markdown` in `frontend/src/components/Markdown.tsx` — React elements only, no raw-HTML pass-through (no `rehype-raw`, no `dangerouslySetInnerHTML` anywhere in `frontend/src`). A model that emits `<script>` gets literal text.
- **Structured summaries (A2UI)** are protocol messages rendered by the official `@a2ui/react` renderer (`frontend/src/components/AnswerUiView.tsx`) — "payloads are data, never markup." The component tree itself is generated server-side against a whitelisted schema (`backend/app/orchestrator/answer_ui.py`) and translated deterministically into A2UI v0.9; invalid payloads render nothing.
- **Charts** are pure themed SVG built by `frontend/src/components/ChartSvg.tsx` from specs validated by the `render_chart` native tool's pydantic schema (`_ChartSpec` in `backend/app/native/tools.py`: kind ∈ {bar, line, pie}, numeric series length-checked against labels). No markup surface.

## Network and dependency notes

- Three compose services (`docker-compose.yml`): `db`, `backend`, `frontend`. Published ports: frontend `${FRONTEND_PORT:-5173}→80`, backend `${BACKEND_PORT:-8000}→8000`, and — only under the optional `redis` profile — Redis bound to `127.0.0.1:6379`. Postgres publishes no host port.
- The frontend nginx (`frontend/nginx.conf`) proxies `/api/` and `/metrics` to the backend; everything else serves the SPA. The backend port is also published directly, so the API is reachable on two ports.
- No message broker, no task queue, no Celery — smaller attack/ops surface, one process to reason about.

## Hardening checklist before any real deployment

1. Put an authenticating reverse proxy (or real authn/authz) in front of both the UI and the API; remove the direct backend port publish.
2. Lock down CORS (`allow_origins`) to the actual frontend origin.
3. Disable or strictly allowlist stdio MCP registration — UI-supplied `command`/`args` is remote code execution. At minimum run the backend container non-root, read-only FS, with seccomp/AppArmor.
4. Encrypt or externalize MCP server `env`/`headers` values (secrets manager), and encrypt the database at rest; run traces contain tool inputs/outputs.
5. Add egress controls: the `fetch` server and HTTP MCP clients can reach anywhere the container can.
6. Protect `/metrics` and `/health`, and rate-limit `/chat` (each message spends provider tokens).
7. Review every HITL-less workflow that binds write-capable tools; add gates before irreversible actions.
8. Add TLS everywhere (compose ships plain HTTP), and pin/scan images and dependencies.
