# Stage 30 — prompt suite: inducing each routing rung without naming it

Every prompt in this stage describes a **task**, never a capability. The point
was to find out which rungs of the §7.1 ladder real phrasing actually reaches.
Captured through the UI on a fresh `docker compose up`, default model
`anthropic:claude-sonnet-4-6` (effort medium), planner `anthropic:claude-sonnet-5`
(effort high), registry cache bypass, 76 frames.

Registry as tested — skills: only `notes-formatter` exposed; tools: only
`fetch.fetch` exposed; sub agents: research-concierge, workspace-warden,
workspace-reporter exposed, site-analyst ×2 hidden (routable, not invocable).

## P1 — one composite ask, four different rungs

Prompt: a platform-review pack — a written record of the workspace, a sourced
brief on LangGraph checkpointing kept alongside it, a block of raw standup notes
to tidy, and the exact headline of a cited URL.

| mode | run | routes | gates |
|---|---|---|---|
| graph | `af0f9f26` | custom_sub_agent → workspace-reporter · custom_sub_agent → research-concierge · **direct_skill** → notes-formatter · **direct_tool** → fetch.fetch | 2 approved |
| agentic | `56f271e0` | direct_skill → notes-formatter · custom_sub_agent → research-concierge · custom_sub_agent → workspace-reporter | 2 approved |

Both modes, zero failed steps. The mode difference is the interesting part:
graph raised the URL fetch to its own top-level `direct_tool` step, agentic did
the fetch inside research-concierge and never routed a tool of its own.

frames: `p1-multistep-graph/`, `p1-multistep-agentic/`

## P2 — direct invocation, three sub agents

Pinned via the TARGET picker; the prompts never name the agent.

| pin | run | route | note |
|---|---|---|---|
| workspace-warden | `9307a443` | native_sub_agent | 1 failed tool step inside the curate stage |
| research-concierge | `4fc1d865` | custom_sub_agent | plain approve gate |
| workspace-reporter | `9d7f6bd0` | custom_sub_agent | **form** gate (report format + free text) |

All three ran with `orchestrator_mode=direct` — no planner step. The hidden
site-analyst is absent from the picker and its invoke endpoint returns **403**.

frames: `p2-direct-invocation/`

## P3 — full-catalog fallback

All three asks engaged the fallback, banner visible live, two `fallback` route
steps each (the engagement plus the skill it ran inline):

- SHA-256 of a literal string, derived not recalled — `no_confident_match: true`
- total byte count of the site folder + percentage — empty plan
- "are these two scripts doing the same job?" — empty plan

frames: `p3-fallback/`

## P4 — parallel ephemeral workers: what actually induces rung 4

This is where indirect phrasing hit a wall. Four attempts produced **no**
workers:

| attempt | setup | outcome |
|---|---|---|
| `8b1d7d30` | 2 skills exposed, "two lists, same style, independent" | planner **decomposed** into 4 × direct_skill |
| `14fadb7c` | + web-research exposed, three lists | fallback |
| `728ad904` | covering sub agents inactive, interleaving language | empty plan → fallback |
| `3e589474` | same, second attempt | empty plan → fallback |

What worked (`e09060e5`): the same interleaved two-job prompt, covering sub
agents inactive, **and `max_plan_steps` capped at 2**. With only two plan entries
available, a per-skill pipeline no longer fits and composition is the only
shape left:

```
WAVE 1   s1 · SPIN_WORKER   s2 · SPIN_WORKER      (PARALLEL)
route dynamic_worker → worker-alpha (workspace-auditor+workspace-curator)
route dynamic_worker → worker-bravo (summarize-site)
```

Both entries `depends_on: []`, both workers live at once, zero failed steps.

**Conclusion for rung 4:** the planner prefers decomposition. Reaching rung 4
without naming the mechanism needs the escape hatches closed — no sub agent
covering the skills, and a step budget too small to split the job.

frames: `p4-parallel-workers/` (00–10 the failed shapes, 11–17 the stood-down
attempt, 18–23 the successful two-worker run and the restore)

## P5 — multi-turn sequences

| seq | result |
|---|---|
| **A** history-only follow-up | ✅ turn 1 `custom_sub_agent`, turn 2 **zero route steps** — answered from history |
| **B** escalation | ⚠️ behaviour escalated (turn 1 audit-only, turn 2 also moved files via workspace-curator) but both turns went through the fallback, not a sub agent dispatch |
| **C** deny | ✅ via the pinned surface (`3d8cae0b`): `{"status":"denied","note":"not yet — I will circulate it manually"}`, run completed cleanly. Auto-routed attempts (`25d986cb`, `de8ac2ff`) fell to the fallback, so no gate existed to deny |
| **D** stop + queue | ✅ `de29642c` cancelled by Stop; the queued message auto-sent as `dfb2759f`, which completed |
| **E** fallback does not persist | ✅ turn 3's fallback is planner-driven, not leakage: its plan is `{entries: [], direct_answer: null}` and the route step is the `full-catalog fallback` **engagement**, which only exists when that run's own planner declines |

frames: `p5-multiturn/`

## Findings worth acting on

1. **An MCP tool call has no timeout.** During the first P1 agentic run the
   `fetch` stdio server died mid-call (`mcp_ping_failed` seven seconds later).
   The two in-flight `fetch_fetch` steps never returned and never failed, so the
   run sat in `running` for 25 minutes until cancelled by hand.
   `backend/app/mcp/manager.py` bounds *connect* (`CONNECT_TIMEOUT_S`) and
   *ping* (`PING_TIMEOUT_S`) but not invocation, and the health monitor that
   marks the server `error` does not cancel in-flight calls. A dead server
   should fail the step, not hang the run.
2. **Sub agent status cannot be changed from the UI.** Spec §4 allows toggling
   `status` on static records and the API accepts it, but the Sub Agents page
   renders status as a read-only pill — only `direct_exposure` has a control.
   Standing an agent down (the documented lever for forcing composition) needs a
   PATCH by hand.
3. **The fallback banner renders once per fallback route step.** A run with one
   engagement plus three skill-level `fallback` routes shows three banners
   (`p4-parallel-workers/13-live-parallel-workers-mid.png`). One per engagement
   would read better.
4. **Indirect asks reach the fallback far more often than named ones.** Across
   this stage, short task-shaped prompts frequently produced an empty plan with
   `no_confident_match: false` — neither the "trivial" branch nor the "no match"
   branch — which the graph correctly treats as fallback. Worth a planner-prompt
   look if sub-agent routing is meant to be the common path.

## Registry state after the campaign

Restored exactly as found: only `notes-formatter` exposed, all sub agents
`active`, `max_plan_steps` back to 6, orchestrator mode `graph`.
