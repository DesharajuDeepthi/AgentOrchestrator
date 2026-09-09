# A2A acceptance campaign — §14d steps 33–40 (M37–M39)

Live manual-UI acceptance for the A2A outbound wave (spec §19), run on the
rebuilt `docker compose` stack (backend image carrying `a2a-sdk` 0.3.26 +
`authlib`), real LLM `openrouter:qwen/qwen3.8-max` end to end, against five
scripted A2A counterparties (`tests/a2a_counterparty.py` — the SAME stub the
contract tests use, run as host processes on the docker bridge like the
smtp/webhook sinks; no new compose service). Evidence: [frames/](./frames/)
(39 UI screenshots) + [logs/](./logs/) (campaign transcripts with the
API/DB/remote-state assertions inline).

**Verdict: all eight §14d steps pass.** Two real defects were found by the
campaign and fixed + regression-tested before the final frames (see
[Findings](#findings-fixed-during-the-campaign) — this is what live
acceptance is for).

The stack ran throughout with memory + ambient ON (the volume carries the
previous campaign's data), so every frame shows A2A coexisting with the
full §16/§17 machinery — remembered context in planner prompts, the
ambient Inbox carrying a2a deliveries next to anticipation digests.

## Step-by-step

| § | What was proven | Frames / log lines |
|---|---|---|
| 33 | Boot default `a2a_enabled=false`: Remote Agents nav item absent, `POST /remote-agents` → 409 `a2a is disabled`; §11 byte-identity + dark suites green in the full run (721→723 passed). Enabled via the settings API (the master switch, like ambient) → nav appears. | `33-01`, `33-02`, `a2a-c1.txt` |
| 34 | Registered the open counterparty from the page by card URL; card fetched + rendered, both skills listed, `no auth declared — open`; Tools page shows `stub-open.research` / `stub-open.summarize` as `kind=a2a`, active, agent-prefixed keys. | `34-01…05` |
| 35 | Auth matrix through the UI's write-only credential editor: apiKey-header configured as **`env:STUB_API_KEY`** (env indirection — the secret lives only in the backend env), http bearer, oauth2 client_credentials (id+secret). Per-scheme chips: `ok` ✓ for all three, `unsupported` ✕ for the mutualTLS-only card. The API NEVER serializes credentials (`credentials-key-present=false` on every agent; payloads grepped for the secrets — clean). Live authenticated chat runs (real qwen, rung-1 direct tool): all three schemes completed against the 401-enforcing stub gate with fenced `stub-echo` results; the mutualTLS-only agent's call failed with the counterparty's 401 as a plain tool error, honestly reported. | `35-*`, `a2a-c2.txt`, `a2a-c3.txt` |
| 36 | ExComm composition authored during acceptance (not seeded): skill `excomm-research` wrapping `stub-open.research`, sub agent `excomm-delegate` over it. A routed chat run (`rung: custom_sub_agent`) drove the skill loop through the a2a tool — trace shows `plan → route → skill → tool_call(stub-open_research, kind=a2a) → aggregate`, the remote answer wrapped in `<untrusted_remote_agent_output>` in the step output, and the answer quoting `stub-echo: …` verbatim. | `36-excomm-routed-chat`, `36-excomm-trace` |
| 37 | Remote `input-required` inside the ExComm worker raised the STANDARD HITL card: prompt `Remote agent 'stub-open' asks (untrusted, its own words): …`, typed-reply field. Approve with `target the staging environment` → the resumed tool ADOPTED the open remote task (single a2a_tasks row for the run, DB-verified) and replied into it → `stub-answered: target the staging environment` fenced in the trace. Deny (second run) → local row `canceled` / `denied by human reviewer` AND the counterparty's own `tasks/get` reports `canceled`. | `37-01…05`, `a2a-c4.txt` |
| 38 | Stop pressed mid-`slow:60` remote call → run `cancelled`, local row `canceled`, and the counterparty's `tasks/get` reports `canceled` (remote-state proof, not just local bookkeeping). | `38-01`, `38-02` |
| 39 | `a2a_task_timeout_s=10`, remote `slow:30`: the tool returned the structured parked note ("result will be delivered ambiently — do not wait"), the run COMPLETED, and the REAL ambient leader tick (no manual poller call) delivered the fenced result to the Inbox ~40s later: `category=a2a`, tier 2, `[stub-open] remote task completed`, `<untrusted_remote_agent_output …state="completed">stub-slow-done…`. **Zero recheck runs** — the newest run after delivery is still the park run. | `39-01`, `39-02`, `a2a-c5.txt`, `a2a-c5b.txt` |
| 40 | Card drift: the counterparty added a `translate` skill live (`/_control/add-skill`); **Refresh card** re-projected it — detail drawer shows 3 skills, Tools page gains `stub-open.translate` (active). Parked-task drawer: a parked `slowask` task's remote question was flipped to `input-required` by the poller with a tier-1 `needs your input` delivery; the drawer reply completed the remote task (`stub-answered: use the staging dataset` in the DB row + result delivery); a second parked task cancelled from the drawer — remote `tasks/get` reports `canceled`. | `40-01…06` |

## Findings (fixed during the campaign)

Both were GraphInterrupt-swallowing violations of the house hard rule,
unreachable before a TOOL could raise a gate (only a2a tools do), and both
now carry regression tests in `tests/test_a2a_execution.py`:

1. **Rung-1 direct tool + remote question → raw swallowed interrupt.**
   `run_direct_tool` executes outside any graph (no checkpointer — it
   CANNOT pause), and its blanket `except Exception` turned the proxy's
   `interrupt()` into an unreadable `(Interrupt(value={…})` step error.
   Fix: catch `GraphInterrupt` explicitly and surface a clear error — the
   remote question verbatim plus "reply from the Remote Agents task drawer,
   or route via a skill". The task row stays `input-required`, and the
   drawer reply completes it (proven live: `38-03…05`).
2. **Graph-mode worker skill loop swallowed the pause.** `_make_skill_node`'s
   §3.5 error-edge `except Exception` caught the `GraphInterrupt`
   propagating out of the inner skill loop, so a remote question became a
   failed node instead of a paused run (agentic mode already re-raised it
   correctly, which is why the M38 suites — agentic for HITL — were green).
   Fix: re-raise `GraphInterrupt` before the error-edge catch; the worker
   pauses, and on resume the replayed a2a tool adopts its open remote task
   (§7.1) — the live 37a run confirmed single-send adoption on real qwen.

Observations (recorded, no change made):
- `A2ATaskOut` doesn't serialize the stored `result` payload — the drawer
  shows state/question/error and results arrive via step outputs and
  deliveries, which is what §8.10 asks for. Cosmetic candidate for later.
- `update_task` clears `question` on every state change — coherent with its
  "pending question" mirror semantics (the drawer's amber box disappears
  once answered), noted here because it surprised the campaign scripts.
- The AmbientPage dark-mode banner says "Settings → Ambient" but the master
  switches for the experiment waves (memory aside) are settings-API-only —
  pre-existing wording, untouched by this branch.

## How to reproduce

```sh
# five counterparties on the docker bridge (auth mode per §14d-35)
cd backend
.venv/bin/python -m tests.a2a_counterparty --port 8027 --name stub-open
.venv/bin/python -m tests.a2a_counterparty --port 8028 --name stub-hdr    --auth apikey-header
.venv/bin/python -m tests.a2a_counterparty --port 8029 --name stub-bearer --auth bearer
.venv/bin/python -m tests.a2a_counterparty --port 8030 --name stub-oauth2 --auth oauth2
.venv/bin/python -m tests.a2a_counterparty --port 8031 --name stub-mtls   --auth mtls-only
# then follow §14d steps 33–40; campaign scripts preserved in logs/
```

Final backend suite after the two fixes: **723 passed, 1 skipped** (includes
the 32 A2A contract tests). Frontend: eslint 0
errors, vitest 49 passed, `tsc -b` clean.
