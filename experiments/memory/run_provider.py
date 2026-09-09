"""Cross-provider battery driver (M19): point every model role at one
model ref, run the routing experiment + memory off/semantic harness, and
tag the result files. One invocation per model; results become the
single-table provider comparison.

Usage: python run_provider.py <tag> <provider:model>
"""

import asyncio
import os
import subprocess
import sys

import httpx

BASE = "http://localhost:8000/api/v1"
HERE = os.path.dirname(os.path.abspath(__file__))


async def set_model(ref: str) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.patch(
            f"{BASE}/settings",
            json={
                "default_model": ref,
                "default_model_params": {"effort": "low"},
                "planner_model": ref,
                "planner_model_params": {"effort": "medium"},
                "aggregator_model": None,
                "formatter_model": None,
                "memory_extraction_model": None,
            },
        )
        resp.raise_for_status()
        got = (await client.get(f"{BASE}/settings")).json()
        assert got["default_model"] == ref, got["default_model"]
        print(f"== all roles → {ref}", flush=True)


def run(script: str, *args: str) -> int:
    return subprocess.call([sys.executable, os.path.join(HERE, script), *args], cwd=HERE)


def keep(src: str, dst: str) -> None:
    p = os.path.join(HERE, src)
    if os.path.exists(p):
        os.replace(p, os.path.join(HERE, dst))


def main() -> None:
    tag, ref = sys.argv[1], sys.argv[2]
    asyncio.run(set_model(ref))
    print(f"=== [{tag}] fallback/routing ===", flush=True)
    rc = run("fallback_experiment.py")
    if rc == 0:
        keep("result_fallback.json", f"result_fallback_{tag}.json")
    else:
        print(f"  [{tag}] fallback experiment exited rc={rc} (no result kept)", flush=True)
    for config in ("off", "semantic"):
        print(f"=== [{tag}] harness {config} ===", flush=True)
        rc = run("harness.py", config)
        if rc == 0:
            keep(f"result_{config}.json", f"result_{config}_{tag}.json")
        else:
            print(f"  [{tag}] harness {config} exited rc={rc}", flush=True)


if __name__ == "__main__":
    main()
