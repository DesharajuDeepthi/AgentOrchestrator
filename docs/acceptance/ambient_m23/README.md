# M23 acceptance evidence — delivery plane + §8.9 Ambient UI

Captured live on `docker compose up` with `default_model=openrouter:qwen/qwen3.8-max`
(all roles) and `ambient_enabled=true`.

| frame | shows |
|---|---|
| 30-ambient-inbox | Inbox tab: digest preview + delivered items with tier badges (interrupt/notify/digest/silent), urgency, channel, and per-item feedback controls |
| 31-ambient-inbox-feedback | feedback captured through the UI — the item flips to `accepted · reward N` (blended reward persisted) |
| 32-ambient-routines | Routines tab: status, trigger summary, autonomy, consecutive failures, last fired |
| 33-ambient-routine-drawer | routine drawer: trusted prompt, triggers JSON, pause/resume, fire-token lifecycle, delete |
| 34-ambient-watches | Watches tab: standing intents with compiled-rule echo, cadence/backoff state, confirm/pause/retire |
| 35-ambient-ledger | Ledger tab: per-category intervention precision (with override + revert) above the fire/hold audit with verdict reasons |

## §14c-26 (urgency-5 during quiet hours vs outside)

Run live inside the backend container (`proof_14c26.py`):

```
quiet-hours pass (20:14 inside quiet): flush -> {'interrupt': 0, 'notify': 0, 'digest': 0, 'demoted': 1}
--- A: during quiet hours (expect demoted tier=2, NOT delivered)
  urgency5 DURING quiet     tier=2 urgency=5 channel=-         delivered=NO
budget pass (budget=3): flush -> {'interrupt': 3, 'notify': 0, 'digest': 0, 'demoted': 1}
--- B: outside quiet (expect 3 interrupt-delivered, 1 demoted digest-lead)
  urgency5 OUTSIDE quiet #1 tier=0 urgency=5 channel=interrupt delivered=yes
  urgency5 OUTSIDE quiet #2 tier=0 urgency=5 channel=interrupt delivered=yes
  urgency5 OUTSIDE quiet #3 tier=0 urgency=5 channel=interrupt delivered=yes
  urgency5 OUTSIDE quiet #4 tier=2 urgency=5 channel=-         delivered=NO
```

The demoted items kept urgency 5, so they led the next digest flush (delivered
as one batch at the configured digest time, verified at 20:16:12 UTC). The
anticipation job fired unprompted during a real idle window (Qwen produced a
2-item briefing); accepting the ops-watchdog incident report returned reward
0.4096 (1.0 × 0.8⁴ repetition decay), dismissing the briefing returned −1.0.
