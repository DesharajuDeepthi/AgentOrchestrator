# Spec Amendment — §19 A2A Outbound + M37–M39

Draft merged into `spec.md` in the same commit as this doc (the memory/
ambient waves' 06-doc pattern). Insertion points: `## 19.` appended at end
of file; M37–M39 rows after the M36 row in the headerless §12 block; §3.7
keys appended inside the settings paragraph under "plus the §19 keys:";
`### 8.10` after §8.9; `**§14d**` bold paragraph after §14c step 32 with
global numbering continuing at 33. **No new env vars** (credentials are
per-agent registry data with `env:VAR` indirection; no new services), so
§13 is untouched.

Sign-off decisions this amendment encodes (from the brainstorm dialogue):
outbound-only with inbound-shaped task states; official `a2a-sdk`
`>=0.3,<0.4` confined to `backend/app/a2a/`; auth = apiKey + http
bearer/basic + oauth2 client_credentials (authlib), interactive flows
deferred; credentials masked write-only with env indirection; remote
agents as a registry peer of MCP servers projecting per-skill tools
(`kind='a2a'`); `input-required` ⇒ HITL; park-on-budget ⇒ ambient poller
⇒ delivery; `a2a_enabled` dark by default.

The verbatim §19 text, milestone rows, §3.7 additions, §8.10, and §14d
steps live in `spec.md` — this doc records the insertion mechanics and
the decisions; the architecture rationale is doc 05; SDK ground truth is
doc 02; the reuse inventory is doc 01.

## Milestone rows (as merged)

| M37 | A2A substrate (§19.1–19.4): `remote_agents` registry + card fetch/refresh, `a2a-sdk` isolated in `app/a2a/`, credential store (masked write-only, `env:` indirection) + scheme dispatch (apiKey/basic/bearer/oauth2 client_credentials), per-card-skill tools projection `kind='a2a'`, Remote Agents UI page, scripted in-process A2A counterparty + contract tests | byte-identity with a2a off; §14d-33..35 |
| M38 | A2A execution (§19.5): lazy call-time proxy via `materialize_tool`, streaming+polling consumption, all nine task states mapped, `input-required` ⇄ HITL gate with replay-idempotent task adoption, untrusted-fenced outputs, Stop → `tasks/cancel`, `a2a` step labels (+ direct-tool kind-label fix) | §14d-36..38 |
| M39 | A2A long-running (§19.6): park-on-budget, ambient leader-tick poller → outbox deliveries, task drawer reply/cancel, ExComm demo composition | §14d-39..40 |

## Open questions (none blocking)

- Push-notification receiver (remote agent calls us back instead of our
  poller) is deliberately deferred past M39 — the poller covers the POC;
  the fire-token endpoint pattern is the ready-made shape when wanted.
- Serving our own Agent Card (inbound) remains out of scope; the task
  state mapping and the SDK server half (already used by tests) make it
  additive later.
