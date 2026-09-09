"""M16 measured goal (spec §12): does procedural learning cut the stage-30
fallback rate on indirect asks?

Design: warm up plan exemplars with two successful DIRECT-phrased asks that
route to sub agents, then send indirect paraphrases (the stage-30 finding-#4
shape) with procedural learning OFF vs ON and compare route rungs.

Usage: python fallback_experiment.py
"""

import asyncio
import json
import time
from typing import Any

import httpx

BASE = "http://localhost:8000/api/v1"

WARMUPS = [
    "Have the site-analyst summarize /tmp/site-notes.txt. Keep it brief.",
    "Use the workspace-warden: audit the workspace, one-line summary, move nothing.",
]

# indirect paraphrases — no capability named (the stage-30 shape that fell back)
# round 2: obliquer still — content words only, zero overlap with skill or
# sub-agent names/descriptions (round 1 never induced a fallback in either arm)
TEST_ASKS = [
    "Anything worrying come out of the line 3 walkthrough?",
    "Quick pulse check: what did the field visit turn up?",
    "Rough picture of what we've got lying around in the shared area?",
]


async def _wait(client: httpx.AsyncClient, run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        run = (await client.get(f"{BASE}/runs/{run_id}")).json()
        if run["status"] == "paused_hitl":
            await client.post(f"{BASE}/runs/{run_id}/hitl", json={"decision": "approve"})
            await asyncio.sleep(1.5)
            continue
        if run["status"] != "running":
            return run
        await asyncio.sleep(1.0)
    raise TimeoutError(run_id)


def _rungs(run: dict[str, Any]) -> list[str]:
    return [
        s["output"].get("rung")
        for s in run.get("steps", [])
        if s["step_type"] == "route" and (s.get("output") or {}).get("rung")
    ]


async def _ask(client: httpx.AsyncClient, message: str) -> list[str]:
    r = (await client.post(f"{BASE}/chat", json={"message": message})).json()
    run = await _wait(client, r["run_id"])
    detail = (await client.get(f"{BASE}/runs/{run['id']}")).json()
    return _rungs(detail)


async def main() -> None:
    async with httpx.AsyncClient(timeout=360) as client:
        (await client.post(f"{BASE}/memories/purge")).raise_for_status()
        (await client.delete(f"{BASE}/runs")).raise_for_status()
        await client.patch(
            f"{BASE}/settings",
            json={
                "memory_enabled": True,
                "memory_extraction_enabled": False,  # isolate the L3 effect
                "procedural_learning_enabled": True,
                "orchestrator_mode": "graph",
                "embedding_model": "fake:scripted",
                "memory_injection_budget_tokens": 1200,
                "memory_score_floor": 0.35,
            },
        )
        print("— warm-up (procedural ON so exemplars harvest) —", flush=True)
        for w in WARMUPS:
            rungs = await _ask(client, w)
            print(f"  warmup rungs={rungs}", flush=True)
            await asyncio.sleep(6)  # post-run harvest debounce

        results: dict[str, list[list[str]]] = {"off": [], "on": []}
        for mode, flag in (("off", False), ("on", True)):
            await client.patch(f"{BASE}/settings", json={"procedural_learning_enabled": flag})
            print(f"— indirect asks with procedural learning {mode.upper()} —", flush=True)
            for ask in TEST_ASKS:
                rungs = await _ask(client, ask)
                results[mode].append(rungs)
                print(f"  [{mode}] '{ask[:45]}…' rungs={rungs}", flush=True)

        def fallback_rate(rows: list[list[str]]) -> str:
            n = sum(1 for r in rows if "fallback" in r)
            return f"{n}/{len(rows)}"

        summary = {
            "fallback_rate_procedural_off": fallback_rate(results["off"]),
            "fallback_rate_procedural_on": fallback_rate(results["on"]),
            "detail": results,
        }
        print(json.dumps(summary, indent=2))
        with open("result_fallback.json", "w") as f:
            json.dump(summary, f, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
