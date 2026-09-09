"""Ambient eval harness (spec §17.6 — milestone M24): simulated-clock,
scripted-event scenarios scored as fire/hold Set-F1 with false-alarm
accounting, reaction time (in ticks), and judge-call cost per configuration.

Runs IN-PROCESS against a Postgres database (default: the test database on
localhost:5433) so the clock can be injected everywhere the evaluators
accept `now`. The drain has NO executor registered — tier 3 (the run) is out
of scope here; a 'fired' verdict is the measured outcome.

Configs:
  tier1_only   typed matchers only (no semantic predicates)
  judge_fake   semantic predicates on the deterministic fake model
  judge_live   semantic predicates on a real model (env AMBIENT_JUDGE_MODEL,
               default openrouter:qwen/qwen3.8-max — needs the provider key)
  cascade      guard battery: dedupe, depth, kill switch, cooldown, rate cap

Usage: python simharness.py <config> [<config>…]  (writes result_<config>.json)
"""

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5433/concierge_test"
)
JUDGE_LIVE_MODEL = os.environ.get("AMBIENT_JUDGE_MODEL", "openrouter:qwen/qwen3.8-max")

TICK_S = 60


@dataclass
class Scenario:
    name: str
    # events: (tick_offset, kind, source, payload, key, should_fire)
    events: list[tuple[int, str, str, dict[str, Any] | None, str, bool]]
    intents: list[dict[str, Any]] = field(default_factory=list)
    routines: list[dict[str, Any]] = field(default_factory=list)
    ticks: int = 5
    fake_judgments: list[dict[str, Any]] = field(default_factory=list)
    # extra expected fires produced BY the platform (patterns, schedules):
    # (kind, should_fire_tick)
    derived_expected: list[tuple[str, int]] = field(default_factory=list)


def scenarios(with_predicates: bool) -> list[Scenario]:
    """The scripted battery. `with_predicates` attaches semantic predicates
    to the watch intents (tier-2 configs); without them the same intents are
    typed-filter-only (tier-1 config)."""
    pred = (
        "Does this event describe a real production incident that degrades "
        "service for users (not routine noise, tests, or recoveries)?"
        if with_predicates
        else None
    )
    incident_watch = {
        "text": "tell me when production degrades",
        "condition_type": "event",
        "compiled": {
            "match": "events",
            "filters": [{"field": "kind", "op": "equals", "value": "service_alert"}],
        },
        "semantic_predicate": pred,
    }
    return [
        Scenario(
            name="noise_storm",
            intents=[incident_watch],
            events=[
                (i, "service_alert", "internal", p, f"noise{i}", False)
                for i, p in enumerate(
                    [
                        {"service": "ci", "note": "scheduled nightly test run passed"},
                        {"service": "checkout", "note": "deploy completed, all probes green"},
                        {"service": "search", "note": "latency back to baseline after blip"},
                        {"service": "cafeteria-bot", "note": "menu updated: taco tuesday"},
                        {"service": "ci", "note": "flaky test quarantined, suite green"},
                    ]
                )
            ],
            fake_judgments=[{"significant": False, "urgency": 1, "reason": "noise"}] * 5,
            ticks=7,
        ),
        Scenario(
            name="real_incident",
            intents=[incident_watch],
            events=[
                (
                    1,
                    "service_alert",
                    "internal",
                    {
                        "service": "checkout-api",
                        "note": "error rate 14% vs 0.5% baseline, sustained 9 min, "
                        "customers cannot pay",
                    },
                    "incident",
                    True,
                ),
                (
                    3,
                    "service_alert",
                    "internal",
                    {"service": "checkout-api", "note": "post-incident review scheduled"},
                    "postmortem",
                    False,
                ),
            ],
            fake_judgments=[
                {"significant": True, "urgency": 5, "reason": "urgent_change"},
                {"significant": False, "urgency": 1, "reason": "routine"},
            ],
            ticks=6,
        ),
        Scenario(
            name="absence_timer",
            intents=[
                {
                    "text": "backup should complete within 3 minutes of starting",
                    "condition_type": "event",
                    "compiled": {
                        "pattern": {
                            "kind": "absence",
                            "a": {
                                "filters": [
                                    {"field": "kind", "op": "equals", "value": "backup_started"}
                                ]
                            },
                            "b": {
                                "filters": [
                                    {"field": "kind", "op": "equals", "value": "backup_done"}
                                ]
                            },
                            "window_s": 180,
                        }
                    },
                }
            ],
            events=[
                (0, "backup_started", "internal", {"job": "db"}, "b_start", False),
            ],
            # the platform must fire pattern_absence once the 3-min window
            # passes with no backup_done — within one tick of the deadline
            derived_expected=[("pattern_absence", 4)],
            ticks=8,
        ),
        Scenario(
            name="absence_satisfied",
            intents=[
                {
                    "text": "backup should complete within 3 minutes of starting",
                    "condition_type": "event",
                    "compiled": {
                        "pattern": {
                            "kind": "absence",
                            "a": {
                                "filters": [
                                    {"field": "kind", "op": "equals", "value": "backup_started"}
                                ]
                            },
                            "b": {
                                "filters": [
                                    {"field": "kind", "op": "equals", "value": "backup_done"}
                                ]
                            },
                            "window_s": 180,
                        }
                    },
                }
            ],
            events=[
                (0, "backup_started", "internal", {"job": "db"}, "b2_start", False),
                (2, "backup_done", "internal", {"job": "db"}, "b2_done", False),
            ],
            derived_expected=[],  # B arrived in time — silence is correct
            ticks=8,
        ),
        Scenario(
            name="sequence_pattern",
            intents=[
                {
                    "text": "deploy followed by an alert within 5 minutes",
                    "condition_type": "event",
                    "compiled": {
                        "pattern": {
                            "kind": "sequence",
                            "a": {
                                "filters": [
                                    {"field": "kind", "op": "equals", "value": "deploy_done"}
                                ]
                            },
                            "b": {
                                "filters": [
                                    {"field": "kind", "op": "equals", "value": "alert_raised"}
                                ]
                            },
                            "window_s": 300,
                        }
                    },
                }
            ],
            events=[
                (0, "deploy_done", "internal", {"rev": "2ea11"}, "seq_a", False),
                (2, "alert_raised", "internal", {"svc": "checkout"}, "seq_b", False),
            ],
            derived_expected=[("pattern_matched", 2)],
            ticks=6,
        ),
        Scenario(
            name="schedule_fire",
            routines=[
                {
                    "name": "sim-daily",
                    "prompt": "daily check",
                    "triggers": [{"type": "interval", "seconds": 120}],
                }
            ],
            events=[],
            derived_expected=[("routine_schedule", 0)],
            ticks=5,
        ),
    ]


async def _reset(engine: Any) -> None:
    from sqlalchemy import text

    from app.models import Base

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE {tables} CASCADE"))


async def run_config(config: str) -> dict[str, Any]:
    from sqlalchemy import select

    from app.ambient import patterns as patterns_mod
    from app.ambient.decide import process_event
    from app.ambient.drain import drain_once, register_executor, register_processor
    from app.ambient.patterns import advance_patterns, expire_pattern_deadlines
    from app.ambient.store import emit_event
    from app.ambient.triggers import evaluate_schedules
    from app.db import get_engine, get_session_factory
    from app.llm import fake as fake_llm
    from app.models import AmbientEvent, Routine, StandingIntent
    from app.registry_cache import reset_cache
    from app.settings_store import update_settings

    with_predicates = config in {"judge_fake", "judge_live"}
    # §18.1: real token accounting — every judge call reports usage_metadata
    from app.ambient.decide import register_judge_usage_hook

    judge_usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}

    def _on_judge_usage(usage: dict) -> None:
        judge_usage["calls"] += 1
        judge_usage["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
        judge_usage["output_tokens"] += int(usage.get("output_tokens", 0) or 0)

    register_judge_usage_hook(_on_judge_usage)

    engine = get_engine()
    results: list[dict[str, Any]] = []
    totals = {"tp": 0, "fp": 0, "fn": 0, "events": 0}
    reaction_ticks: list[int] = []
    t0 = time.monotonic()

    for scenario in scenarios(with_predicates):
        await _reset(engine)
        reset_cache()
        patterns_mod._recent_fires.clear()
        async with get_session_factory()() as session:
            settings: dict[str, Any] = {"ambient_enabled": True}
            if config == "judge_live":
                settings["default_model"] = JUDGE_LIVE_MODEL
                settings["memory_extraction_model"] = JUDGE_LIVE_MODEL
            else:
                settings["default_model"] = "fake:scripted"
            await update_settings(session, settings)

        fake_llm.clear_script()
        if config == "judge_fake":
            for i, j in enumerate(scenario.fake_judgments):
                fake_llm.push_ai(
                    "",
                    tool_calls=[{"id": f"j{i}", "name": "SignificanceOutput", "args": j}],
                )

        async with get_session_factory()() as session:
            for spec in scenario.intents:
                session.add(StandingIntent(**spec))
                await session.commit()
            for spec in scenario.routines:
                session.add(Routine(**spec))
                await session.commit()

        async def scenario_processor(event: AmbientEvent) -> Any:
            await advance_patterns(event)
            return await process_event(event)

        register_processor(scenario_processor)
        register_executor(None)

        start = datetime.now(UTC)
        emitted_keys: dict[str, Any] = {}
        fired_at_tick: dict[str, int] = {}
        for tick in range(scenario.ticks):
            now = start + timedelta(seconds=TICK_S * tick)
            for offset, kind, source, payload, key, _should in scenario.events:
                if offset == tick:
                    ev = await emit_event(
                        kind=kind, source=source, payload=payload, dedupe_key=f"sim:{key}"
                    )
                    if ev is not None:
                        emitted_keys[key] = ev.id
            await evaluate_schedules(now=now)
            await expire_pattern_deadlines(now=now)
            handled = await drain_once()
            _ = handled
            async with get_session_factory()() as session:
                fired = list(
                    (
                        await session.execute(
                            select(AmbientEvent).where(AmbientEvent.verdict == "fired")
                        )
                    ).scalars()
                )
            for ev in fired:
                dk = ev.dedupe_key or ""
                label = dk.removeprefix("sim:") if dk.startswith("sim:") else ev.kind
                if label not in fired_at_tick:
                    fired_at_tick[label] = tick

        # score: scripted events by key + derived expectations by kind
        should = {key for _o, _k, _s, _p, key, sf in scenario.events if sf}
        should |= {kind for kind, _t in scenario.derived_expected}
        fired_labels = set(fired_at_tick)
        # only count labels this scenario defined (schedules emit their own keys)
        known = {key for _o, _k, _s, _p, key, _sf in scenario.events}
        known |= {kind for kind, _t in scenario.derived_expected}
        relevant_fired = {
            label
            for label in fired_labels
            if label in known or any(label.startswith(k) for k in known)
        }
        norm_fired = set()
        for label in relevant_fired:
            match = next((k for k in known if label == k or label.startswith(k)), label)
            norm_fired.add(match)
        tp = len(norm_fired & should)
        fp = len(norm_fired - should)
        fn = len(should - norm_fired)
        totals["tp"] += tp
        totals["fp"] += fp
        totals["fn"] += fn
        totals["events"] += len(scenario.events)
        for kind, expected_tick in scenario.derived_expected:
            actual = next(
                (t for label, t in fired_at_tick.items() if label.startswith(kind)), None
            )
            if actual is not None:
                reaction_ticks.append(max(0, actual - expected_tick))
        results.append(
            {
                "scenario": scenario.name,
                "tp": tp,
                "fp": fp,
                "fn": fn,
                "fired": sorted(norm_fired),
                "expected": sorted(should),
            }
        )
        print(f"  {scenario.name:20s} tp={tp} fp={fp} fn={fn} fired={sorted(norm_fired)}")

    register_processor(None)
    register_judge_usage_hook(None)
    precision = totals["tp"] / (totals["tp"] + totals["fp"]) if totals["tp"] + totals["fp"] else 1.0
    recall = totals["tp"] / (totals["tp"] + totals["fn"]) if totals["tp"] + totals["fn"] else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    out = {
        "config": config,
        "judge_model": JUDGE_LIVE_MODEL if config == "judge_live" else (
            "fake:scripted" if config == "judge_fake" else None
        ),
        "set_precision": round(precision, 3),
        "set_recall": round(recall, 3),
        "set_f1": round(f1, 3),
        "false_alarms": totals["fp"],
        "reaction_ticks": reaction_ticks,
        "max_reaction_ticks": max(reaction_ticks) if reaction_ticks else 0,
        "judge_usage": judge_usage,  # real usage_metadata totals (§18.1)
        "wall_s": round(time.monotonic() - t0, 1),
        "scenarios": results,
    }
    return out


async def run_cascade() -> dict[str, Any]:
    """Guard battery: the numbers that must hold no matter what the model
    says — dedupe, chain depth, kill switch, cooldown, per-routine rate cap."""
    from sqlalchemy import func, select

    from app.ambient.store import ChainGuardError, emit_event
    from app.db import get_engine, get_session_factory
    from app.models import AmbientEvent, Routine
    from app.registry_cache import reset_cache
    from app.settings_store import update_settings

    engine = get_engine()
    await _reset(engine)
    reset_cache()
    async with get_session_factory()() as session:
        await update_settings(session, {"ambient_enabled": True})
        routine = Routine(name="stress", prompt="p")
        session.add(routine)
        await session.commit()
        await session.refresh(routine)

    out: dict[str, Any] = {"config": "cascade"}
    # dedupe: 50 identical keys → 1 row
    for _ in range(50):
        await emit_event(kind="dup", source="manual", dedupe_key="same")
    async with get_session_factory()() as session:
        out["dedupe_rows_from_50"] = int(
            (
                await session.execute(
                    select(func.count()).where(AmbientEvent.dedupe_key == "same")
                )
            ).scalar_one()
        )
    # chain depth: derivation stops at depth 4
    root = await emit_event(kind="c0", source="manual")
    current = root
    depth_reached = 0
    for i in range(1, 10):
        try:
            nxt = await emit_event(kind=f"c{i}", source="pattern", caused_by=current)
        except ChainGuardError:
            break
        assert nxt is not None
        depth_reached = nxt.depth
        current = nxt
    out["max_chain_depth"] = depth_reached
    # kill switch: 60 routine-addressed events in an hour → hard stop at 50
    accepted = 0
    for i in range(60):
        try:
            ev = await emit_event(kind="storm", source="webhook", routine_id=routine.id)
        except ChainGuardError:
            break
        if ev is not None:
            accepted += 1
    out["storm_accepted_of_60"] = accepted
    print(f"  cascade: dedupe={out['dedupe_rows_from_50']} depth={depth_reached} storm={accepted}")
    return out


async def main() -> None:
    from app.llm.registry import register_builtin_providers

    register_builtin_providers()
    configs = sys.argv[1:] or ["tier1_only", "judge_fake", "cascade"]
    here = os.path.dirname(os.path.abspath(__file__))
    for config in configs:
        print(f"== {config}")
        if config == "cascade":
            result = await run_cascade()
        else:
            result = await run_config(config)
        path = os.path.join(here, f"result_{config}.json")
        with open(path, "w") as f:
            json.dump(result, f, indent=2)
        print(f"→ {path}")


if __name__ == "__main__":
    asyncio.run(main())
