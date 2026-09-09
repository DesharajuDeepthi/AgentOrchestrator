# Operator's Guide to the Admin UI

The Concierge Agent admin is a single web app with seven pages in the left nav: **Chat, MCP Servers, Tools, Skills, Sub Agents, Runs, Settings**. This guide walks each page task by task. Screenshots of every flow live in the acceptance evidence set — links below point at the stage folders under [docs/acceptance/](./acceptance/README.md).

A shared pattern everywhere: registry pages are tables with a search box, source filter (static/dynamic), kind filter, badges, and status pills; clicking a row opens a detail/edit drawer. Records marked `static` shipped with the system — their definitions are read-only, but their status and exposure toggles stay live (see [15-static-guards/](./acceptance/15-static-guards/)).

## Chat

The front door: one conversation window over the whole capability registry. Visuals: [06-trial-graph-thinking-on/](./acceptance/06-trial-graph-thinking-on/) and [08-trial-agentic-thinking-on/](./acceptance/08-trial-agentic-thinking-on/).

- **To start a conversation**: click **+ New conversation** in the sidebar, type into the composer, press Enter (Shift+Enter for a newline). Past conversations are listed in the sidebar with run counts; selecting one loads its full history, and follow-up messages use that history as context.
- **While a run works**, the live view builds up in order:
  - a **plan card** — in graph mode, plan entries grouped into numbered *waves* (entries in one wave run in parallel, marked `∥ parallel`); in agentic mode, a live todo checklist whose items check off as the loop progresses;
  - **route lines** showing which rung each capability resolved to (`direct_tool`, `custom_sub_agent`, …), and an amber **full-catalog fallback** banner when descriptions failed to route the request ([10-fallback-uncovered-ask/](./acceptance/10-fallback-uncovered-ask/));
  - **dispatch rails** — one card per dispatched capability, labeled with tier, kind, and name (e.g. `SUB_AGENT · CUSTOM · site-analyst`), with the sub agent's own skill runs, tool calls, and approval card nested inside it. Ephemeral workers appear under phonetic **callsigns** like `worker-alpha (web-research+file-ops)`;
  - a one-line **activity ticker** at the bottom showing the last few steps currently executing — names only, no payloads (those live in the trace);
  - a collapsible dimmed **model thinking** block when the model streams reasoning.
- **HITL cards**: when a workflow reaches a human gate, an amber card pauses the run. A plain gate offers **Approve** / **Deny** plus an optional note for the worker. A **form gate** renders every question on the same card — choice chips, text fields, yes/no toggles — and enables a single **Submit answers** button only once all are answered ([21-m8-features/](./acceptance/21-m8-features/) shows a two-choice + text gate). Deny with a note is shown in [11-hitl-deny-and-queue/](./acceptance/11-hitl-deny-and-queue/). A gate resolved from anywhere else (the Settings HITL queue, a cancel) collapses the card automatically.
- **Answers**: how an answer renders is frozen at run time by the **formatter** (Settings → Formatter). With the formatter on and presentation **A2UI first** (the default), the structured view — cards, tables, stats, SVG charts — is primary and the raw markdown collapses behind **▸ view raw response**; with **raw first**, the markdown bubble is primary and the structured artifact sits behind **▸ show structured summary**. A quiet **coverage** badge shows how much of the raw answer's numbers/URLs/code the structured view retained (amber when below the Settings threshold). With the formatter **off** (or if its call failed for a run), the raw answer renders directly and **no structured toggle exists** for that run — there is no fallback formatting, and history always re-renders each run exactly as it happened. Charts made by the `render_chart` tool render with the primary answer in every state. Every answer footer links to its **run trace**.
- **To stop a run**: the Send button becomes **■ Stop** while a run is in flight — it cancels the run at the next step boundary ([12-stop-and-queued-message/](./acceptance/12-stop-and-queued-message/)).
- **To queue a message**: type while a run works and press **Queue ⏎** (or Enter). One message waits per conversation, stays editable in the composer (or can be discarded), and auto-sends the moment the current run finishes — in that conversation only.
- **Themes** are picked on the Settings page (Appearance): `default`, `anthropic`, `openai`, `google` — see the same conversation in all four in [16-theme-gallery/](./acceptance/16-theme-gallery/).

## MCP Servers

Plug tool servers in at runtime — no restart. Visuals: [02-mcp-servers/](./acceptance/02-mcp-servers/).

- **To add a server**: **+ Register server**, pick the transport:
  - **stdio** — a command the backend spawns as a subprocess: fill `Command` (e.g. `uvx`), space-separated `Args` (e.g. `mcp-server-fetch`), and optional env key/value pairs (values entered masked).
  - **http** — a remote streamable-HTTP endpoint: fill `URL` and optional headers.
  Give it a clear description — **the planner routes by this prose**. Click **Register & connect**: the backend connects, lists the server's tools, and ingests them into the Tools registry immediately. A failed connection keeps the record with an `error` status and the error text, ready to retry.
- **Health**: the table shows a status pill per server (hover an `error` pill for the last error), tool count, and last-connected time. A background health ping (interval set in Settings → MCP) flips dead servers to `error` ([13-failure-retry-cancel/](./acceptance/13-failure-retry-cancel/) shows the degrade-and-recover cycle).
- **To re-ingest tools**: open the server drawer and click **Refresh tools** (re-runs the tool listing and reconciles — new tools added, removed ones marked inactive). Servers also push `listChanged` notifications, so most changes reconcile on their own. **Reconnect** retries a dead connection; **Deactivate/Activate** toggles the server; **Delete** (dynamic servers only) is blocked with a dependents dialog if its tools are bound into skills.

## Tools

The bottom tier: everything ingested from MCP servers plus code-registered native tools. Tools are never created here — only browsed and tuned. Visuals: [03-tools/](./acceptance/03-tools/).

- **To browse**: search by tool key or description, filter by source (`static`/`dynamic`) or kind (`mcp`/`native`). Each row shows the `{server}.{tool}` key, kind and source badges, its server, and **skill chips** — one per skill that binds this tool (click a chip to jump to that skill); unbound tools show `unassigned`.
- **Static vs dynamic**: `static` tools were seeded at startup (or registered from code, for `native` kind); `dynamic` tools arrived through an MCP server you added. Same registry, same behavior — the badge only tells you where the record came from.
- **To expose a tool to the orchestrator**: open its drawer and flip **Expose to orchestrator**. The tool gains a `direct` badge and the orchestrator may now call it itself (rung 1, no skill persona). If total exposures exceed the cap set in Settings, a context-cost warning banner appears at the top of this page ([14-runs-and-ops/](./acceptance/14-runs-and-ops/)).
- Also in the drawer: the server-owned input schema (read-only JSON), editable description (dynamic tools), and the status toggle — the global kill switch for a tool.
- **Refresh cache**: the header carries a cache status line (`records · generation · loaded ago`) and a **Refresh cache** button when the registry cache is in `memory`/`redis` mode — an operator override; freshness is otherwise automatic ([18-registry-cache-and-retrieval/](./acceptance/18-registry-cache-and-retrieval/)).

## Skills

The middle tier: a skill is a markdown document — persona plus step-by-step instructions plus a strict list of bound tools. Visuals: [04-skills/](./acceptance/04-skills/).

- **To author a skill**: **+ New skill**. The editor is the skill document template split into form fields and a body:
  - **Frontmatter fields**: name, description (again: the planner routes by it — make it precise), minor persona, **Expose to orchestrator** toggle, an optional **model + effort override** (model select, effort none/low/medium/high, temperature, max output tokens — fields enable only for what the chosen model supports), and **Max tool iterations** — the per-skill loop budget; leave empty to inherit the global default (the seeded `web-research` skill ships with 20).
  - **Tool tags**: a searchable checklist over the whole tool registry, system-seeded static tools listed first, kind/source badges on each. Binding is strict availability — the skill's loop will see exactly these tools and nothing else.
  - **Instructions**: a markdown body pre-loaded with the Purpose / Steps / Output format template, with a live preview pane alongside. Reference bound tools inline as `{tool:server.tool_name}` — the preview highlights resolved mentions and flags unbound ones in red; save rejects any mention that doesn't resolve to a tagged tool.
- **On save, the overlap guard runs first**: an LLM judge compares your draft against existing skills *and tools*; at high overlap a dialog shows the match and the judge's reasoning with **Save anyway** or **Cancel** (use the existing one). It is advisory — if the judge is unavailable the save proceeds ([04-skills/](./acceptance/04-skills/) captures a flagged near-duplicate).
- The table cross-references both directions: **tool chips** and **sub agent chips** per skill. **Delete** (custom skills only) is refused with a 409 dependents dialog while any active sub agent's workflow uses the skill.

## Sub Agents

The top tier: a persona plus a hard, machine-executed workflow DAG over skills. Visuals: [05-sub-agents/](./acceptance/05-sub-agents/).

- **To compose a workflow**: **+ New sub agent**, then either start from a **starter template** (Blank · Sequential pipeline · Branch + HITL approve · Parallel fan-out/join — pre-filled skeletons you repoint at your skills) or build directly. The builder is form-based:
  - **Nodes list**: add **skill nodes** (each picks a skill from a dropdown that shows the skill's bound tools, so the whole skills-vs-tools pool is visible while composing) and **HITL nodes** (each carries an approval prompt).
  - **Edges list**: `from → to` selects between `START`, your nodes, and `END`; an optional natural-language **condition** makes a branch (an LLM router picks the edge at runtime); the **on: success/error** select makes an error edge that catches the node's failure and routes it onward.
  - A **live read-only graph preview** (react-flow) renders nodes, edges, condition labels, and error edges as you type. There is no drag-to-connect editing and no raw-JSON pane in the UI — the node and edge lists are the editor. (Form-gate `questions` on HITL nodes are part of the workflow schema and render in Chat when present; the builder exposes the plain prompt field, so questions are added via the API today.)
- **Validation is real compilation**: **Validate (dry-run compile)** runs the worker factory over your DAG and lists errors (missing START path, cycles, unknown skills, duplicate node ids…) inline; save rejects anything that doesn't compile ([05-sub-agents/](./acceptance/05-sub-agents/) shows reject → fix → save).
- **Model override per sub agent**: the editor carries the same model + effort/temperature/max-tokens override block as skills; empty inherits the Settings default. Resolution order at runtime: skill override → sub agent override → settings default.
- **Overlap guard on save**, same dialog as skills (compared against sub agents *and* skills).
- **Native sub agents** (code-defined graphs, like the seeded `research-concierge`'s siblings) render as a read-only definition card — description, covered-skill chips, `native_ref` — with no builder; only status is togglable.
- **Test invoke** on any row jumps to Chat with the composer pre-filled to target that sub agent, bypassing your own routing prose.

## Runs

Every run, fully traced. Visuals: [14-runs-and-ops/](./acceptance/14-runs-and-ops/) and [13-failure-retry-cancel/](./acceptance/13-failure-retry-cancel/).

- **History**: time, message excerpt, status pill, **orchestrator mode badge** (graph/agentic), duration, and `input→output` token totals; searchable by message, status, or mode.
- **To inspect a trace**: click a row. The drawer shows the message, the final answer with its structured summary panel (expanded here — this is the audit surface), the sub agents involved, and the **step timeline**: a nested tree (children indent under their parent — tool calls under skill nodes, native-subgraph steps under their tool call) with per-step icons, node ids, **route rung chips** (fallback highlighted), model references, token counts, durations, and expandable input/output JSON; failures are highlighted with their error text. Below: the raw **plan JSON** and the **config snapshot** frozen at dispatch.
- **Run controls**: **Cancel run** (running runs — cooperative, stops at the next step boundary), **Retry (re-plan)** (failed runs — re-plans from the original message), **Delete** (finished runs). Runs paused at a gate show Approve/Deny right in the drawer.

## Settings

The command center — every runtime control, applied to the next run, no restarts. API keys are deliberately absent (env-only). Visuals: [01-settings-models/](./acceptance/01-settings-models/), [18-registry-cache-and-retrieval/](./acceptance/18-registry-cache-and-retrieval/), [17-data-purge/](./acceptance/17-data-purge/).

- **Models**: three role selects — **Default**, **Planner**, **Aggregator** (planner/aggregator can inherit the default) — each with effort (none/low/medium/high), temperature, and max-output-tokens fields that enable only for what the selected model supports. The read-only **Provider adapters** panel shows each registered provider as configured / no api key, with its model list; only configured providers appear in the selects.
- **Orchestrator**: the **mode toggle** — 🗺 graph (explicit planner) vs 🤖 agentic (todo-driven) — plus switches for **full-catalog fallback**, **dynamic worker fallback**, **declarative answer UI**, and **charts in the answer panel**; and the limits: max parallel dispatch, max plan steps, **max tool iterations** (the global per-skill-loop budget), and the direct-exposure cap warning threshold.
- **Registry cache**: mode buttons `bypass` (direct DB reads — the shipped default) / `memory` (in-process, event-invalidated) / `redis` (needs `REDIS_URL`; pinged at save), a per-registry status readout (records · generation · loaded-at), and **⟳ Refresh all caches**.
- **Retrieval (progressive disclosure)**: the top-K toggle, threshold (registries at or below it always inject in full), top-K count, and an embedding model reference (empty = lexical-only ranking).
- **MCP**: health-check interval plus global **Reconnect all** and **Refresh all tools** buttons.
- **Observability**: log level select, **LangSmith tracing** toggle + endpoint (empty = SaaS; set a URL to self-host) + project name, and the OTLP endpoint field. See [observability.md](./observability.md) for what each switch does.
- **HITL queue**: every paused run across all conversations, with inline **Approve** / **Deny** — the place to resolve a gate when the original chat tab is gone ([11-hitl-deny-and-queue/](./acceptance/11-hitl-deny-and-queue/)).
- **Appearance**: the four-theme picker; stored in this browser only, applied instantly.
- **Data**: **Reload seed** (idempotent — restores/repairs the static records) and **Purge run history** (confirm-guarded, irreversible) ([17-data-purge/](./acceptance/17-data-purge/)).
