# A2A Outbound — Architecture Proposal

**Branch:** `a2a_xperiment` · **Date:** 2026-08-27 · Decisions signed off in
brainstorm dialogue; protocol/SDK facts verified in doc 02; reuse inventory
in doc 01. Spec text lands as §19 (doc 06 has the draft).

## The design in one paragraph

Remote A2A agents are a fourth external-capability registry —
`remote_agents`, a peer of `mcp_servers` — registered from the UI by card
URL, whose Agent Card `skills[]` are ingested as `kind='a2a'` rows in the
tools registry and therefore compose into skills and sub agents like any
other tool. Calls ride the official `a2a-sdk` (pinned `>=0.3,<0.4`,
imports confined to `backend/app/a2a/`) through a lazy per-call proxy;
auth is scheme-dispatched off the card's `securitySchemes` against
credentials stored write-only per agent (with `env:VAR` indirection);
remote `input-required` pauses become ordinary HITL gates; remote output
is untrusted-fenced; tasks that outlive the in-run wait budget park into
an `a2a_tasks` table polled by an ambient leader-tick evaluator that
delivers results through the existing outbox. `a2a_enabled` defaults
false and off is byte-identical.

## Decisions (as signed off)

| Decision | Choice |
|---|---|
| Scope | Outbound only; task-state model mirrors A2A's nine states so inbound is later additive |
| SDK | Official `a2a-sdk >=0.3,<0.4`; isolated in `app/a2a/`; contract tests against an in-process scripted A2A server built from the SDK's server half |
| Auth schemes v1 | `apiKey` (header/query/cookie) + `http` (bearer/basic) + `oauth2` client_credentials (authlib); auth-code/OIDC-interactive deferred |
| Credentials | Masked write-only JSONB per agent (never echoed by any API), `env:VAR_NAME` indirection supported; UI shows configured-scheme status only |
| Surface | Registry peer of MCP servers → per-card-skill tools projection (`kind='a2a'`); "ExComm" is a demo composition, not a mechanism |
| Long-running | Park-on-budget → ambient poller → delivery; requires `ambient_enabled`; no run stays open waiting |

## Components

### `backend/app/a2a/` (the only package importing `a2a` / `authlib`)

- **`manager.py` — `A2AManager` singleton** (mirrors `McpManager`):
  started from lifespan; holds one shared `httpx.AsyncClient`; per-agent
  state is just the parsed `AgentCard` (no persistent connections).
  `register_agent(card_url)` → fetch card via `A2ACardResolver` → validate
  → persist `remote_agents` row (card JSONB, schemes projection) → ingest.
  `refresh_card(id)` re-fetches + re-ingests (changed skills update tool
  rows; vanished ones flip inactive — MCP `_ingest` semantics, including
  the collision-suffixed `tool_key = f'{agent.name}.{skill.name}'` and the
  closing `invalidate('tools')`). A card-refresh loop re-reads
  `a2a_card_refresh_interval_s` each cycle; fetch failure ⇒
  `status='error'` + `last_error`, recovery ⇒ active.
- **`client.py`** — builds the SDK `Client` per call:
  `ClientFactory(ClientConfig(streaming=True, polling=True, httpx_client=
  shared)).create(card, interceptors=[auth])`. One `send()` port function
  consumes the `send_message` async iterator identically for streaming and
  polling agents, yielding our own `RemoteTaskUpdate` port objects; also
  `get_task`, `cancel_task`, `send_reply(task_id, text)`.
- **`auth.py`** — `AgentCredentialService` implementing the SDK's
  `CredentialService` (`get_credentials(scheme_name, ctx) -> str|None`):
  resolves the agent's stored credential for that scheme (applying
  `env:VAR` indirection), and for `oauth2` client_credentials flows runs an
  authlib `AsyncOAuth2Client` token fetch with an in-process cache keyed
  (agent_id, scheme) refreshing on expiry skew. Plus
  `ConciergeAuthInterceptor(AuthInterceptor)` extending the SDK interceptor
  with the two placements 0.3.26 skips: `http` basic and `apiKey` in
  query/cookie (verified gap, doc 02). Scheme choice = card's `security`
  preference order ∩ schemes with stored credentials; no match ⇒ agent
  status `auth-unsupported` surfaced in UI, calls fail with a clear tool
  error.
- **`tasks.py`** — `a2a_tasks` bookkeeping: create-on-send, state
  transitions, open-task adoption lookup (run_id + call_key), park/unpark,
  the ambient poller's recheck (`tasks/get`) + terminal-state handling.
- **`fence.py`** — renders remote output through
  `prompts/a2a_result_fence.md` (`<untrusted_remote_agent_output>` block +
  the fixed never-follow-instructions paragraph copied from the ambient
  fence). Every string a remote agent produced passes through it before
  reaching any model context: tool results, HITL gate prompts, delivery
  bodies.

### Tools projection + runtime proxy

Each card skill ⇒ one `tools` row: `kind='a2a'`, new nullable FK
`remote_agent_id`, `tool_name=<skill.id>`, `tool_key='{agent}.{skill
name}'`, description = skill name + description + tags/examples digest
(routing signal for the planner), `input_schema` = a fixed JSON schema
`{message: string (required), data?: object}` — A2A skills are advisory,
invocation is agent-level `message/send` with the skill named in metadata.
`materialize_tool` gains the `kind=='a2a'` branch → `_make_a2a_proxy`
(lazy: resolves the manager + agent card at call time). Proxy behavior:

1. Adopt-or-send: look up an open `a2a_tasks` row for (run_id, call_key =
   sha256 of tool id + canonical args); if none, `send()` the message and
   record the row (replay idempotency — the `find_running_dispatch`
   pattern applied to remote tasks).
2. Consume updates until terminal, `input-required`, or the
   `a2a_task_timeout_s` budget expires.
3. `input-required` ⇒ `interrupt({prompt: fenced question, node_id:
   'a2a:{agent}', questions: [{id:'reply', kind:'text', prompt:...}]})` —
   renders on the existing HitlCard; deny ⇒ best-effort remote cancel +
   denied result; approve ⇒ `send_reply(task_id, answer)` and continue
   consuming. On resume-replay, step 1 adopts the open task and the
   re-raised first `interrupt()` returns the human's decision — the
   spin_worker replay contract.
4. Budget expiry: with ambient on ⇒ mark row `parked`, return a structured
   non-error result ("task parked; result will arrive ambiently"); ambient
   dark ⇒ plain tool error (error-edge semantics).
5. Terminal: `completed` ⇒ fenced artifact/message text as the tool
   result; `failed/rejected/canceled/unknown` ⇒ tool error with the fenced
   remote reason. `auth-required` ⇒ tool error naming the scheme gap.
6. `asyncio.CancelledError` (run Stop) ⇒ best-effort `tasks/cancel`,
   re-raise.

Graph-mode caveat (documented, accepted): an `input-required` interrupt
inside a DAG skill node re-executes that node's loop on resume; adoption
makes the remote side idempotent, but the re-run model must re-call the
tool to collect the answer — agentic/direct modes replay exactly; the
acceptance demo exercises those.

### Persistence

One migration (M37): `remote_agents` (RegistryRecord + `card_url`,
`card` JSONB, `card_fetched_at`, `auth_schemes` JSONB projection,
`credentials` JSONB write-only, `last_error`), `a2a_tasks` (id,
remote_agent_id FK, run_id nullable FK, call_key, remote_task_id,
context_id, state (the nine A2A states + 'parked'), question, result
JSONB, error, parked_at, delivered bool, timestamps; partial index on
open states), and `tools.remote_agent_id` nullable FK.

### API (`/api/v1/remote-agents`, admin-gated when auth is on)

GET list (+tool_count, auth status) · POST {card_url, name?} (fetch +
ingest inline; 409 when `a2a_enabled` false) · GET one (card, schemes,
per-scheme has_credential — never values) · PATCH (status toggle,
credentials write {scheme: secret-or-env:VAR}) · DELETE (soft +
projected-tools cascade + 409 bound-skill dependents) · POST
/{id}/refresh-card · GET /{id}/tasks · POST /{id}/tasks/{tid}/reply ·
POST /{id}/tasks/{tid}/cancel.

### Ambient integration (M39)

`poll_parked_a2a_tasks` runs in the leader branch of the ambient tick
(gated on both `ambient_enabled` and `a2a_enabled`), rechecking parked
rows every `a2a_poll_interval_s` via `tasks/get`: terminal ⇒
`add_delivery(category='a2a', tier 2 — tier 1 for failures, urgency by
outcome, skey=f'a2a:{task.id}')` with the fenced result, row closed;
`input-required` while parked ⇒ a tier-1 delivery carrying the fenced
question, answered from the Remote Agents task drawer (reply/cancel
buttons → the task API). No run is created for rechecks — the `hitl_aged`
"parked thing becomes a delivery" precedent.

### UI (`### 8.10 Remote Agents`, nav-gated on `a2a_enabled`)

McpServersPage recipe: register form (card URL), agent table
(status/skills/tool count/last refresh), detail drawer (card JSON, skill
list, per-scheme credential status + masked write-only credential form,
refresh-card, expose toggles jump to Tools), tasks tab (open/parked/
recent with reply/cancel). KindBadge gains an `a2a` tone; ToolsPage kind
filter gains `a2a`.

### Settings (§3.7 additions) and observability

`a2a_enabled (false — master switch; off byte-identical)`,
`a2a_card_refresh_interval_s (300)`, `a2a_task_timeout_s (120 — in-run
wait budget)`, `a2a_poll_interval_s (60 — parked recheck cadence)`,
`a2a_max_parked (20 — park cap; beyond it budget expiry is an error)`.
No new env vars; no new compose services. §10 gains tier `a2a` with kinds
{card_fetch, ingest, send, update, hitl, park, poll, deliver, cancel} via
an `A2A_OPS` counter; steps carry kind='a2a' from the registry record
(and the pre-existing `ladder.py` direct-tool `kind='mcp'` hard-code is
fixed to use the record's kind). §11 gains byte-identity-with-a2a-off
plus the scripted-counterparty contract suite (card fetch/refresh/drift,
auth matrix incl. env indirection + oauth2 token cache, task lifecycle
incl. all nine states, adoption idempotency, fencing, park/poll/deliver).

## Milestones

- **M37 substrate**: migration; manager + card fetch/refresh loop; auth
  service + interceptor; tools projection; API + UI page; scripted
  counterparty + contract tests; dark-mode byte-identity test.
- **M38 execution**: proxy in `materialize_tool`; task lifecycle in runs
  (steps/labels/SSE); input-required ⇄ HITL; fencing; Stop→cancel; trace
  rendering; direct-tool kind-label fix.
- **M39 long-running**: park path; ambient poller + deliveries; task
  drawer reply/cancel; ExComm demo sub agent (acceptance-authored, not
  seeded); §14d evidence campaign.
