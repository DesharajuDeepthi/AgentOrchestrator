# M34 acceptance — auth + tenancy + hardening (spec §18.8, §14c-32)

Live stack: fresh M34 images over the persistent milestone volume, real LLM
(`openrouter:qwen/qwen3.8-max`) for every run in this proof. Full curl-level
evidence in `transcript.md`; screenshots numbered in order.

## What was proven live

1. **Dark gate** — with `AUTH_ENABLED=1`, unauthenticated `/api/v1/runs` → 401
   with the security-header set (`x-content-type-options`, `x-frame-options`,
   `referrer-policy`); `/health` stays open (transcript §1–2).
2. **Bootstrap admin** — first boot with auth on created `admin` and printed a
   one-time password exactly once in the boot log (`auth_bootstrap_admin`);
   login with it returned a 43-char bearer token (stored sha256-hashed).
3. **User admin** — admin created member `mallory` (role=member); she logged in
   with her own credentials (transcript §5).
4. **Real-LLM run under an identity** — admin ran a chat on qwen3.8-max end to
   end (completed, 2109 in / 202 out tokens, sensible answer) (§6).
5. **Tenancy both ways** —
   - member `GET /runs` → 0, direct fetch of admin's run → 404 (§7);
   - member created routine `mallory-ping`; admin `GET /routines` → `[]` and
     admin cannot mint fire tokens for it (404) (§8–9);
   - legacy pre-auth rows (user_id NULL) are visible to NO ONE while auth is
     on — strict `user_id =` scoping, no NULL fallback for work rows (memories
     keep a deliberate NULL fallback as shared knowledge).
6. **Ambient ownership end to end** — the routine was fired with ONLY its fire
   token (no session; the token IS the auth on that §18.8-exempt path; wrong
   token → 401). The fire became a completed run owned by mallory — visible in
   her `GET /runs?routine_id=…`, invisible in admin's — and produced delivery
   "[mallory-ping] pong" in HER inbox only (member 1 / admin 0) (§10–12,
   screenshots 03/04).
7. **Admin-gated writes** — member: registry PATCH 403, settings PATCH 403;
   reads stay open to any signed-in user; admin PATCH 200 (§13).
8. **SSE auth** — `/ambient/stream` without token → 401, with `?token=` → 200
   (EventSource cannot set headers) (§14).
9. **Per-user prefs** — `PATCH /users/me/prefs` stored mallory's
   `ambient_quiet_hours` overlay (§15).
10. **Rate limit** — single-connection hammer of 200 requests: 112×200 +
    88×429 (burst ≈120, then the 10/s refill) (§16).
11. **Login UI** — fresh browser hit the app, the 401 raised the LoginGate
    overlay (01); signing in as mallory landed in the normal UI (02) showing
    only her run (03) and her delivery (04).
12. **Byte-identity** — `AUTH_ENABLED` unset, backend recreated: `/runs` → 200
    with no auth header, no security headers, all 47 rows (both users' work +
    legacy rows) visible again — single-user behavior restored (§17).

## Honest scoping notes

- The global digest-time learner stands down when auth is on (per-user
  learning continues; the global anchor shift is single-user machinery).
- `?token=` on SSE URLs is a deliberate tradeoff (EventSource has no header
  API); tokens ride TLS in real deployments and are hashed at rest.
- Admin role gates registry/settings WRITES; it does not grant visibility
  into other users' work rows — there is no cross-user read path.
- Legacy NULL-owned work rows are orphaned while auth is on (visible again
  the moment auth is off). A claim/backfill tool was consciously not built.

## Suite status

Full backend suite (auth off, fake provider): **684 passed, 1 skipped**.
Getting there surfaced one pre-existing test-infra leak that M34 exposed:
sse-starlette monkey-patches `uvicorn.Server.handle_exit`, so the in-process
uvicorn the MCP http-transport tests start/stop sets
`AppStatus.should_exit = True` process-wide — after which every later
`EventSourceResponse` believes the server is draining. Pre-M34 the SSE
replay won the race anyway; the AuthMiddleware hop reordered task startup so
the poisoned flag won and the stream returned empty (`anyio.EndOfStream`).
Fixed with an autouse conftest fixture resetting the flag per test; the app
itself was correct. (An ENOSPC red herring was also diagnosed on the way — a
full disk fails structlog's stdout write and kills run tasks mid-suite.)
`tests/test_m34_auth.py` (11 tests) covers hashing, sessions, the gate +
exemptions, admin gating, scoping helpers, ambient ownership, and
byte-identity with auth off.
