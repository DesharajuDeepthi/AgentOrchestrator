# Acceptance Evidence — Full Regression Recapture (Direct-Invocation Era)

**Date**: 2026-08-09 · **Models**: genuine `anthropic:claude-sonnet-4-6` for every role (default/planner/aggregator/formatter inherit, first-boot resolved) across all stages, **except** stage 20, which keeps its heterogeneous role mix (`claude-opus-5` default + `claude-sonnet-5` planner/formatter) because mixed models are that stage's claim. · **Method**: a file-for-file 1:1 replacement of all 161 frames of the previous campaign, re-run from a **fresh `docker compose up`** (wiped volumes, first-boot seed) on the branch carrying direct sub-agent invocation (spec §7.5) and the seeded native tier — proving no base behavior regressed under the new code. Same prompt discipline and UI-first method as the prior round; the API verified outcomes (run settlement, route rungs) but never produced evidence. New-feature evidence lives in `docs/acceptance-new/` stages 25+.

<details><summary>Previous campaign header (2026-08-07 → 08, sonnet-5 era)</summary>

**Date**: 2026-08-07 → 2026-08-08 · **Models**: genuine `anthropic:claude-sonnet-5` default (effort `medium`, planner effort `high`) for the core stages; `anthropic:claude-opus-5` + `claude-sonnet-5` role mixes for stages 19–20; the M9 **formatter** enabled (`a2ui_first`, inheriting the default model) for every run unless a stage explicitly turns it off. · **Method**: a one-to-one replacement of the previous acceptance evidence **plus** every capability added since (stages 18–24), driven through the real UI in Chromium against the running `docker compose` stack. The API was used only to *verify* what the UI did (run settlement, route rungs, checkpoint counts) — never to produce evidence.

</details>

Prompt discipline: chat prompts avoid capability names except in the stages whose claim *is* explicit invocation (site-analyst gate stages). The orchestrator routes on its own judgment; traces verify what it chose after the fact.

## What makes this campaign different from the last one

1. **The formatter exists now.** Every settled answer in these screenshots is the M9 presentation pipeline at work: `a2ui_first` structured answers with a collapsed `VIEW RAW RESPONSE` toggle and a deterministic **coverage** badge, `raw_first` as the alternate arrangement, and — with the formatter off — raw rendering with **no structured toggle at all** (no fallback, by design). Stage `24-formatter/` proves the whole contract, including per-run frozen presentation in history.
2. **Two adversarial audit workflows drove re-passes.** A parity audit mapped every claim of the previous campaign to the new tree and generated the gap plan; a pixel-level "right moment" QA pass judged every screenshot against its claimed instant (live shots must show live indicators, settled shots must not, traces must have the claimed step in frame). Everything flagged was reshot with event-anchored captures — twice, because a second full QA pass ran over the finished tree.
3. **The campaign found and fixed two real orchestrator bugs.** (a) Thinking models occasionally answer the planner's forced tool call in prose — the planner now runs the validate→repair-once→fail loop on `OutputParserException`. (b) The aggregator could accept an *empty* streamed completion and persist a completed run with an empty answer (caught live in a stage-20 rerun: 4 output tokens) — it now retries once and fails the run honestly if the retry is also empty. Both fixes landed with regression tests before the affected stages were reshot.

## Stage index

| Folder | What it proves |
|---|---|
| `00-fresh-slate/` | Seeds on a fresh DB; chat home cropped to the main pane |
| `01-settings-models/` | Providers panel (anthropic configured), model pickers + params |
| `02-mcp-servers/` | Register form, test-connection, active status, refresh-tools captured in its busy state (last-connected updates only on reconnect) |
| `03-tools/` | Table + badges, search, source filter, schema drawer, expose toggle |
| `04-skills/` | Bad `{tool:}` mention rejected; editor + preview; fresh updated-at after edit (the registry has no version column — spec §4 tracks `updated_at`) |
| `05-sub-agents/` | Static seed card, template picker, invalid DAG rejected inline, live DAG preview |
| `06-trial-graph-thinking-on/` | Trial T1 mid-run + trace. **No plan-card shot: this run engaged the full-catalog fallback, so no plan card ever existed** — documented, not faked |
| `07-trial-graph-thinking-off/` | Trial T2: plan card live (waves + S1), gate armed, a2ui answer, follow-up turn |
| `08-trial-agentic-thinking-on/` | Trial T3: agentic todos card live, mid-run activity, step-timeline trace |
| `09-trial-agentic-thinking-off/` | Trial T4: same assertions, thinking off |
| `10-fallback-uncovered-ask/` | **Live** full-catalog fallback banner mid-run, settled answer, `rung: fallback` trace |
| `11-hitl-deny-and-queue/` | Deny with a typed note, post-deny rail, trace with the hitl step expanded (`status: denied` + note) |
| `12-stop-and-queued-message/` | Stop mid-run → cancelled; queued draft auto-fires |
| `13-failure-retry-cancel/` | Fallbacks disabled via UI → **genuinely failed** run (SHA-512 uncovered ask) → re-enable → Retry re-plans → completed → failed row deleted from drawer |
| `14-runs-and-ops/` | Runs table + search, observability controls, exposure-cap banners, seed reload |
| `15-static-guards/` | Static drawers: definition fields disabled, status toggles live |
| `16-theme-gallery/` | The same settled structured answer in all four palettes; picker restored |
| `17-data-purge/` | Runs table before purge → purge → **post-purge clean run completes** |
| `18-registry-cache-and-retrieval/` | Cache bypass/memory status + runs in both modes ×2 orchestrator modes, generation bump, refresh-all, retrieval-active run (`retrieval_threshold=1, top_k=1`) |
| `19-provider-agnostic/` | Multi-turn conversations (3-turn graph, 2-turn agentic) — see honest notes |
| `20-heterogeneous-models/` | opus-5 default@high + sonnet-5 planner@high + sonnet-5 formatter@medium; role mix proven in the per-step trace |
| `21-m8-features/` | HITL **form gate** (choice + text) filled and submitted; chart inside the structured answer; agentic research within the 20-iteration budget |
| `22-hitl-stale-card-fix/` | Direct-approve leg + queue pending/resolved in a second tab (cross-surface regression) |
| `23-ops-fixes/` | Live log-level switch; per-run delete removes its LangGraph checkpoints (**25 → 0** proven in DB); runs empty post-purge |
| `24-formatter/` | Formatter on (`a2ui_first`) settings; `raw_first` primary + expanded structured; **off → no artifact, no toggle**; history immutable after flipping settings back |

## Honest notes (read before comparing to the previous campaign)

- **Stages 19–21 are Anthropic-only substitutes.** Only `ANTHROPIC_API_KEY` exists in this environment right now, so stage 19 proves multi-turn robustness on one provider instead of provider-swap parity, and stage 20 proves *heterogeneous roles* (different models + efforts per role) within Anthropic. The original cross-provider evidence (OpenAI gpt-5.6-terra, Gemini) remains in git history. Re-proving cross-provider needs those keys back.
- **Trial 06 has no plan-card screenshot** because that run's planner engaged the full-catalog fallback — there was no plan card to shoot. The mid-run and trace evidence is real; we chose documentation over a staged reshoot.
- **The overlap judge is nondeterministic.** In this campaign it failed to flag a deliberately near-duplicate skill once (it flagged it in the previous campaign). Recorded as-is.
- **Stage 12 observation**: a message queued while a run is being cancelled fires after cancellation — the queued turn belongs to the *next* run, which is the designed behavior, but the visual can read as "the cancelled run answered".
- **Retrieval ranking is planner-side by design** — it shapes which catalog cards the planner sees and leaves no artifact in the run trace. The retrieval-active run is corroborated by the backend log: `{"kind": "sub_agents", "total": 2, "shown": 1, "dropped": 1, "event": "retrieval_truncated_catalog"}`.
- **The sandbox initially broke web fetches** (the fetch MCP server's readability helper needs an npm install that offline runtime blocks, and the npm-latest undici needs a newer node). Both were repaired mid-campaign (deps pre-installed, undici pinned to 6.x) and stages 19/21 were reshot with real fetched content and source links; search engines still block automated queries, which the answers state transparently when relevant.
- **The purge numbers are from the DB, not the UI**: checkpoints 1646 → 0 (with `checkpoint_blobs` and `checkpoint_writes` also 0) captured via `psql` alongside the UI screenshots.

## Screenshot-moment QA

Every one of the 162 screenshots was judged against its claimed instant by an adversarial multi-agent QA pass — live shots must show live indicators (Stop button / RUNNING pill / streaming cursor), settled shots must not, traces must have the claimed step in frame, second-tab shots must show the claimed surface. Pass 1 (mid-campaign) flagged 33 of 133; every flag was either reshot event-anchored or the file was dropped with the reason recorded above. Pass 2 (on the finished tree): 162 files judged, 24 flagged. Every flag was resolved: moment misses were reshot event-anchored (themed answers re-taken with the conversation actually open, traces re-taken from completed runs, agentic shots re-taken with the mode pill live), over-claiming filenames were renamed to what the product actually shows, and two files whose claims were duplicates were dropped. The final tree holds 161 screenshots.
