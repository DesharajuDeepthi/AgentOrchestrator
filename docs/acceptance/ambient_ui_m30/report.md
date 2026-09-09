# M30 — Ambient UI completeness (§18.5)

Live stack on `openrouter:qwen/qwen3.8-max`. One Playwright pass, no manual
touch-ups; frames in this directory, in order.

## Routines — typed trigger builder + escape hatch + run history

- `01-trigger-builder-webhook` — the New-routine drawer with the typed
  builder: kind picker set to `webhook`, one §17.3 filter row
  (`repo equals core`).
- `02-json-escape-hatch` — the same trigger handed to the raw-JSON textarea
  by the builder→JSON toggle.
- `03-routine-created` — `m30-webhook-guard` in the table.
- `10-routine-run-history` — the drawer's RUN HISTORY (1 fire): the matching
  webhook fire became a real completed qwen run (2327→449 tokens). The
  stored trigger JSON shows exactly what the builder authored.

## The §18.5 decision-plane fix, live

The routine's fire token was fired twice:

- payload `{"repo": "core", ...}` → **fired: routine trigger matched** → run
- payload `{"repo": "docs", ...}` → **held: webhook trigger filters did not
  match**

`04-ledger-webhook-held` shows both verdicts in the fire/hold audit — a
filter authored in the builder is actually evaluated on every webhook fire
(before M30, webhook fires bypassed stored filters entirely).

## Ledger — correlation chain + precision sparklines

- `05-correlation-chain` — the fired row expanded into the chain view
  (grouped by correlation id, cause → effect, indented by depth), with the
  per-category precision sparklines above (green accept / red dismiss ticks
  over each judged window — demo-noisy's all-red bar sits beside its
  learner tier-3 override).
- `06-precision-sparklines` — the section in full.

## Watches — authoring from the page

- `07-watch-compile-proposal` — "tell me when any run gets stuck waiting
  for my approval" compiled LIVE by qwen through `POST /watches/compile`
  (the same compiler as `ambient.watch`): it chose the M28 native state
  probe `pending_hitl_count` with `>= 1` and echoed a plain-language
  interpretation; confirmed from the proposal card.
- `08-watch-typed-builder` — the typed event-filter path: filter rows +
  optional semantic predicate, `POST /watches`, proposed → confirm.
- `09-watches-list-after` — both new watches active in the list beside the
  M28 http_json feed watch.
