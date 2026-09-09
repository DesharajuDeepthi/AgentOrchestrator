"""Multi-replica ambient coordination (spec §18.9).

The ambient tick elects a leader through a Postgres SESSION advisory lock
held on a dedicated (unpooled) connection: the session IS the lease. A
leader "renews" each tick by proving the lock connection is still alive;
when the process dies or the connection drops, Postgres releases the lock
with the session and another replica's try-acquire succeeds on its next
tick — takeover within one tick, no lease table, no clock comparisons.

Non-leaders keep LISTENing and draining (the FOR UPDATE SKIP LOCKED drain
and the executor are replica-safe by construction) but skip the evaluators.
"""

import contextlib
from typing import Any

import structlog

from app.config import get_config

# dedicated advisory-lock pair for the ambient tick (spec §18.9). The
# consolidation jobs key their locks on job ids — this classid must never
# collide with them, and it is part of the cross-replica contract.
AMBIENT_LEADER_CLASSID = 427017
AMBIENT_LEADER_OBJID = 1

logger = structlog.get_logger()


class LeaderLease:
    """try-acquire / renew / release over one dedicated asyncpg session."""

    def __init__(self) -> None:
        self._conn: Any | None = None
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    async def ensure(self) -> bool:
        """Renew when held, otherwise try to acquire. True == lead this tick."""
        if self._conn is not None and self._held:
            try:
                await self._conn.execute("SELECT 1")  # heartbeat renewal
                return True
            except Exception:
                # the lease session died under us — the lock is already gone
                await self._teardown()
                logger.warning("ambient_leader_lost", tier="ambient", kind="leader")
        try:
            if self._conn is None:
                import asyncpg  # type: ignore[import-untyped]

                dsn = get_config().database_url.replace("postgresql+asyncpg://", "postgresql://")
                self._conn = await asyncpg.connect(dsn)
            got = bool(
                await self._conn.fetchval(
                    "SELECT pg_try_advisory_lock($1, $2)",
                    AMBIENT_LEADER_CLASSID,
                    AMBIENT_LEADER_OBJID,
                )
            )
            if got and not self._held:
                logger.info("ambient_leader_acquired", tier="ambient", kind="leader")
            self._held = got
            return got
        except Exception as exc:  # noqa: BLE001 — a broken DB must not kill the loop
            await self._teardown()
            logger.warning("ambient_leader_error", tier="ambient", kind="leader", error=str(exc))
            return False

    async def release(self) -> None:
        """Unlock and drop the session. Safe on a dead or absent connection."""
        if self._conn is not None and self._held:
            with contextlib.suppress(Exception):
                await self._conn.fetchval(
                    "SELECT pg_advisory_unlock($1, $2)",
                    AMBIENT_LEADER_CLASSID,
                    AMBIENT_LEADER_OBJID,
                )
        await self._teardown()

    async def _teardown(self) -> None:
        conn, self._conn = self._conn, None
        self._held = False
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.close()
