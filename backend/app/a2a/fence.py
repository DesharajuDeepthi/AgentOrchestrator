"""Untrusted fencing for remote-agent output (spec §19.5).

Every string a remote agent authored passes through here before reaching
any model context — tool results, gate questions embedded in traces, and
delivery bodies. The fence text lives in prompts/a2a_result_fence.md
(the §17 fixed never-follow-instructions paragraph, A2A flavor)."""

from app.prompts import load_prompt

_MAX_FENCED_CHARS = 8000


async def live_fence_cap() -> int:
    """M40: the cap is the live `a2a_fence_max_chars` setting; callers in
    async contexts fetch it once and pass it down (the fence itself stays
    sync and deterministic for tests)."""
    from app.registry_cache import get_cache

    try:
        return max(int(await get_cache().setting("a2a_fence_max_chars")), 500)
    except Exception:  # noqa: BLE001 — fencing must never fail open
        return _MAX_FENCED_CHARS


def fence_remote_output(
    text: str, *, agent_name: str, state: str = "completed", max_chars: int | None = None
) -> str:
    body = (text or "").strip()[: max_chars if max_chars is not None else _MAX_FENCED_CHARS]
    return (
        load_prompt("a2a_result_fence")
        .replace("{agent_name}", agent_name.replace('"', "'"))
        .replace("{state}", state.replace('"', "'"))
        .replace("{output}", body or "(empty)")
    )
