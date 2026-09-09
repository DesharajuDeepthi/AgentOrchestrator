# Acceptance evidence — consolidated

This folder is the **single consolidated acceptance-evidence tree** for the
project. Its primary content is the latest full campaign — every stage of
the acceptance suite re-run end-to-end in the **anthropic theme** on live
`openrouter:qwen/qwen3.8-max`, including the complete A2A wave — plus the
preserved evidence of every earlier campaign and milestone proof, so no
historical verification is lost.

Start with **[report.md](./report.md)** — the latest campaign's full report
(stages, §14d A2A proofs, defects found and fixed, honest notes).

## Layout

| Path | What it is |
|---|---|
| `00-…` – `26-…` | The latest campaign: 27 base stages at exact 1:1 frame parity with the prior ambient campaign — fresh slate → registries → four HITL trials → fallback/deny/stop → ops/themes/purge → cache/providers → M8 features → formatter → memory sweep → ambient sweep |
| `27-a2a/` | The A2A stage (spec §14d steps 33–40): dark gate, card registration, write-only credentials, tools projection, skill + ExComm authoring, organic routing with fenced traces, HITL approve/deny with counterparty-side cancel proofs, Stop propagation, park→leader-tick→Inbox, drawer reply, card drift, auth matrix |
| `28-config-hardening/` | The M40 stage (spec §14e steps 41–44): per-conversation composer pin, Settings-page Ambient/A2A/API-guardrail sections with live nav toggling and inline 422s, tick-bounded poll-interval throttle proof, live overlap threshold, rate-limit 429-boundary transcript — plus the surgical in-place refresh of the settings frames the new sections changed (changelog in `report.md`) |
| `29-ambient-pursuit/` | The M41 stage (spec §14f steps 45–47): presence-conditional external channels — the tri-state × presence matrix driven live against a local SMTP sink and an SMS-gateway-shaped webhook sink, including the quiet-hours subordination leg |
| `30-salience/` | The M42 stage (spec §14g steps 48–51): the truthful in-app delivery record, seen/unread, and the content-salience pass judged live — plus `regression/`, a randomized re-capture of archived frames proving unchanged surfaces |
| `31-salience-decisions/` | The M43 stage (spec §14h steps 52–54): the salience decision surface — a live-judged proposal with Do it / Leave it, the layered "why this?" disclosure, apply → digest-lead, Undo restoring the row exactly, decline leaving it untouched, and the two role-model pickers that had no UI until now |
| `32-durable-forgetting/` | The M44 stage (spec §14i steps 55–57): forget/erase verbs, the Forgotten tab, live re-admission suppression — including the two honest misses that calibrated the hybrid gate (measured paraphrase cosines 0.876/0.847) — and the §17.7 proposal-reject rider |
| `report.md` | The latest campaign's report |
| `a2a-14d/` | The first A2A acceptance campaign (39 frames + transcripts + its report) — the §14d steps proven immediately after M37–M39 landed |
| `report-ambient.md` | The prior ambient campaign's report (its frames are superseded 1:1 by the stage dirs above) |
| `README-original-campaign.md` | The original base campaign's index/report |
| `archive/walkone-retest/` | The walkONE-era full retest campaign (stages incl. direct-invoke, native tier, history summary, agent files, rung-4 gate, five-part prompt suite, early memory sweep) — preserved intact with its own per-stage reports |
| `ceremony_m36/` | M36 full acceptance ceremony (spec §18.10) — 66 frames + transcripts |
| `auth_m34/` | M34 auth + tenancy live proofs |
| `coordination_m35/` | M35 multi-replica leader failover proofs |
| `evals_m32/` | M32 evals feature live proofs |
| `ambient_m23/`, `ambient_m25/`, `ambient_channels_m29/`, `ambient_ui_m30/` | Ambient milestone live proofs (delivery plane, policy learning, channels, UI completeness) |

## Provenance

Historical dirs keep the theme and model of their original capture (mostly
the default theme on the models of their era) — the visual difference from
the anthropic-theme primary campaign is intentional and makes provenance
self-evident. Two frame sets in the primary campaign are carried over
rather than recaptured, both flagged in `report.md`: stage 13's retry-flow
frames (the fresh stack would not produce a natural run failure) and — if
present in default theme — the transient agentic plan-card frames.
