"""M47 — the extraction tuner: second consumer under the §17.7 rule,
the tombstone-informed learner the M44 spec text promised. Own gate
(`memory_extraction_learning`, default off, born dark), machine writes
only, riding the existing policy ledger (reject inert, approve applies
through `_apply_special`, review UI free)."""

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select

from app.db import get_session_factory
from app.memory.extract_learn import run_extraction_tuner
from app.memory.store import Candidate, gate_candidates, remember
from app.models import AmbientPolicy, Conversation, Memory, MemoryTombstone, Run
from app.settings_store import update_settings

pytestmark = pytest.mark.anyio

API = "/api/v1"


async def _set(**kv: Any) -> None:
    async with get_session_factory()() as session:
        await update_settings(session, kv)


async def _run_id() -> UUID:
    async with get_session_factory()() as session:
        conv = Conversation(title="m47 probe")
        session.add(conv)
        await session.flush()
        run = Run(
            conversation_id=conv.id,
            chat_message="m47",
            status="completed",
            trigger={"kind": "test"},
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
    return run.id


async def _ledger(
    kind: str,
    *,
    kept: int = 0,
    forgotten: int = 0,
    rejected: int = 0,
    confidence: float = 0.7,
    source: str = "extracted",
) -> None:
    """Fabricate the tuner's inputs directly: active rows, tombstones with
    confidence-at-admission, and review-rejected rows."""
    async with get_session_factory()() as session:
        for i in range(kept):
            session.add(
                Memory(
                    text=f"{kind} kept {confidence} {i}",
                    kind=kind,
                    scope="global",
                    source=source,
                    status="active",
                    confidence=confidence,
                )
            )
        for i in range(rejected):
            session.add(
                Memory(
                    text=f"{kind} rejected {confidence} {i}",
                    kind=kind,
                    scope="global",
                    source=source,
                    status="rejected",
                    confidence=confidence,
                )
            )
        for _ in range(forgotten):
            session.add(
                MemoryTombstone(
                    scope="global",
                    kind=kind,
                    source=source,
                    confidence=confidence,
                    text_hash="0" * 64,
                    token_hashes=[],
                )
            )
        await session.commit()


async def _policies() -> list[AmbientPolicy]:
    async with get_session_factory()() as session:
        return list((await session.execute(select(AmbientPolicy))).scalars())


async def _setting(key: str) -> Any:
    from app.registry_cache import get_cache

    return await get_cache().setting(key)


@pytest.fixture(autouse=True)
async def _clean_gate(client: Any) -> Any:
    yield
    await _set(
        memory_extraction_learning="off",
        memory_quarantine_kinds=[],
        memory_admission_min_confidence=0.5,
    )


class TestEnforcement:
    async def test_floor_setting_drives_the_gate(self, client: Any) -> None:
        """The promoted constant (M40 pattern): default byte-identical,
        raised floor refuses what the constant would have passed."""
        accepted, _ = await gate_candidates(
            [Candidate(text="fine at the default floor", kind="fact", confidence=0.6)]
        )
        assert len(accepted) == 1
        await _set(memory_admission_min_confidence=0.7)
        accepted, dropped = await gate_candidates(
            [Candidate(text="refused at the raised floor", kind="fact", confidence=0.6)]
        )
        assert accepted == [] and "confidence" in dropped[0][1]

    async def test_routed_kind_quarantines_machine_writes_only(self, client: Any) -> None:
        """The hard §17.7 invariant: routing touches machine writes; the
        human's own words always land active."""
        await _set(memory_quarantine_kinds=["entity"])
        run_id = await _run_id()
        machine = await remember(
            text="machine entity write", kind="entity", source="extracted", run_id=run_id
        )
        human = await remember(text="human entity write", kind="entity", source="user_stated")
        assert machine.status == "quarantined"
        assert machine.review_note == "extraction learner: kind routed through review"
        assert human.status == "active"

    async def test_unrouted_kind_is_byte_identical(self, client: Any) -> None:
        run_id = await _run_id()
        row = await remember(
            text="normal fact write", kind="fact", source="extracted", run_id=run_id
        )
        assert row.status == "active" and row.review_note is None


class TestGate:
    async def test_off_is_a_noop(self, client: Any) -> None:
        await _ledger("entity", forgotten=10)
        out = await run_extraction_tuner()
        assert out == {"considered": 0, "kind_routes": 0, "floor_moves": 0}
        assert await _policies() == []


class TestKindRouting:
    async def test_repudiated_kind_routes_in_auto(self, client: Any) -> None:
        await _set(memory_extraction_learning="auto")
        await _ledger("entity", kept=2, forgotten=6)
        out = await run_extraction_tuner()
        assert out["kind_routes"] == 1
        assert await _setting("memory_quarantine_kinds") == ["entity"]
        (row,) = [p for p in await _policies() if "quarantine_kinds" in p.category]
        assert row.source == "learner" and "proposed=+entity" in row.reason

    async def test_rejections_count_as_repudiation(self, client: Any) -> None:
        await _set(memory_extraction_learning="auto")
        await _ledger("entity", kept=2, rejected=6)
        assert (await run_extraction_tuner())["kind_routes"] == 1

    async def test_a_valuable_kind_is_never_routed(self, client: Any) -> None:
        await _set(memory_extraction_learning="auto")
        await _ledger("preference", kept=18, forgotten=2)  # 0.1 < 0.5
        assert (await run_extraction_tuner())["kind_routes"] == 0
        assert await _setting("memory_quarantine_kinds") == []

    async def test_user_stated_repudiation_never_drives_the_tuner(self, client: Any) -> None:
        """Forgetting your own words is consent about YOUR words — not
        evidence against the machine's writes of that kind."""
        await _set(memory_extraction_learning="auto")
        await _ledger("entity", forgotten=10, source="user_stated")
        out = await run_extraction_tuner()
        assert out["considered"] == 0 and out["kind_routes"] == 0

    async def test_propose_stays_inert_until_approved(self, client: Any) -> None:
        await _set(memory_extraction_learning="propose")
        await _ledger("entity", kept=2, forgotten=6)
        assert (await run_extraction_tuner())["kind_routes"] == 1
        assert await _setting("memory_quarantine_kinds") == []  # inert
        (prop,) = [p for p in await _policies() if "quarantine_kinds" in p.category]
        assert prop.source == "learner_proposal"
        ok = await client.post(f"{API}/ambient/policies/{prop.id}/approve")
        assert ok.status_code == 200
        assert await _setting("memory_quarantine_kinds") == ["entity"]

    async def test_rejected_proposal_stays_inert(self, client: Any) -> None:
        await _set(memory_extraction_learning="propose")
        await _ledger("entity", kept=2, forgotten=6)
        await run_extraction_tuner()
        (prop,) = [p for p in await _policies() if "quarantine_kinds" in p.category]
        assert (await client.post(f"{API}/ambient/policies/{prop.id}/reject")).status_code == 200
        assert await _setting("memory_quarantine_kinds") == []

    async def test_already_routed_kind_is_not_re_proposed(self, client: Any) -> None:
        await _set(memory_extraction_learning="auto", memory_quarantine_kinds=["entity"])
        await _ledger("entity", forgotten=8)
        assert (await run_extraction_tuner())["kind_routes"] == 0


class TestFloorMoves:
    async def test_junk_band_raises_the_floor(self, client: Any) -> None:
        """The tombstone-informed rule: the floor rises only when the band
        a bump would refuse is itself mostly repudiated."""
        await _set(memory_extraction_learning="auto")
        await _ledger("fact", kept=1, forgotten=6, confidence=0.52)  # in [0.5,0.55)
        await _ledger("fact", kept=10, confidence=0.8)  # healthy above the band
        out = await run_extraction_tuner()
        assert out["floor_moves"] == 1
        assert float(await _setting("memory_admission_min_confidence")) == 0.55

    async def test_confidence_independent_repudiation_never_ratchets(self, client: Any) -> None:
        """The harness-forced refinement: high forget-rate ABOVE the band
        must not raise the floor — that ratchet starves valuable kinds."""
        await _set(memory_extraction_learning="auto")
        await _ledger("fact", kept=8, forgotten=8, confidence=0.8)  # bad, but not band-local
        assert (await run_extraction_tuner())["floor_moves"] == 0

    async def test_clean_stream_relaxes_toward_the_default(self, client: Any) -> None:
        await _set(memory_extraction_learning="auto", memory_admission_min_confidence=0.6)
        await _ledger("preference", kept=15, confidence=0.8)
        await run_extraction_tuner()
        assert float(await _setting("memory_admission_min_confidence")) == 0.55

    async def test_clamps_hold_at_both_ends(self, client: Any) -> None:
        await _set(memory_extraction_learning="auto", memory_admission_min_confidence=0.5)
        await _ledger("preference", kept=15, confidence=0.8)  # clean, floor already MIN
        await run_extraction_tuner()
        assert float(await _setting("memory_admission_min_confidence")) == 0.5
        await _set(memory_admission_min_confidence=0.9)
        # junk band at MAX — kept mass above the band keeps the kind's
        # overall rate under ROUTE_RATE so only the floor rule is in play
        await _ledger("fact", kept=1, forgotten=6, confidence=0.9)
        await _ledger("fact", kept=10, confidence=0.95)
        out = await run_extraction_tuner()
        assert out["kind_routes"] == 0 and out["floor_moves"] == 0
        assert float(await _setting("memory_admission_min_confidence")) == 0.9

    async def test_propose_ledgers_without_changing_the_setting(self, client: Any) -> None:
        await _set(memory_extraction_learning="propose")
        await _ledger("fact", kept=1, forgotten=6, confidence=0.52)
        out = await run_extraction_tuner()
        assert out["floor_moves"] == 1
        assert float(await _setting("memory_admission_min_confidence")) == 0.5
        (prop,) = [
            p for p in await _policies() if p.category == "setting:memory_admission_min_confidence"
        ]
        assert prop.source == "learner_proposal" and "proposed=0.55" in prop.reason
        ok = await client.post(f"{API}/ambient/policies/{prop.id}/approve")
        assert ok.status_code == 200
        assert float(await _setting("memory_admission_min_confidence")) == 0.55

    async def test_routed_kinds_are_excluded_from_the_floor_signal(self, client: Any) -> None:
        """Once a kind is routed its junk is handled at the review queue —
        it must not ALSO push the global floor."""
        await _set(memory_extraction_learning="auto", memory_quarantine_kinds=["entity"])
        await _ledger("entity", kept=1, forgotten=6, confidence=0.52)
        assert (await run_extraction_tuner())["floor_moves"] == 0
