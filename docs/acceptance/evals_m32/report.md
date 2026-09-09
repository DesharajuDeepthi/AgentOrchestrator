# M32 — Evals (§14c-31)

Live stack on `openrouter:qwen/qwen3.8-max`. One Playwright pass.

- `01-skill-drawer-launcher` — the seeded `web-research` skill drawer with
  its **Evals →** launcher.
- `02-dataset-uploaded` — a 3-case csv (`level,target_id,input,expected,
  judge_notes,grader`) uploaded through the Evals page; the dataset drawer
  lists the cases with their graders (exact / contains / llm_judge).
- `03-graded-results` — **Run eval** on the live model: **3/3 passed**.
  - `exact`: "capital of France" → answer "Paris" → PASS (normalized match)
  - `contains`: "chemical symbol for gold" → answer contains "Au" → PASS
  - `llm_judge`: sky-blue explanation graded by a structured verdict on the
    extraction role — PASS, score 1.00, reason: *"The answer correctly
    explains Rayleigh scattering of sunlight by atmospheric molecules …"*
    (the `judge_notes` guidance said Rayleigh wasn't required, any correct
    scattering explanation passes).

Every case ran as an ordinary Run, admin-direct as a single-skill ephemeral
worker (`eval_skill_id` set, no planner routing, rung-4 exposure exempt):

```
runs: is_eval=t, status=completed ×3
logs: {"event": "memory_digest", "eval": true, ...}   <- §10 label set
```

LangSmith publishing was skipped (langsmith_enabled=false, no key in this
sandbox) — per §15 that skips ONLY the publishing step; the Postgres run
traces + eval_results above are the full record.
