"""M44 — durable forgetting + feedback-trace completeness (spec §16.1/§16.2).

The finding this milestone fixes: hard delete plus existence-only
reconciliation meant the system could quietly RE-LEARN a fact the user
deleted. Forget now leaves a content-free tombstone the admission gate
checks; Erase remains the explicit no-trace verb; Unforget is the escape
hatch. Plus the two capture riders: §17.7 proposal reject and the
overlap-override event.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.db import get_session_factory
from app.memory.extract import reconcile_and_write
from app.memory.store import (
    Candidate,
    check_suppressed,
    normalized_hash,
    remember,
    tombstone_forget,
)
from app.models import Conversation, Memory, MemoryTombstone, Run
from app.settings_store import update_settings

pytestmark = pytest.mark.anyio

API = "/api/v1"


async def _set(**kv: Any) -> None:
    async with get_session_factory()() as session:
        await update_settings(session, kv)


async def _run_id() -> UUID:
    """A completed run for machine-write provenance."""
    async with get_session_factory()() as session:
        conv = Conversation(title="m44 probe")
        session.add(conv)
        await session.flush()
        run = Run(
            conversation_id=conv.id,
            chat_message="m44",
            status="completed",
            trigger={"kind": "test"},
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
    return run.id


async def _stored(text: str, **kw: Any) -> Memory:
    return await remember(text=text, kind="fact", source="user_stated", **kw)


async def _tombstones() -> list[MemoryTombstone]:
    async with get_session_factory()() as session:
        return list((await session.execute(select(MemoryTombstone))).scalars())


async def _memory_texts() -> list[str]:
    async with get_session_factory()() as session:
        return list((await session.execute(select(Memory.text))).scalars())


@pytest.fixture(autouse=True)
async def _forget_on(client: Any) -> Any:
    await _set(memory_forget_enabled=True, memory_enabled=True)
    yield
    await _set(memory_forget_enabled=False, memory_enabled=False)


class TestVerbs:
    async def test_forget_leaves_a_content_free_tombstone(self, client: Any) -> None:
        row = await _stored("the staging db password rotates monthly")
        resp = await client.delete(f"{API}/memories/{row.id}?mode=forget")
        assert resp.status_code == 204
        assert await _memory_texts() == []  # the memory itself is gone
        (t,) = await _tombstones()
        assert t.kind == "fact" and t.scope == "global" and t.source == "user_stated"
        assert t.text_hash == normalized_hash("the staging db password rotates monthly")
        # the trace is metadata + hash — the text appears NOWHERE on it
        assert not hasattr(t, "text")
        listed = (await client.get(f"{API}/memories/tombstones")).json()["items"]
        assert len(listed) == 1 and "text" not in listed[0]

    async def test_erase_leaves_nothing_even_when_forget_is_on(self, client: Any) -> None:
        row = await _stored("erase me completely")
        resp = await client.delete(f"{API}/memories/{row.id}")  # default mode=erase
        assert resp.status_code == 204
        assert await _tombstones() == []

    async def test_forget_requires_the_master(self, client: Any) -> None:
        await _set(memory_forget_enabled=False)
        row = await _stored("flag is off")
        resp = await client.delete(f"{API}/memories/{row.id}?mode=forget")
        assert resp.status_code == 422
        assert "memory_forget_enabled" in resp.text
        # byte-identity: the plain delete still physically erases
        ok = await client.delete(f"{API}/memories/{row.id}")
        assert ok.status_code == 204 and await _tombstones() == []

    async def test_unknown_mode_is_rejected(self, client: Any) -> None:
        row = await _stored("bad mode")
        assert (await client.delete(f"{API}/memories/{row.id}?mode=obliterate")).status_code == 422


class TestSuppression:
    async def test_the_gate_refuses_to_relearn_a_forgotten_fact(self, client: Any) -> None:
        """The M44 headline: before this, the same candidate would re-admit."""
        row = await _stored("the invoice bucket is s3://acme-invoices")
        await client.delete(f"{API}/memories/{row.id}?mode=forget")
        out = await reconcile_and_write(
            Candidate(text="The invoice bucket is  s3://acme-invoices", kind="fact"),
            await _run_id(),
        )
        assert out is None  # suppressed — normalization makes the hash match
        assert await _memory_texts() == []
        (t,) = await _tombstones()
        assert t.suppressed_count == 1 and t.last_suppressed_at is not None

    async def test_suppression_is_scope_aware(self, client: Any) -> None:
        row = await _stored("deploy fridays are frozen")
        await client.delete(f"{API}/memories/{row.id}?mode=forget")
        # same scope suppresses; a different scope is a different promise
        assert await check_suppressed("deploy fridays are frozen", "global", None) is True
        assert await check_suppressed("deploy fridays are frozen", "project", None) is False

    async def test_unforget_makes_the_fact_learnable_again(self, client: Any) -> None:
        row = await _stored("the oncall rotation is weekly")
        await client.delete(f"{API}/memories/{row.id}?mode=forget")
        (t,) = await _tombstones()
        resp = await client.delete(f"{API}/memories/tombstones/{t.id}")
        assert resp.status_code == 204
        out = await reconcile_and_write(
            Candidate(text="the oncall rotation is weekly", kind="fact"), await _run_id()
        )
        assert out is not None and await _tombstones() == []

    async def test_user_assertion_overrides_the_tombstone(self, client: Any) -> None:
        """The human re-stating the fact beats their earlier forget."""
        row = await _stored("the API gateway lives in eu-west-1")
        await client.delete(f"{API}/memories/{row.id}?mode=forget")
        again = await remember(
            text="the API gateway lives in eu-west-1", kind="fact", source="user_stated"
        )
        assert again.id is not None
        assert await _tombstones() == []  # unforgotten by assertion

    async def test_gate_off_means_no_suppression(self, client: Any) -> None:
        """Byte-identity: with the master off even an existing tombstone
        (from an earlier enabled period) changes nothing."""
        row = await _stored("legacy tombstone survives the toggle")
        await client.delete(f"{API}/memories/{row.id}?mode=forget")
        await _set(memory_forget_enabled=False)
        assert (
            await check_suppressed("legacy tombstone survives the toggle", "global", None) is False
        )

    async def test_semantic_suppression_catches_paraphrases(
        self, client: Any, monkeypatch: Any
    ) -> None:
        """With an embedding model, a paraphrase is refused too; an
        unrelated fact still admits."""
        vecs = {
            "the payroll run happens on the 25th": [1.0, 0.0, 0.0],
            "payroll executes every month on the 25th": [0.98, 0.199, 0.0],
            "the office plants need watering": [0.0, 0.0, 1.0],
        }

        async def fake_embeddings(_model: str, texts: list[str]) -> list[list[float]]:
            return [vecs[t] for t in texts] if texts[0] in vecs else [[0.5, 0.5, 0.5]]

        monkeypatch.setattr("app.llm.get_embeddings", fake_embeddings)
        await _set(embedding_model="fake:scripted")
        try:
            row = await _stored("the payroll run happens on the 25th")
            await client.delete(f"{API}/memories/{row.id}?mode=forget")
            (t,) = await _tombstones()
            assert t.embedding is not None and t.model_key  # the copy landed
            suppressed = await check_suppressed(
                "payroll executes every month on the 25th", "global", None
            )
            assert suppressed is True  # cosine ≈ 0.98 ≥ 0.85
            unrelated = await check_suppressed("the office plants need watering", "global", None)
            assert unrelated is False
        finally:
            await _set(embedding_model=None)

    async def test_purge_erases_tombstones_too(self, client: Any) -> None:
        row = await _stored("purge takes everything")
        await client.delete(f"{API}/memories/{row.id}?mode=forget")
        assert len(await _tombstones()) == 1
        assert (await client.post(f"{API}/memories/purge")).status_code == 204
        assert await _tombstones() == []


@pytest.fixture
def auth_on(monkeypatch: pytest.MonkeyPatch) -> Any:
    from app.config import get_config

    monkeypatch.setenv("AUTH_ENABLED", "1")
    get_config.cache_clear()
    yield
    get_config.cache_clear()


class TestTenancy:
    async def test_tombstones_are_per_user(self, client: Any, auth_on: Any) -> None:
        from app.auth import bootstrap_admin

        admin_user, password = await bootstrap_admin()
        login = await client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": password}
        )
        ah = {"Authorization": f"Bearer {login.json()['token']}"}
        created = await client.post(
            "/api/v1/auth/users",
            json={"username": "sasha", "password": "member-pass-3", "role": "member"},
            headers=ah,
        )
        assert created.status_code == 201, created.text
        member_login = await client.post(
            "/api/v1/auth/login", json={"username": "sasha", "password": "member-pass-3"}
        )
        mh = {"Authorization": f"Bearer {member_login.json()['token']}"}

        row = await _stored("admin's secret preference", user_id=admin_user.id)
        assert await tombstone_forget(row.id) is True
        (t,) = await _tombstones()
        # the other user neither lists it …
        assert (await client.get(f"{API}/memories/tombstones", headers=mh)).json()["items"] == []
        # … nor unforgets it …
        assert (
            await client.delete(f"{API}/memories/tombstones/{t.id}", headers=mh)
        ).status_code == 404
        # … and their own identical fact is NOT suppressed (different owner)
        member_id = uuid4()
        assert await check_suppressed("admin's secret preference", "global", member_id) is False
        # the owner sees and can unforget
        assert (
            len((await client.get(f"{API}/memories/tombstones", headers=ah)).json()["items"]) == 1
        )
        assert (
            await client.delete(f"{API}/memories/tombstones/{t.id}", headers=ah)
        ).status_code == 204


class TestCaptureRiders:
    async def test_proposal_reject_is_captured_and_stays_inert(self, client: Any) -> None:
        from app.ambient.deliver import current_tier_override
        from app.models import AmbientPolicy

        async with get_session_factory()() as session:
            prop = AmbientPolicy(
                category="chatter",
                tier_override=3,
                reason="learner: chronically dismissed",
                source="learner_proposal",
            )
            session.add(prop)
            await session.commit()
            await session.refresh(prop)
        resp = await client.post(f"{API}/ambient/policies/{prop.id}/reject")
        assert resp.status_code == 200 and resp.json()["status"] == "rejected"
        async with get_session_factory()() as session:
            fresh = await session.get(AmbientPolicy, prop.id)
            assert fresh is not None
            assert fresh.source == "learner_rejected"
            assert fresh.reason.startswith("rejected ")
        # rejected ⇒ still inert: it must never win the effective policy
        assert await current_tier_override("chatter") is None
        # and it can no longer be approved
        assert (await client.post(f"{API}/ambient/policies/{prop.id}/approve")).status_code == 404
        # replaying the reject is a 404 too — first decision wins
        assert (await client.post(f"{API}/ambient/policies/{prop.id}/reject")).status_code == 404

    async def test_overlap_override_capture(self, client: Any) -> None:
        ok = await client.post(
            f"{API}/skills/overlap-ack", json={"draft_type": "skill", "overlap_percent": 84}
        )
        assert ok.status_code == 204
        bad = await client.post(
            f"{API}/skills/overlap-ack", json={"draft_type": "tool", "overlap_percent": 84}
        )
        assert bad.status_code == 422


class TestHybridGate:
    """M44 stage-32 calibration: live paraphrase pairs measured cosine
    0.876 and 0.847 — a lone threshold cannot separate 'same fact
    restated' from 'same topic, different fact'. The gate therefore
    suppresses at the threshold alone, or in the gray band (≥ 0.70) only
    WITH a shared distinctive-token anchor."""

    async def _forgotten(
        self, client: Any, text: str, vecs: dict[str, list[float]], monkeypatch: Any
    ) -> None:
        async def fake_embeddings(_m: str, texts: list[str]) -> list[list[float]]:
            return [vecs.get(t, [0.5, 0.5, 0.5]) for t in texts]

        monkeypatch.setattr("app.llm.get_embeddings", fake_embeddings)
        await _set(embedding_model="fake:scripted")
        row = await _stored(text)
        assert (await client.delete(f"{API}/memories/{row.id}?mode=forget")).status_code == 204

    async def test_gray_band_with_anchor_suppresses(self, client: Any, monkeypatch: Any) -> None:
        # cosine ≈ 0.80 (< 0.85 threshold) but the payload token is shared
        vecs = {
            "the billing api key lives in vault path secret/acme-billing-key": [1.0, 0.0, 0.0],
            "acme-billing-key is kept in vault": [0.8, 0.6, 0.0],
        }
        try:
            await self._forgotten(
                client,
                "the billing api key lives in vault path secret/acme-billing-key",
                vecs,
                monkeypatch,
            )
            assert (
                await check_suppressed("acme-billing-key is kept in vault", "global", None) is True
            )
        finally:
            await _set(embedding_model=None)

    async def test_gray_band_without_anchor_admits(self, client: Any, monkeypatch: Any) -> None:
        # same cosine, but a DIFFERENT payload — a value update, not a restatement
        vecs = {
            "the billing api key lives in vault path secret/acme-billing-key": [1.0, 0.0, 0.0],
            "the billing key moved to secret/new-billing-key2": [0.8, 0.6, 0.0],
        }
        try:
            await self._forgotten(
                client,
                "the billing api key lives in vault path secret/acme-billing-key",
                vecs,
                monkeypatch,
            )
            assert (
                await check_suppressed(
                    "the billing key moved to secret/new-billing-key2", "global", None
                )
                is False
            )
        finally:
            await _set(embedding_model=None)

    async def test_below_the_floor_even_an_anchor_admits(
        self, client: Any, monkeypatch: Any
    ) -> None:
        vecs = {
            "the billing api key lives in vault path secret/acme-billing-key": [1.0, 0.0, 0.0],
            "unrelated note that merely mentions acme-billing-key once": [0.5, 0.86, 0.0],
        }
        try:
            await self._forgotten(
                client,
                "the billing api key lives in vault path secret/acme-billing-key",
                vecs,
                monkeypatch,
            )
            assert (
                await check_suppressed(
                    "unrelated note that merely mentions acme-billing-key once", "global", None
                )
                is False
            )
        finally:
            await _set(embedding_model=None)
