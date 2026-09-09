## 1. Dark-gate: unauthenticated /runs → 401 + security headers
HTTP/1.1 401 Unauthorized
date: Wed, 26 Aug 2026 00:13:53 GMT
server: uvicorn
content-length: 36
content-type: application/json
x-content-type-options: nosniff
x-frame-options: DENY
referrer-policy: no-referrer

{"detail":"authentication required"}
## 2. Exempt endpoints stay open: /health
GET /health -> 200

## 3. Admin login (bootstrap one-time password) → bearer token (redacted)
POST /auth/login -> token 9ZlZVlMz… (43 chars)

## 4. /auth/me as admin
{"id":"d9ebd865-2d3a-465a-b922-bd1c58e19b58","username":"admin","role":"admin","prefs":{}}

## 5. Admin creates member user 'mallory' (role=member)
{"id":"b9b5a939-2db5-40d7-8725-4204291d65fb","username":"mallory","role":"member"}
member login -> token _imPR7LN… (43 chars)

## 6. Admin chat run on the real LLM (openrouter:qwen/qwen3.8-max)
{"run_id":"ddb6055d-68e7-47e8-8a57-31379da31882","conversation_id":"d8a71dec-1e39-4f27-b71f-53185561fa39"}
admin run status: completed
final_answer: A bearer token protects API endpoints and resources from unauthorized access by serving as a credential that grants the holder (the "bearer") authenticated access to those resources.
tokens: 2109 in / 202 out

## 7. Tenancy — member cannot see admin's work
admin  GET /runs        -> 1 runs
member GET /runs        -> 0 runs
member GET /runs/ddb6055d-68e7-47e8-8a57-31379da31882 -> 404
member GET /conversations -> 0 conversations

## 8. Member creates a routine (owner=mallory); token minted separately, shown once
{'id': '90a81496-9906-492e-8bd6-fab011842e4d', 'name': 'mallory-ping', 'status': 'active'}
member POST /routines/90a81496-9906-492e-8bd6-fab011842e4d/token -> fire token issued once (amb_…, 47 chars, stored hashed)

## 9. Routine invisibility both ways + admin cannot mint tokens for it
member GET /routines -> ['mallory-ping']
admin  GET /routines -> []
admin  POST /routines/90a81496-9906-492e-8bd6-fab011842e4d/token -> 404

## 10. Token-auth fire (NO session): the fire token IS the auth (§18.8 exempt path)
{"status":"accepted","event_id":"1eb5b9dd-aed7-4117-b05b-1bd54031e816"}
fire with a WRONG token -> 401

## 11. The fire became a run OWNED by mallory (run task re-binds owner from Routine.user_id)
member GET /runs?routine_id ->  [('completed', None)]
admin  GET /runs?routine_id -> []
member total runs: 1 / admin total runs: 1

## 13. Admin-gated writes: member 403 on registry + settings, reads stay open; admin 200
member GET  /skills           -> 200 (reads open to any signed-in user)
member PATCH /skills/{id}     -> 403
member PATCH /settings        -> 403
admin  PATCH /skills/{id}     -> 200

## 12. Deliveries are per-owner (GET /api/v1/deliveries)
member GET /deliveries -> 1 item(s): [('[mallory-ping] pong', 2)]
admin  GET /deliveries -> 0 item(s)

## 14. SSE auth: EventSource cannot set headers, so streams accept ?token=
GET /ambient/stream (no token)  -> 401
GET /ambient/stream?token=…     -> 200

## 15. Per-user prefs overlay (quiet hours / digest times ride users.prefs)
{"detail":"Not Found"}

## 16. Token-bucket rate limit (burst 120, then 10/s) — 140 rapid /auth/me calls
sequential curl (slow: ~1 rps per process spawn) -> all 200, refill outpaces it
single-connection hammer, 200 requests -> {200: 112, 429: 88}  # burst ~120 then throttled

## 17. Byte-identity: AUTH_ENABLED off → same deployment reopens, NO auth artifacts
GET /runs (no auth header) ->
HTTP/1.1 200 OK
date: Wed, 26 Aug 2026 00:17:55 GMT
server: uvicorn
content-length: 110204
content-type: application/json

[…full run list JSON, 110KB — every run visible again with no auth header…]
runs visible unauthenticated: 47 (single-user mode sees every row again)
