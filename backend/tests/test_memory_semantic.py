"""Semantic write pipeline tests (spec §16.2 — milestone M15).

The fake provider's structured output parses pushed JSON scripts, so every
LLM decision here is scripted and deterministic.
"""

from typing import Any

import pytest
from sqlalchemy import select

from app.db import get_session_factory
from app.llm import fake as fake_llm
from app.memory import remember
from app.memory.extract import extract_from_run
from app.models import Memory, Run
from app.settings_store import update_settings

pytestmark = pytest.mark.anyio


async def _set(**kv: Any) -> None:
    async with get_session_factory()() as session:
        await update_settings(session, kv)


async def _enable() -> None:
    await _set(
        memory_enabled=True,
        embedding_model="fake:scripted",
        memory_extraction_model="fake:scripted",
    )


async def _completed_run(message: str, answer: str) -> Run:
    from app.orchestrator.runner import create_run

    run = await create_run(None, message)
    async with get_session_factory()() as session:
        row = await session.get(Run, run.id)
        assert row is not None
        row.status = "completed"
        row.final_answer = answer
        await session.commit()
        await session.refresh(row)
        return row


def _push_extraction(memories: list[dict[str, Any]]) -> None:
    fake_llm.push_ai(
        "", tool_calls=[{"name": "ExtractionOutput", "args": {"memories": memories}, "id": "x"}]
    )


def _push_verdicts(*pairs: tuple[int, str]) -> None:
    fake_llm.push_ai(
        "",
        tool_calls=[
            {
                "name": "ReconcileOutput",
                "args": {"verdicts": [{"index": i, "verdict": v} for i, v in pairs]},
                "id": "v",
            }
        ],
    )


async def test_extraction_pipeline_gates_and_writes() -> None:
    await _enable()
    run = await _completed_run(
        "my office moved to Lisbon and I prefer short answers", "noted, congrats on the move"
    )
    _push_extraction(
        [
            {
                "text": "the user's office is in Lisbon",
                "kind": "fact",
                "importance": 6,
                "confidence": 0.9,
                "entity_key": "office.location",
            },
            {
                "text": "the user prefers short answers",
                "kind": "preference",
                "importance": 5,
                "confidence": 0.85,
                "entity_key": None,
            },
            {
                "text": "a vague guess that should be gated out",
                "kind": "fact",
                "importance": 2,
                "confidence": 0.2,
                "entity_key": None,
            },
        ]
    )
    written = await extract_from_run(run.id)
    texts = {m.text for m in written}
    assert "the user's office is in Lisbon" in texts
    assert "the user prefers short answers" in texts
    assert len(written) == 2  # the low-confidence candidate was gated
    assert all(m.source == "extracted" and m.run_id == run.id for m in written)
    assert all(m.status == "active" for m in written)


async def test_extracted_instruction_quarantines_never_supersedes() -> None:
    await _enable()
    existing = await remember(
        text="always include a summary table", kind="instruction", source="user_stated"
    )
    run = await _completed_run("from now on always answer in bullet points", "understood")
    _push_extraction(
        [
            {
                "text": "always answer in bullet points",
                "kind": "instruction",
                "importance": 7,
                "confidence": 0.95,
                "entity_key": None,
            }
        ]
    )
    written = await extract_from_run(run.id)
    assert len(written) == 1
    assert written[0].status == "quarantined"
    async with get_session_factory()() as session:
        old = await session.get(Memory, existing.id)
    assert old is not None and old.status == "active"  # untouched — no supersede path


async def test_entity_key_match_supersedes_deterministically() -> None:
    """Same entity_key ⇒ same fact by construction — no LLM call needed."""
    await _enable()
    old = await remember(
        text="the user's office is in Berlin",
        kind="fact",
        source="user_stated",
        entity_key="office.location",
    )
    run = await _completed_run("we moved the office to Lisbon", "noted")
    _push_extraction(
        [
            {
                "text": "the user's office is in Lisbon",
                "kind": "fact",
                "importance": 6,
                "confidence": 0.9,
                "entity_key": "office.location",
            }
        ]
    )
    written = await extract_from_run(run.id)
    assert len(written) == 1
    new = written[0]
    assert new.supersedes == old.id
    async with get_session_factory()() as session:
        old_row = await session.get(Memory, old.id)
    assert old_row is not None
    assert old_row.status == "superseded"
    assert old_row.superseded_by == new.id
    assert old_row.valid_to == new.valid_from  # bi-temporal close


async def test_llm_same_verdict_supersedes_related_adds() -> None:
    await _enable()
    old = await remember(
        text="the team standup happens at nine thirty", kind="fact", source="user_stated"
    )
    run = await _completed_run("standup moved to ten, and fridays are no-meeting days", "got it")
    _push_extraction(
        [
            {
                "text": "the team standup happens at ten",
                "kind": "fact",
                "importance": 5,
                "confidence": 0.9,
                "entity_key": None,
            },
            {
                "text": "fridays are no-meeting days for the team",
                "kind": "fact",
                "importance": 5,
                "confidence": 0.9,
                "entity_key": None,
            },
        ]
    )
    # candidate 1 reconciles against neighbors (the standup memory) → same
    _push_verdicts((1, "same"))
    # candidate 2 reconciles against neighbors → related (coexists)
    _push_verdicts((1, "related"), (2, "related"))
    written = await extract_from_run(run.id)
    assert len(written) == 2
    superseder = next(m for m in written if "at ten" in m.text)
    assert superseder.supersedes == old.id
    async with get_session_factory()() as session:
        old_row = await session.get(Memory, old.id)
        active = list(
            (
                await session.execute(
                    select(Memory).where(Memory.status == "active", Memory.kind == "fact")
                )
            ).scalars()
        )
    assert old_row is not None and old_row.status == "superseded"
    active_texts = {m.text for m in active}
    assert "the team standup happens at ten" in active_texts
    assert "fridays are no-meeting days for the team" in active_texts
    assert "the team standup happens at nine thirty" not in active_texts


async def test_extraction_respects_toggles() -> None:
    run = await _completed_run("remember that my dog is called biscuit", "cute name")
    # memory off entirely
    assert await extract_from_run(run.id) == []
    # memory on but extraction off
    await _set(memory_enabled=True, memory_extraction_enabled=False)
    assert await extract_from_run(run.id) == []
    async with get_session_factory()() as session:
        rows = (await session.execute(select(Memory))).all()
    assert rows == []


async def test_extraction_fail_open_on_bad_llm_output() -> None:
    await _enable()
    run = await _completed_run("some message", "some answer")
    fake_llm.push_ai("this is not json and cannot become an ExtractionOutput")
    written = await extract_from_run(run.id)
    assert written == []  # fail-open: zero candidates, run unaffected


async def test_scheduler_chains_extraction() -> None:
    from app.memory.scheduler import process_run

    await _enable()
    run = await _completed_run("my favorite color is teal", "noted")
    fake_llm.push_ai("digest text for the scheduler chain test")  # digest LLM call
    _push_extraction(
        [
            {
                "text": "the user's favorite color is teal",
                "kind": "preference",
                "importance": 4,
                "confidence": 0.9,
                "entity_key": None,
            }
        ]
    )
    await process_run(run.id)
    async with get_session_factory()() as session:
        mems = list((await session.execute(select(Memory))).scalars())
    assert any("teal" in m.text for m in mems)
