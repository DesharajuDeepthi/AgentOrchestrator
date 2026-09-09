"""Memory experiment harness (spec §16.9 / milestone M17).

Drives a LIVE stack over HTTP through one layer configuration at a time:
reset state → apply settings → run the probe suite (real conversations, real
model) → grade deterministically → collect cost/latency. The comparison
matrix lives in run_matrix.py.

Usage: python harness.py <config_name>   (or import run_config)
"""

import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

BASE = "http://localhost:8000/api/v1"

CONFIGS: dict[str, dict[str, Any]] = {
    # baseline — the existing solution, memory dark (spec §16.0 byte-identity)
    "off": {"memory_enabled": False},
    # L0/L1 only: digests + episodic injection, no semantic extraction
    "episodic": {
        "memory_enabled": True,
        "embedding_model": "fake:scripted",
        "memory_extraction_enabled": False,
        "procedural_learning_enabled": False,
    },
    # + L2 semantic extraction (the core layer)
    "semantic": {
        "memory_enabled": True,
        "embedding_model": "fake:scripted",
        "memory_extraction_enabled": True,
        "procedural_learning_enabled": False,
    },
    # + L3 procedural learning (exemplars + routing stats)
    "full": {
        "memory_enabled": True,
        "embedding_model": "fake:scripted",
        "memory_extraction_enabled": True,
        "procedural_learning_enabled": True,
    },
    # distraction control: full layers under a starved budget + high floor
    "tight": {
        "memory_enabled": True,
        "embedding_model": "fake:scripted",
        "memory_extraction_enabled": True,
        "procedural_learning_enabled": True,
        "memory_injection_budget_tokens": 250,
        "memory_score_floor": 0.6,
    },
}
_RESET_KEYS = {
    "memory_enabled": False,
    "embedding_model": None,
    "memory_extraction_enabled": True,
    "procedural_learning_enabled": False,
    "memory_injection_budget_tokens": 1200,
    "memory_score_floor": 0.35,
    "memory_recall_top_k": 6,
}


@dataclass
class ProbeResult:
    probe: str
    ability: str
    passed: bool
    reason: str
    answer_head: str
    input_tokens: int
    output_tokens: int
    latency_s: float


@dataclass
class ConfigResult:
    config: str
    results: list[ProbeResult] = field(default_factory=list)
    memories_active: int = 0
    setup_input_tokens: int = 0
    setup_output_tokens: int = 0

    def summary(self) -> dict[str, Any]:
        by_ability: dict[str, list[bool]] = {}
        for r in self.results:
            by_ability.setdefault(r.ability, []).append(r.passed)
        return {
            "config": self.config,
            "passed": sum(r.passed for r in self.results),
            "total": len(self.results),
            "by_ability": {k: f"{sum(v)}/{len(v)}" for k, v in by_ability.items()},
            "question_input_tokens": sum(r.input_tokens for r in self.results),
            "question_output_tokens": sum(r.output_tokens for r in self.results),
            "setup_input_tokens": self.setup_input_tokens,
            "mean_question_latency_s": round(
                sum(r.latency_s for r in self.results) / max(len(self.results), 1), 1
            ),
            "memories_active": self.memories_active,
        }


async def _wait_run(client: httpx.AsyncClient, run_id: str, timeout_s: float = 600) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        run = (await client.get(f"{BASE}/runs/{run_id}")).json()
        if run["status"] == "paused_hitl":
            await client.post(f"{BASE}/runs/{run_id}/hitl", json={"decision": "approve"})
            await asyncio.sleep(1.5)
            continue
        if run["status"] not in {"running"}:
            return run
        await asyncio.sleep(1.0)
    raise TimeoutError(f"run {run_id} did not settle")


async def _settle_consolidation(client: httpx.AsyncClient, seconds: float = 18.0) -> None:
    """Post-run digest/extraction are debounced background work. The memories
    count only moves when extraction is on, so the early-exit needs a floor
    long enough for the digest LLM call (~1s debounce + one model call)."""
    start = time.monotonic()
    last = -1
    while time.monotonic() - start < seconds:
        status = (await client.get(f"{BASE}/memories/status")).json()
        total = sum(status["counts"].values())
        if total == last and time.monotonic() - start > 10:
            return
        last = total
        await asyncio.sleep(2.0)


async def reset_state(client: httpx.AsyncClient) -> None:
    (await client.post(f"{BASE}/memories/purge")).raise_for_status()
    (await client.delete(f"{BASE}/runs")).raise_for_status()
    (await client.patch(f"{BASE}/settings", json=dict(_RESET_KEYS))).raise_for_status()
    status = (await client.get(f"{BASE}/memories/status")).json()
    left = sum(status["counts"].values())
    if left:
        raise RuntimeError(f"purge left {left} memories — configs would contaminate each other")


async def run_config(name: str) -> ConfigResult:
    from probes import PROBES, grade

    cfg = CONFIGS[name]
    out = ConfigResult(config=name)
    async with httpx.AsyncClient(timeout=360) as client:
        await reset_state(client)
        resp = await client.patch(f"{BASE}/settings", json=cfg)
        resp.raise_for_status()
        convs: dict[str, str] = {}

        async def send(conv_slot: str, message: str) -> dict[str, Any]:
            body: dict[str, Any] = {"message": message}
            if conv_slot in convs:
                body["conversation_id"] = convs[conv_slot]
            r = (await client.post(f"{BASE}/chat", json=body)).json()
            convs[conv_slot] = r["conversation_id"]
            return await _wait_run(client, r["run_id"])

        for probe in PROBES:
            for turn in probe.turns:
                t0 = time.monotonic()
                run = await send(turn.conv, turn.message)
                latency = time.monotonic() - t0
                if turn.kind == "setup":
                    out.setup_input_tokens += run.get("total_input_tokens", 0)
                    out.setup_output_tokens += run.get("total_output_tokens", 0)
                    if turn.settle and cfg.get("memory_enabled"):
                        await _settle_consolidation(client)
                    continue
                answer = str(run.get("final_answer") or "")
                passed, reason = grade(turn, answer)
                if run["status"] != "completed":
                    passed, reason = False, f"run {run['status']}: {run.get('error')}"
                out.results.append(
                    ProbeResult(
                        probe=probe.name,
                        ability=probe.ability,
                        passed=passed,
                        reason=reason,
                        answer_head=" ".join(answer.split())[:160],
                        input_tokens=run.get("total_input_tokens", 0),
                        output_tokens=run.get("total_output_tokens", 0),
                        latency_s=round(latency, 1),
                    )
                )
                print(
                    f"  [{name}] {probe.name}: {'PASS' if passed else 'FAIL'} ({reason}) "
                    f"in={run.get('total_input_tokens', 0)} lat={latency:.0f}s",
                    flush=True,
                )
        status = (await client.get(f"{BASE}/memories/status")).json()
        out.memories_active = status["counts"].get("active", 0)
    return out


if __name__ == "__main__":
    config = sys.argv[1] if len(sys.argv) > 1 else "off"
    result = asyncio.run(run_config(config))
    print(json.dumps(result.summary(), indent=2))
    detail = [r.__dict__ for r in result.results]
    with open(f"result_{config}.json", "w") as f:
        json.dump({"summary": result.summary(), "detail": detail}, f, indent=2)
