"""FLE-3 (feedback_loop_exp) — the salience tuner: first consumer of the
M43b judge_reward ledger, under the §17.7 rule (own gate, born dark,
clamped, every change ledgered and reversible through the EXISTING
policy machinery — revert un-mutes, reject stays inert)."""

from typing import Any

import pytest
from sqlalchemy import select

from app.ambient.salience_learn import run_salience_tuner, salience_muted
from app.db import get_session_factory
from app.models import AmbientPolicy, Delivery
from app.settings_store import update_settings

pytestmark = pytest.mark.anyio

API = "/api/v1"


async def _set(**kv: Any) -> None:
    async with get_session_factory()() as session:
        await update_settings(session, kv)


async def _decided(category: str, decisions: list[str]) -> None:
    """Fabricate decided salience ledgers — the tuner reads only these."""
    async with get_session_factory()() as session:
        for i, decision in enumerate(decisions):
            session.add(
                Delivery(
                    category=category,
                    tier=0,
                    urgency=4,
                    title=f"{category} {i}",
                    salience={
                        "verdict": "escalate",
                        "confidence": 0.9,
                        "mode": "propose",
                        "decision": decision,
                        "applied": decision == "applied",
                        "judge_reward": 1.0 if decision == "applied" else -1.0,
                    },
                )
            )
        await session.commit()


async def _policies() -> list[AmbientPolicy]:
    async with get_session_factory()() as session:
        return list((await session.execute(select(AmbientPolicy))).scalars())


@pytest.fixture(autouse=True)
async def _ambient_on(client: Any) -> Any:
    await _set(ambient_enabled=True)
    yield
    await _set(ambient_enabled=False, ambient_salience_learning="off")


class TestGate:
    async def test_off_is_a_noop(self, client: Any) -> None:
        await _decided("noise", ["declined"] * 10)
        out = await run_salience_tuner()
        assert out == {"considered": 0, "mutes": 0, "floor_moves": 0}
        assert await _policies() == []  # byte-identity: no ledger rows


class TestMutes:
    async def test_repudiated_category_is_muted_in_auto(self, client: Any) -> None:
        await _set(ambient_salience_learning="auto")
        await _decided("noise", ["declined"] * 6)
        out = await run_salience_tuner()
        assert out["mutes"] == 1
        assert await salience_muted("noise") is True
        (row,) = [p for p in await _policies() if p.category == "salience:noise"]
        assert row.source == "learner" and row.tier_override == 1

    async def test_a_five_percent_category_still_mutes(self, client: Any) -> None:
        """The harness-forced rule change: ≤0.10 endorsement, not zero."""
        await _set(ambient_salience_learning="auto")
        await _decided("noise", ["applied"] + ["declined"] * 19)  # 0.05
        assert (await run_salience_tuner())["mutes"] == 1

    async def test_a_valuable_category_is_never_muted(self, client: Any) -> None:
        await _set(ambient_salience_learning="auto")
        await _decided("prod", ["applied"] * 9 + ["declined"])  # 0.9
        assert (await run_salience_tuner())["mutes"] == 0

    async def test_undone_counts_as_repudiation(self, client: Any) -> None:
        await _set(ambient_salience_learning="auto")
        await _decided("noise", ["undone"] * 6)
        assert (await run_salience_tuner())["mutes"] == 1

    async def test_propose_queues_and_stays_inert_until_approved(self, client: Any) -> None:
        await _set(ambient_salience_learning="propose")
        await _decided("noise", ["declined"] * 6)
        assert (await run_salience_tuner())["mutes"] == 1
        (prop,) = [p for p in await _policies() if p.category == "salience:noise"]
        assert prop.source == "learner_proposal"
        assert await salience_muted("noise") is False  # inert until approved
        ok = await client.post(f"{API}/ambient/policies/{prop.id}/approve")
        assert ok.status_code == 200
        assert await salience_muted("noise") is True

    async def test_rejected_proposal_stays_inert(self, client: Any) -> None:
        await _set(ambient_salience_learning="propose")
        await _decided("noise", ["declined"] * 6)
        await run_salience_tuner()
        (prop,) = [p for p in await _policies() if p.category == "salience:noise"]
        assert (await client.post(f"{API}/ambient/policies/{prop.id}/reject")).status_code == 200
        assert await salience_muted("noise") is False

    async def test_revert_unmutes(self, client: Any) -> None:
        await _set(ambient_salience_learning="auto")
        await _decided("noise", ["declined"] * 6)
        await run_salience_tuner()
        assert await salience_muted("noise") is True
        ok = await client.post(
            f"{API}/ambient/policies/revert", json={"category": "salience:noise"}
        )
        assert ok.status_code == 200
        assert await salience_muted("noise") is False

    async def test_muted_category_skips_the_judge(self, client: Any, monkeypatch: Any) -> None:
        from datetime import UTC, datetime

        from app.ambient.deliver import add_delivery, flush_deliveries
        from app.ambient.salience import run_salience_pass

        await _set(
            ambient_salience_learning="auto",
            ambient_salience_mode="propose",
            ambient_digest_times=["23:58"],
            ambient_quiet_hours=["03:00", "03:01"],
        )
        await _decided("noise", ["declined"] * 6)
        await run_salience_tuner()

        async def forbidden(_row: Any) -> None:
            raise AssertionError("a muted category must never reach the judge")

        monkeypatch.setattr("app.ambient.salience.judge", forbidden)
        await add_delivery(category="noise", tier=0, urgency=5, title="still noisy")
        await flush_deliveries(datetime.now(UTC).replace(hour=12))
        out = await run_salience_pass()
        assert out["judged"] == 0 and out["skipped"] >= 1
        await _set(ambient_salience_mode="off")


class TestFloorMoves:
    async def test_low_endorsement_raises_the_floor(self, client: Any) -> None:
        await _set(ambient_salience_learning="auto", ambient_salience_min_urgency=3)
        # two categories, both mediocre — neither mutes, mix is 0.2
        await _decided("a", ["applied"] * 2 + ["declined"] * 8)
        await _decided("b", ["applied"] * 2 + ["declined"] * 8)
        out = await run_salience_tuner()
        assert out["floor_moves"] == 1
        from app.registry_cache import get_cache

        assert int(await get_cache().setting("ambient_salience_min_urgency")) == 4

    async def test_high_endorsement_lowers_the_floor(self, client: Any) -> None:
        await _set(ambient_salience_learning="auto", ambient_salience_min_urgency=3)
        await _decided("prod", ["applied"] * 9 + ["declined"])
        await run_salience_tuner()
        from app.registry_cache import get_cache

        assert int(await get_cache().setting("ambient_salience_min_urgency")) == 2

    async def test_clamps_hold_at_both_ends(self, client: Any) -> None:
        from app.registry_cache import get_cache

        await _set(ambient_salience_learning="auto", ambient_salience_min_urgency=5)
        await _decided("junk", ["applied"] * 2 + ["declined"] * 18)  # 0.1 → mutes too
        await _decided("junk2", ["applied"] * 2 + ["declined"] * 8)  # 0.2, no mute
        await run_salience_tuner()
        assert int(await get_cache().setting("ambient_salience_min_urgency")) == 5
        await _set(ambient_salience_min_urgency=2)
        async with get_session_factory()() as session:  # clear ledgers
            for row in (await session.execute(select(Delivery))).scalars():
                await session.delete(row)
            await session.commit()
        await _decided("gold", ["applied"] * 12)
        await run_salience_tuner()
        assert int(await get_cache().setting("ambient_salience_min_urgency")) == 2

    async def test_propose_ledgers_without_changing_the_setting(self, client: Any) -> None:
        from app.registry_cache import get_cache

        await _set(ambient_salience_learning="propose", ambient_salience_min_urgency=3)
        await _decided("prod", ["applied"] * 12)
        out = await run_salience_tuner()
        assert out["floor_moves"] == 1
        assert int(await get_cache().setting("ambient_salience_min_urgency")) == 3
        (prop,) = [
            p for p in await _policies() if p.category == "setting:ambient_salience_min_urgency"
        ]
        assert prop.source == "learner_proposal" and "proposed=2" in prop.reason
        # approval applies it through the same _apply_special path
        ok = await client.post(f"{API}/ambient/policies/{prop.id}/approve")
        assert ok.status_code == 200
        assert int(await get_cache().setting("ambient_salience_min_urgency")) == 2
