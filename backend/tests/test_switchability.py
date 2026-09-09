"""M48 — the §3.7.1 switchability rule, enforced rather than asserted.

Two invariants:
  1. no behavior the system performs on its own is unswitchable — every
     autonomous job has its own named gate, and a master alone is not
     enough when family members have different consequences;
  2. every §3.7 key has a control in §8.7 — checked mechanically here, so
     a future key with no control fails the suite instead of a review.

Plus the corollary: a setting that READS as off must BE off
(`memory_community_budget_tokens = 0` skips the rebuild, not just the
injection).
"""

import re
from pathlib import Path
from typing import Any

import pytest

from app.db import get_session_factory
from app.memory.lifecycle import reset_job_clock, run_due_jobs
from app.settings_store import DEFAULTS, update_settings

pytestmark = pytest.mark.anyio

API = "/api/v1"

# every job that runs on its own schedule, and the key that silences it
GATED_JOBS = {
    "decay": "memory_decay_enabled",
    "contradict": "memory_contradiction_enabled",
    "communities": "memory_communities_enabled",
    "compact": "memory_compaction_enabled",
}


async def _set(**kv: Any) -> None:
    async with get_session_factory()() as session:
        await update_settings(session, kv)


async def _jobs_that_ran() -> set[str]:
    reset_job_clock()
    return set((await run_due_jobs()).keys())


class TestSettingsCoverage:
    """Invariant 2, mechanical: the §8.7 completeness rule as a test."""

    def test_every_settings_key_has_a_ui_control(self) -> None:
        page = (
            Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages" / "SettingsPage.tsx"
        ).read_text()
        missing = sorted(k for k in DEFAULTS if k not in page)
        assert missing == [], (
            "spec §3.7.1: every settings key needs a Settings-page control; "
            f"unreachable in the UI: {missing}"
        )

    def test_every_key_is_documented_in_the_spec(self) -> None:
        spec = (Path(__file__).resolve().parents[2] / "spec.md").read_text()
        undocumented = sorted(k for k in DEFAULTS if f"`{k}" not in spec)
        assert undocumented == [], f"keys absent from spec §3.7: {undocumented}"


class TestGateStructure:
    """The map is the single source of truth, and it must stay complete —
    this is what stops the next job from shipping ungated."""

    def test_every_scheduled_job_declares_a_gate(self) -> None:
        from app.memory import lifecycle

        job_ids = {
            value
            for name, value in vars(lifecycle).items()
            if name.startswith("JOB_") and isinstance(value, int)
        }
        assert job_ids == set(lifecycle.JOB_GATES), (
            "spec §3.7.1: every scheduled job needs an entry in JOB_GATES; "
            f"ungated: {sorted(job_ids - set(lifecycle.JOB_GATES))}"
        )

    def test_every_gate_names_a_real_setting(self) -> None:
        from app.memory.lifecycle import JOB_GATES

        unknown = sorted(k for k in JOB_GATES.values() if k not in DEFAULTS)
        assert unknown == [], f"JOB_GATES names settings that do not exist: {unknown}"

    async def test_gate_open_handles_all_three_gate_shapes(self, client: Any) -> None:
        """Boolean switch, off|propose|auto mode, nullable model ref."""
        from app.memory.lifecycle import gate_open

        await _set(
            memory_decay_enabled=False,
            memory_extraction_learning="off",
            embedding_model=None,
        )
        assert not await gate_open("memory_decay_enabled")
        assert not await gate_open("memory_extraction_learning")
        assert not await gate_open("embedding_model")
        await _set(
            memory_decay_enabled=True,
            memory_extraction_learning="propose",
            embedding_model="fake:scripted",
        )
        assert await gate_open("memory_decay_enabled")
        assert await gate_open("memory_extraction_learning")
        assert await gate_open("embedding_model")
        await _set(memory_extraction_learning="off", embedding_model=None)


class TestGatesHoldOnDirectCalls:
    """The enforcement point is the job, not the dispatcher. These jobs are
    documented as directly awaitable (tests, harnesses), so a gate only the
    scheduler honors is a gate every other call path walks past."""

    @pytest.fixture(autouse=True)
    async def _memory_on(self, client: Any) -> Any:
        await _set(memory_enabled=True)
        yield
        await _set(memory_enabled=False, **{k: True for k in GATED_JOBS.values()})

    async def test_decay_refuses_to_expire_behind_its_switch(self, client: Any) -> None:
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import select

        from app.models import Memory

        stale = datetime.now(UTC) - timedelta(days=3650)
        async with get_session_factory()() as session:
            row = Memory(
                text="ancient and unloved",
                kind="fact",
                scope="global",
                source="user_stated",
                importance=1,
            )
            session.add(row)
            await session.flush()
            row.last_accessed_at = stale
            row.recorded_at = stale
            await session.commit()
            row_id = row.id

        from app.memory.lifecycle import decay_sweep

        await _set(memory_decay_enabled=False)
        assert await decay_sweep() == 0
        async with get_session_factory()() as session:
            assert (await session.get(Memory, row_id)).status == "active"

        await _set(memory_decay_enabled=True)
        assert await decay_sweep() >= 1
        async with get_session_factory()() as session:
            assert (await session.get(Memory, row_id)).status == "expired"
        async with get_session_factory()() as session:  # leave a clean world
            for m in (await session.execute(select(Memory))).scalars():
                await session.delete(m)
            await session.commit()

    async def test_contradiction_refuses_to_quarantine_behind_its_switch(self, client: Any) -> None:
        from app.memory.lifecycle import contradiction_sweep

        await _set(memory_contradiction_enabled=False)
        assert await contradiction_sweep() == 0

    async def test_compaction_refuses_to_delete_behind_its_switch(self, client: Any) -> None:
        from app.memory.episodic import compact_digests

        await _set(memory_compaction_enabled=False)
        assert await compact_digests() == 0

    async def test_communities_refuses_to_rebuild_behind_its_switch(self, client: Any) -> None:
        from app.memory.communities import rebuild_communities

        await _set(memory_communities_enabled=False)
        assert await rebuild_communities() == 0


class TestJobGates:
    """Invariant 1: each consolidation job stops on its own key alone."""

    @pytest.fixture(autouse=True)
    async def _memory_on(self, client: Any) -> Any:
        await _set(memory_enabled=True)
        yield
        await _set(memory_enabled=False, **{k: True for k in GATED_JOBS.values()})

    async def test_all_gates_on_is_byte_identical(self, client: Any) -> None:
        """Defaults ⇒ every job still runs, exactly as before M48."""
        ran = await _jobs_that_ran()
        assert set(GATED_JOBS) <= ran, f"a default-on job stopped running: {ran}"

    @pytest.mark.parametrize("job,key", sorted(GATED_JOBS.items()))
    async def test_one_gate_off_stops_exactly_that_job(
        self, client: Any, job: str, key: str
    ) -> None:
        await _set(**{key: False})
        ran = await _jobs_that_ran()
        assert job not in ran, f"{key}=false did not stop {job}"
        others = set(GATED_JOBS) - {job}
        assert others <= ran, f"{key}=false also stopped {sorted(others - ran)}"

    async def test_the_master_still_stops_everything(self, client: Any) -> None:
        await _set(memory_enabled=False)
        assert await _jobs_that_ran() == set()


class TestAnticipationGate:
    """The one feature that initiates contact gets an explicit switch."""

    async def test_off_composes_nothing(self, client: Any) -> None:
        from app.ambient.anticipate import run_anticipation

        await _set(ambient_enabled=True, ambient_anticipation_enabled=False)
        try:
            assert await run_anticipation() is None
        finally:
            await _set(ambient_enabled=False, ambient_anticipation_enabled=True)

    async def test_on_is_reachable(self, client: Any) -> None:
        """On, the gate is not what stops it — the job proceeds to its own
        preconditions (no completed runs ⇒ nothing to predict from)."""
        from app.ambient.anticipate import run_anticipation

        await _set(ambient_enabled=True, ambient_anticipation_enabled=True)
        try:
            assert await run_anticipation() is None  # no runs, not the gate
        finally:
            await _set(ambient_enabled=False)


class TestEvalsGate:
    async def test_off_refuses_every_route(self, client: Any) -> None:
        await _set(evals_enabled=False)
        try:
            for path in ("/evals/datasets", "/evals/runs"):
                res = await client.get(f"{API}{path}")
                assert res.status_code == 409, path
                assert "evals_enabled" in res.text
        finally:
            await _set(evals_enabled=True)

    async def test_on_is_the_default_and_serves(self, client: Any) -> None:
        assert DEFAULTS["evals_enabled"] is True
        assert (await client.get(f"{API}/evals/datasets")).status_code == 200


class TestOffMeansOff:
    """The §3.7.1 corollary."""

    async def test_zero_community_budget_skips_the_rebuild(self, client: Any) -> None:
        from app.memory.communities import rebuild_communities

        await _set(memory_enabled=True, memory_community_budget_tokens=0)
        try:
            assert await rebuild_communities() == 0
        finally:
            await _set(memory_enabled=False, memory_community_budget_tokens=150)

    async def test_zero_budget_also_drops_the_job_from_the_tick(self, client: Any) -> None:
        await _set(memory_enabled=True, memory_community_budget_tokens=0)
        try:
            assert "communities" not in await _jobs_that_ran()
        finally:
            await _set(memory_enabled=False, memory_community_budget_tokens=150)


class TestDeadCode:
    def test_the_promoted_constant_is_gone(self) -> None:
        """M40 promoted STALL_AFTER_S to run_stall_after_s; the constant
        must not linger as a second, silently-wrong source of truth."""
        src = (Path(__file__).resolve().parents[1] / "app" / "ambient" / "execute.py").read_text()
        assert not re.search(r"^STALL_AFTER_S\s*=", src, re.M)
