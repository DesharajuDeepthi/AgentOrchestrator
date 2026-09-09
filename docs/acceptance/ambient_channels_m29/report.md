# M29 — Delivery channels + toast (§14c-30)

Live stack: `openrouter:qwen/qwen3.8-max` default, ambient on. Local sinks on
the compose bridge: an SMTP sink at `172.18.0.1:8025` (raw asyncio, logs each
DATA payload) and a webhook sink at `172.18.0.1:8026/push`. Backend env:
`SMTP_HOST/PORT/FROM/TO` + `AMBIENT_WEBHOOK_URL` (env-only, per §13).
Routing setting: `ambient_channels = {"digest": ["in_app", "email"],
"interrupt": ["in_app", "webhook"]}`.

## Digest → ONE email

Queued two tier-2 deliveries, set a digest time that had just passed, waited
for the tick. The sink received exactly **one** message:

```
Subject: [concierge] ambient digest: 12 item(s)
From: concierge@local  To: owner@local
```

The batch collapsed 12 pending tier-2 rows (the two new items plus older
pending rows) into one email; both new items appear in the body. Both rows'
inbox records carry the per-channel send ledger:

```
external: {"email": {"ok": true, "error": null, "at": "2026-08-25T22:32:36Z"}}
```

## Tier-0 interrupt → toast (no reload) + webhook

`01-before-toast.png` — Runs page open, ambient stream subscribed, no toast.
A tier-0 delivery was then inserted server-side (no page interaction).
On the next tick the flush delivered it as an interrupt, the SSE stream
broadcast it, and `02-toast-visible.png` shows the toast bottom-right
(“AMBIENT INTERRUPT · OPS — TOAST PROOF 2 …”) — the page was never
reloaded (Playwright held the same document; selector wait, no goto).

The same flush POSTed the envelope to the webhook sink:

```
{"kind": "ambient_delivery", "mode": "interrupt",
 "items": [{"title": "TOAST PROOF 2: …", "tier": 0, ...}]}
```

and the row's ledger reads `{"webhook": {"ok": true, ...}}`.

## Bonus findings exercised live

- The first attempt inserted the tier-0 row while today's interrupt budget
  (3) was already spent — the flush demoted it to digest-lead (tier 2), the
  M23 behavior, proving budgets still gate external channels; the proof rerun
  used a raised budget.
- Quiet hours were moved (22:00–07:00 covers the proof window; quiet hours
  are absolute and would have held everything to the digest).
