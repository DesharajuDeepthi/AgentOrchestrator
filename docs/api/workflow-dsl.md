# Sub-Agent Workflow JSON Reference

A custom sub-agent's behavior is a declarative workflow DAG stored in its `workflow` field ([`POST /api/v1/sub-agents`](rest-api.md)). This document describes the exact schema validated by `backend/app/factory/dag.py` (`validate_workflow`) and compiled by `backend/app/factory/worker.py` (`build_worker`). Validation happens at **save time** — structural checks first, then a full factory compile (`compile_workflow_check`); any error is a 422 with all messages joined by `"; "`. `POST /api/v1/sub-agents/{id}/validate` runs the same checks as a dry run.

## Top-level shape

```json
{
  "nodes": [ ... ],
  "edges": [ ... ]
}
```

Both lists are required and must be non-empty. `START` and `END` are **reserved pseudo-node ids** used only in edges — a real node may not use either as its `id`.

## Node types

Exactly two node types exist (`NODE_TYPES = {"skill", "hitl"}` in `backend/app/factory/dag.py`). There is no dedicated branch or parallel node — branching and fan-out are expressed entirely through **edges** (below).

### `skill` node

Runs one registered skill as a bounded tool loop (see [skill-format.md](skill-format.md)).

| Field | Required | Type | Meaning |
|---|---|---|---|
| `id` | yes | string | Unique within the workflow; not `START`/`END`. |
| `type` | yes | `"skill"` | — |
| `skill_id` | yes | string (UUID) | Must resolve to an **active, non-deleted** skill at save time. |
| `instructions` | no | string | Node-level extra instructions, appended to the assembled prompt as `"Additional instructions for this step:"` (`assemble_skill_prompt`, `backend/app/factory/worker.py`). |

At execution the node sees the sub-agent's `task` plus a formatted digest of all prior node outputs (including `FAILED` / `denied by human reviewer` lines), and records `{status, output, skill_id, skill_name, model, effort, usage}` — or `{status: "error", error}` on failure — into the shared `node_outputs` state.

### `hitl` node

Pauses the run (`run_status: paused_hitl`) and emits an `hitl_request` SSE event ([sse-events.md](sse-events.md)). Resolved via `POST /api/v1/runs/{run_id}/hitl`.

| Field | Required | Type | Meaning |
|---|---|---|---|
| `id` | yes | string | Unique; not `START`/`END`. |
| `type` | yes | `"hitl"` | — |
| `prompt` | yes | string, non-empty | The question shown on the approval card. |
| `questions` | no | array, non-empty if present | Turns the gate into a **form gate** (spec §3.5). |

**Form-gate `questions` schema** (validated in `validate_workflow`):

| Field | Required | Type | Rules |
|---|---|---|---|
| `id` | yes | string, non-empty | Unique across the node's questions. Keys the `answers` map on resolve. |
| `prompt` | no | string | Question label (UI falls back to `id`). |
| `kind` | yes | `"approve"` \| `"choice"` \| `"text"` | `approve` renders yes/no buttons; `choice` renders `options` buttons; `text` renders a free-text input. |
| `options` | for `choice` | array of strings | **At least 2 options** required when `kind` is `"choice"`. |

On approve, the decision plus `answers` (`{question_id: value}`) ride back into worker state (`_make_hitl_node`): the node output becomes `{node_type: "hitl", status: "ok", output: "approved — answers: k=v; ...", answers}`. On **deny**, the output is `{node_type: "hitl", status: "denied", note}` and the router sends the flow **directly to END** ("human denied — routed to END").

## Edge rules

```json
{"from": "<node id | START>", "to": "<node id | END>", "on": "success", "condition": "if ..."}
```

| Field | Required | Type | Meaning |
|---|---|---|---|
| `from` | yes | node id or `START` | Edges may never leave `END`. |
| `to` | yes | node id or `END` | Edges may never enter `START`. |
| `on` | no | `"success"` (default) \| `"error"` | `error` edges fire only when the source node failed. **At most one error edge per node.** |
| `condition` | no | string | Natural-language condition on a success edge — makes it a **branch** candidate (see below). |

Structural rules enforced by `validate_workflow`:

- **Exactly one `START` edge** (one entry point).
- **At least one path from `START` to `END`.**
- **No cycles** — the graph must be a DAG.
- Unique node ids; every edge endpoint must reference an existing node (or `START`/`END`).
- At most one `on: "error"` edge per node.

### Branching, fan-out, and joins (runtime semantics, `backend/app/factory/worker.py`)

Every node is followed by a synthetic router step (`__route__<node id>`) that inspects the node's recorded output and picks targets:

- **Error routing**: if the node output is `status: "error"`, the single error edge is taken. **With no error edge, the whole run fails** (`NodeExecutionError`: `node '<id>' failed: <error>`).
- **Deny routing**: `status: "denied"` (HITL) always routes to `END`.
- **Parallel fan-out**: *all* unconditional success edges fire simultaneously.
- **Branch**: among success edges carrying a `condition`, exactly one is chosen. A single conditional edge with no unconditional siblings is taken directly; otherwise a router LLM call (prompt `backend/app/prompts/router.md`, structured output `ConditionChoice.index`, clamped to range) picks the matching condition based on the source node's output. The chosen conditional target is added to any unconditional fan-out targets.
- **Joins**: a node with more than one incoming edge is compiled as a LangGraph **deferred node** (`defer=True`) — it runs once, after every *reachable* upstream branch completes, so branches not taken never deadlock a join.
- A node whose targets resolve to nothing routes to `END`.

## Validation error catalog

All messages `validate_workflow` can return (a save may return several, joined with `"; "` in the 422 `detail`):

| Message | Cause |
|---|---|
| `workflow must be an object with 'nodes' and 'edges'` | `workflow` is not a JSON object. |
| `workflow needs a non-empty 'nodes' list` / `workflow needs a non-empty 'edges' list` | Missing/empty lists. |
| `every node needs a string 'id'` | Node without a usable `id`. |
| `node '<id>': type must be one of ['hitl', 'skill']` | Unknown node type. |
| `node '<id>': skill nodes require an explicit 'skill_id'` | Missing `skill_id`. |
| `node '<id>': skill_id '<uuid>' does not resolve to an active skill` | Unknown, inactive, or deleted skill. |
| `node '<id>': hitl nodes require a non-empty 'prompt'` | Missing/blank prompt. |
| `node '<id>': 'questions' must be a non-empty list` | Empty/invalid form gate. |
| `node '<id>': every question needs a non-empty 'id'` | Question without `id`. |
| `node '<id>' question '<qid>': kind must be approve\|choice\|text` | Bad question kind. |
| `node '<id>' question '<qid>': choice needs >=2 options` | Choice with < 2 options. |
| `node '<id>': question ids must be unique` | Duplicate question ids. |
| `node id 'START' is reserved` (or `'END'`) | Reserved id used for a node. |
| `node ids must be unique: duplicate '<id>'` | Duplicate node id. |
| `every edge needs 'from' and 'to'` | Malformed edge. |
| `edge <src>-><dst>: 'on' must be 'success' or 'error'` | Bad `on` value. |
| `edge references unknown node '<id>'` | Edge endpoint not in `nodes`. |
| `edges cannot leave END` / `edges cannot enter START` | Reversed pseudo-node use. |
| `workflow must have exactly one START edge (found <n>)` | Zero or multiple entry points. |
| `node '<id>' has <n> error edges; at most one is allowed` | Multiple error edges. |
| `workflow contains a cycle; the DAG must be acyclic` | Cycle detected. |
| `workflow has no path from START to END` | Unreachable `END`. |

When structural checks pass, the save additionally runs a full compile; any exception surfaces as `workflow does not compile: <exception>` (`compile_workflow_check`).

## Worked example: branch + parallel + HITL

A research pipeline: fetch → branch on whether anything was found → form-gated approval → parallel summarize + archive → joined final report. (The seeded `research-concierge` agent in `backend/app/seed/loader.py` follows the same pattern in miniature.)

```jsonc
{
  "nodes": [
    // 1. Entry skill. Extra per-node instructions ride into the prompt.
    { "id": "fetch", "type": "skill",
      "skill_id": "9d2f6f0a-4c1e-4b7a-9a41-1f2f3a4b5c6d",           // web-research (active skill UUID)
      "instructions": "Limit yourself to primary sources." },

    // 2. Form gate: pauses the run, emits hitl_request with `questions`.
    { "id": "approve", "type": "hitl",
      "prompt": "Findings are ready. Publish them?",
      "questions": [
        { "id": "confirm",  "prompt": "Proceed?",            "kind": "approve" },
        { "id": "format",   "prompt": "Output format",       "kind": "choice",
          "options": ["markdown", "plain text"] },              // choice needs >= 2 options
        { "id": "audience", "prompt": "Intended audience",   "kind": "text" }
      ] },

    // 3-4. Parallel pair, fanned out from the gate.
    { "id": "summarize", "type": "skill",
      "skill_id": "9d2f6f0a-4c1e-4b7a-9a41-1f2f3a4b5c6d" },
    { "id": "archive", "type": "skill",
      "skill_id": "2b8c1d3e-7f60-4a2b-8c9d-0e1f2a3b4c5e" },        // file-ops

    // 5. Join: two incoming edges -> compiled as a deferred node,
    //    runs once after every reachable upstream branch finishes.
    { "id": "report", "type": "skill",
      "skill_id": "9d2f6f0a-4c1e-4b7a-9a41-1f2f3a4b5c6d",
      "instructions": "Merge the summary and the archive confirmation into one report." }
  ],
  "edges": [
    { "from": "START", "to": "fetch" },                            // exactly one START edge

    // BRANCH: two conditional success edges from `fetch`; the router
    // model picks exactly one based on fetch's output.
    { "from": "fetch", "to": "approve", "condition": "if relevant results were found" },
    { "from": "fetch", "to": "END",     "condition": "if nothing useful was found" },

    // ERROR edge (max one per node): a fetch failure skips straight to END
    // instead of failing the whole run.
    { "from": "fetch", "to": "END", "on": "error" },

    // PARALLEL fan-out: both unconditional success edges fire together
    // after approval. (A deny on `approve` routes to END automatically.)
    { "from": "approve", "to": "summarize" },
    { "from": "approve", "to": "archive" },

    // JOIN: `report` has 2 incoming edges -> deferred join.
    { "from": "summarize", "to": "report" },
    { "from": "archive",   "to": "report" },

    { "from": "report", "to": "END" }
  ]
}
```

Resolving the gate:

```
POST /api/v1/runs/{run_id}/hitl
{"decision": "approve", "note": "looks good",
 "answers": {"confirm": "yes", "format": "markdown", "audience": "engineering team"}}
```

## How the factory compiles this to LangGraph

`build_worker(snapshot, checkpointer)` (`backend/app/factory/worker.py`) turns a frozen sub-agent snapshot — the workflow plus per-skill snapshots captured at dispatch — into a compiled LangGraph `StateGraph` over the standard worker state `{messages, task, node_outputs}`. Each `skill` node becomes an async function that assembles the §6 prompt order (sub-agent persona → skill persona → skill instructions → node instructions → tool guidance), resolves its model down the override chain (skill → sub-agent → settings default), builds a `create_agent` tool loop through `build_middleware_stack` with the scoped tools-registry middleware (only the skill's bound tools materialize), and merges its result into `node_outputs`. Each `hitl` node calls LangGraph's `interrupt()` with the prompt/questions payload; pause and resume ride the Postgres checkpointer. Every node is followed by a synthetic `__route__<id>` node that enforces the error/deny/branch/fan-out semantics above via `add_conditional_edges`; joins compile with `defer=True`. Compiled workers are cached per `(sub_agent_id, updated_at)` — ephemeral rung-4 workers are never cached.

The same compile runs at save time with no checkpointer (`compile_workflow_check`), which is why "compile-time = save-time": a workflow that saves is a workflow that builds. For where the worker factory sits relative to the orchestrator, ladder, and middleware stacks, see [../architecture/components.md](../architecture/components.md).
