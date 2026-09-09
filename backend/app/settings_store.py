"""app_settings store (spec §3.7) — defaults, live reads, validated writes.

Values are read live at runtime; a PATCH applies to the next run with no
restart. Provider API keys are env-only and deliberately absent here.
"""

from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import ModelParams, validate_model_selection
from app.models import AppSetting

DEFAULTS: dict[str, Any] = {
    "orchestrator_mode": "graph",
    "orchestrator_full_fallback_enabled": True,
    "default_model": "anthropic:claude-sonnet-4-6",
    "default_model_params": None,
    "planner_model": None,  # null → falls back to default_model
    "planner_model_params": None,
    "aggregator_model": None,  # null → falls back to default_model
    "aggregator_model_params": None,
    "max_parallel_dispatch": 4,
    "max_plan_steps": 6,
    "max_tool_iterations": 8,
    "dynamic_worker_fallback_enabled": True,
    "direct_exposure_cap_warning": 10,
    # the formatter (spec §7.1): presentation role, separate from the aggregator
    "formatter_enabled": True,
    "formatter_presentation": "a2ui_first",  # 'a2ui_first' | 'raw_first'
    "formatter_model": None,  # null → falls back to default_model (single hop)
    "formatter_model_params": None,
    "formatter_coverage_flag_threshold": 90,  # visual flag only, never a gate
    "answer_ui_charts_enabled": True,
    "mcp_health_interval_s": 30,
    "log_level": "INFO",
    "langsmith_enabled": False,
    "langsmith_endpoint": "",
    "langsmith_project": "concierge-agent",
    "otlp_endpoint": "",
    # registry cache (spec §7.3)
    "registry_cache_mode": "bypass",
    # progressive-disclosure retrieval (spec §7.4) — dark by default
    "retrieval_enabled": False,
    "retrieval_threshold": 30,
    "retrieval_top_k": 10,
    "embedding_model": None,  # nullable 'provider:model'; null → lexical-only
    # memory layers (spec §16.7) — dark by default: off is byte-identical
    "memory_enabled": False,
    "memory_extraction_enabled": True,
    "memory_reflection_enabled": False,
    "procedural_learning_enabled": False,
    "memory_injection_budget_tokens": 1200,
    "memory_pinned_budget_tokens": 400,
    "memory_recall_top_k": 6,
    "memory_score_floor": 0.35,
    "memory_extraction_model": None,  # null → default_model at effort low
    "memory_extraction_model_params": None,
    "memory_half_life_days": 30.0,
    "memory_idle_minutes": 10,
    "memory_digest_compact_days": 14,
    # M44 §16.1 durable forgetting — off is byte-identical (deletes stay
    # physical); similarity is the semantic suppression threshold
    "memory_forget_enabled": False,
    "memory_forget_similarity": 0.85,
    # M47 §16.2/§17.7 extraction admission: the gate floor promoted to a
    # live setting (default = the constant it replaces, byte-identical),
    # the machine-write kind router, and the extraction tuner's own gate
    "memory_admission_min_confidence": 0.5,
    "memory_quarantine_kinds": [],
    "memory_extraction_learning": "off",
    # §18.6 community breadth: its own budget line; 0 disables the section
    # AND (M48 §3.7.1 corollary) short-circuits the rebuild job
    "memory_community_budget_tokens": 150,
    # M48 §3.7.1: every autonomous job carries its own gate. Defaults equal
    # the behavior they replace, so the promotion is byte-identical; the
    # four differ in consequence (expire / quarantine / spend / DELETE),
    # which is exactly why one family switch would not have been enough.
    "memory_decay_enabled": True,
    "memory_contradiction_enabled": True,
    "memory_communities_enabled": True,
    "memory_compaction_enabled": True,
    # §17 ambient keys — dark by default
    "ambient_enabled": False,
    "ambient_max_routines": 10,
    "ambient_runs_per_day": 50,
    "ambient_routine_events_per_hour": 20,
    "ambient_idle_minutes": 10,
    "ambient_hitl_timeout_h": 24,
    "ambient_digest_times": ["09:00", "17:00"],
    "ambient_notification_budget_per_day": 3,
    "ambient_quiet_hours": ["22:00", "07:00"],
    "ambient_interrupt_threshold": 4,
    "ambient_wakeups_per_routine_per_day": 100,
    "ambient_escalation_budget_per_day": 10,
    "ambient_learning_mode": "off",
    # M43c: the §17.3 rule-based auto-downgrade is a feedback CONSUMER and
    # gets its own gate (capture stays always-on); true = pre-M43c behavior
    "ambient_precision_rule_enabled": True,
    # §18.4 per-tier channel routing, e.g. {"digest": ["in_app", "email"]};
    # empty ⇒ in-app only, byte-identical to M23–M25
    "ambient_channels": {},
    # M41 pursuit: 'always' is the pre-M41 presence-blind behavior, so the
    # default leaves external dispatch byte-identical
    "ambient_pursuit": "always",
    # M42 salience: off is byte-identical — the pass does not run at all
    "ambient_salience_mode": "off",
    "ambient_salience_min_urgency": 3,
    # FLE-3 (feedback_loop_exp): the salience tuner's own §17.7 gate
    "ambient_salience_learning": "off",
    "ambient_salience_model": None,
    "ambient_salience_model_params": None,
    # M48 §3.7.1: anticipation is the only feature that INITIATES contact
    # unprompted, so silence must be statable, not only learnable via the
    # hit-rate floor. Default true = byte-identical to pre-M48.
    "ambient_anticipation_enabled": True,
    # M48: the §15 eval surface is passive, so this removes surface area
    # rather than changing behavior; off ⇒ every /evals route 409s
    "evals_enabled": True,
    # §19 a2a keys — dark by default
    "a2a_enabled": False,
    "a2a_card_refresh_interval_s": 300,
    "a2a_task_timeout_s": 120,
    "a2a_poll_interval_s": 60,
    "a2a_max_parked": 20,
    # M40 config-hardening keys — every default equals the constant it
    # replaced, so untouched settings are byte-identical to pre-M40
    "ambient_tick_interval_s": 60,
    "rate_limit_burst": 120,
    "rate_limit_per_s": 10,
    "overlap_threshold_percent": 70,
    "run_stall_after_s": 300,
    "agentic_recursion_limit": 100,
    "a2a_http_timeout_s": 15,
    "a2a_fence_max_chars": 8000,
}

_MODEL_KEYS = {
    "default_model",
    "planner_model",
    "aggregator_model",
    "formatter_model",
    "memory_extraction_model",
    "ambient_salience_model",
}
_PARAMS_KEYS = {
    "default_model_params",
    "planner_model_params",
    "aggregator_model_params",
    "formatter_model_params",
    "memory_extraction_model_params",
    "ambient_salience_model_params",
}
_INT_KEYS = {
    "max_parallel_dispatch",
    "max_plan_steps",
    "max_tool_iterations",
    "direct_exposure_cap_warning",
    "mcp_health_interval_s",
    "retrieval_threshold",
    "retrieval_top_k",
    "memory_injection_budget_tokens",
    "memory_pinned_budget_tokens",
    "memory_recall_top_k",
    "memory_idle_minutes",
    "memory_digest_compact_days",
    "ambient_max_routines",
    "ambient_runs_per_day",
    "ambient_routine_events_per_hour",
    "ambient_idle_minutes",
    "ambient_hitl_timeout_h",
    "ambient_notification_budget_per_day",
    "ambient_interrupt_threshold",
    "ambient_wakeups_per_routine_per_day",
    "ambient_escalation_budget_per_day",
    "a2a_card_refresh_interval_s",
    "a2a_task_timeout_s",
    "a2a_poll_interval_s",
    "rate_limit_burst",
    "rate_limit_per_s",
    "a2a_http_timeout_s",
}
_BOOL_KEYS = {
    "orchestrator_full_fallback_enabled",
    "dynamic_worker_fallback_enabled",
    "langsmith_enabled",
    "formatter_enabled",
    "answer_ui_charts_enabled",
    "retrieval_enabled",
    "memory_enabled",
    "memory_extraction_enabled",
    "memory_reflection_enabled",
    "procedural_learning_enabled",
    "ambient_enabled",
    "ambient_precision_rule_enabled",
    "memory_forget_enabled",
    "a2a_enabled",
    # M48 §3.7.1 job gates
    "memory_decay_enabled",
    "memory_contradiction_enabled",
    "memory_communities_enabled",
    "memory_compaction_enabled",
    "ambient_anticipation_enabled",
    "evals_enabled",
}
_PRESENTATIONS = {"a2ui_first", "raw_first"}
_CACHE_MODES = {"bypass", "memory", "redis"}
_STR_KEYS = {"langsmith_endpoint", "langsmith_project", "otlp_endpoint"}
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}


class SettingsValidationError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


async def get_settings(session: AsyncSession) -> dict[str, Any]:
    """Merged view: defaults overlaid with stored rows (live from DB)."""
    merged = dict(DEFAULTS)
    for row in (await session.execute(select(AppSetting))).scalars():
        if row.key in merged:
            merged[row.key] = row.value.get("value")
    return merged


async def get_setting(session: AsyncSession, key: str) -> Any:
    row = await session.get(AppSetting, key)
    if row is not None:
        return row.value.get("value")
    return DEFAULTS[key]


def _validate_pair(merged: dict[str, Any], model_key: str, errors: list[str]) -> None:
    """Validate a model ref together with its params (spec §2.1: 422 at save)."""
    params_key = f"{model_key}_params"
    ref = merged.get(model_key)
    raw_params = merged.get(params_key)
    params: ModelParams | None = None
    if raw_params is not None:
        try:
            params = ModelParams.model_validate(raw_params)
        except ValidationError as exc:
            errors.append(f"{params_key}: {exc.errors()[0].get('msg', 'invalid')}")
            return
    if ref is None:
        if params is not None and model_key != "default_model":
            # params for a null model would apply to the default model's ref;
            # require the model to be set explicitly alongside its params
            errors.append(f"{params_key} requires {model_key} to be set")
        elif params is not None:
            errors.append(f"{params_key} requires {model_key} to be set")
        return
    if not isinstance(ref, str):
        errors.append(f"{model_key} must be a 'provider:model' string")
        return
    errors.extend(f"{model_key}: {e}" for e in validate_model_selection(ref, params))


def validate_updates(current: dict[str, Any], updates: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key, value in updates.items():
        if key not in DEFAULTS:
            errors.append(f"unknown setting {key!r}")
            continue
        if key == "orchestrator_mode" and value not in {"graph", "agentic"}:
            errors.append("orchestrator_mode must be 'graph' or 'agentic'")
        elif key == "registry_cache_mode":
            if value not in _CACHE_MODES:
                errors.append(f"registry_cache_mode must be one of {sorted(_CACHE_MODES)}")
            elif value == "redis":
                from app.config import get_config

                if not get_config().redis_url:
                    errors.append("registry_cache_mode 'redis' requires REDIS_URL to be set")
        elif key == "embedding_model":
            if value is not None:
                if not isinstance(value, str):
                    errors.append("embedding_model must be a 'provider:model' string or null")
                else:
                    from app.llm import validate_embedding_selection

                    errors.extend(
                        f"embedding_model: {e}" for e in validate_embedding_selection(value)
                    )
        elif key == "log_level" and value not in _LOG_LEVELS:
            errors.append(f"log_level must be one of {sorted(_LOG_LEVELS)}")
        elif key == "formatter_presentation" and value not in _PRESENTATIONS:
            errors.append(f"formatter_presentation must be one of {sorted(_PRESENTATIONS)}")
        elif key == "formatter_coverage_flag_threshold" and (
            not isinstance(value, int) or not 1 <= value <= 100
        ):
            errors.append("formatter_coverage_flag_threshold must be an integer 1–100")
        elif key == "a2a_max_parked" and (not isinstance(value, int) or value < 0):
            errors.append("a2a_max_parked must be a non-negative integer (0 disables parking)")
        elif key == "ambient_tick_interval_s" and (not isinstance(value, int) or value < 15):
            errors.append("ambient_tick_interval_s must be an integer >= 15")
        elif key == "overlap_threshold_percent" and (
            not isinstance(value, int) or not 0 <= value <= 100
        ):
            errors.append("overlap_threshold_percent must be an integer 0-100")
        elif key == "run_stall_after_s" and (not isinstance(value, int) or value < 60):
            errors.append("run_stall_after_s must be an integer >= 60")
        elif key == "agentic_recursion_limit" and (
            not isinstance(value, int) or not 10 <= value <= 500
        ):
            errors.append("agentic_recursion_limit must be an integer 10-500")
        elif key == "a2a_fence_max_chars" and (not isinstance(value, int) or value < 500):
            errors.append("a2a_fence_max_chars must be an integer >= 500")
        elif key in _INT_KEYS and (not isinstance(value, int) or value < 1):
            errors.append(f"{key} must be a positive integer")
        elif key == "memory_forget_similarity" and (
            not isinstance(value, int | float) or not 0.5 <= float(value) <= 1.0
        ):
            errors.append("memory_forget_similarity must be a number between 0.5 and 1.0")
        elif key == "memory_score_floor" and (
            not isinstance(value, int | float) or not 0.0 <= float(value) <= 1.0
        ):
            errors.append("memory_score_floor must be a number between 0 and 1")
        elif key == "ambient_salience_mode" and (
            not isinstance(value, str) or value not in {"off", "propose", "auto"}
        ):
            errors.append("ambient_salience_mode must be one of: off, propose, auto")
        elif key == "ambient_salience_min_urgency" and (
            not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 5
        ):
            errors.append("ambient_salience_min_urgency must be an integer 1-5")
        elif key == "ambient_pursuit" and (
            not isinstance(value, str) or value not in {"off", "away", "always"}
        ):
            errors.append("ambient_pursuit must be one of: off, away, always")
        elif key == "ambient_learning_mode" and value not in {"off", "auto", "propose"}:
            errors.append("ambient_learning_mode must be one of: off, auto, propose")
        elif key == "ambient_salience_learning" and value not in {"off", "auto", "propose"}:
            errors.append("ambient_salience_learning must be one of: off, auto, propose")
        elif key == "memory_extraction_learning" and value not in {"off", "auto", "propose"}:
            errors.append("memory_extraction_learning must be one of: off, auto, propose")
        elif key == "memory_admission_min_confidence" and (
            not isinstance(value, int | float)
            or isinstance(value, bool)
            or not 0.0 <= float(value) <= 0.9
        ):
            # 0.9 cap: a floor above it would refuse even the extractor's
            # most certain output — the learner's clamp, enforced at rest
            errors.append("memory_admission_min_confidence must be a number between 0.0 and 0.9")
        elif key == "memory_quarantine_kinds":
            from app.memory.store import KINDS

            if not isinstance(value, list) or not all(
                isinstance(k, str) and k in KINDS for k in value
            ):
                errors.append(f"memory_quarantine_kinds must be a list drawn from {sorted(KINDS)}")
        elif key == "ambient_channels":
            from app.ambient.channels import DELIVERY_MODES, registered_channels

            if not isinstance(value, dict):
                errors.append("ambient_channels must be an object of mode → channel list")
            else:
                known = registered_channels()
                for mode, chans in value.items():
                    if mode not in DELIVERY_MODES:
                        errors.append(
                            f"ambient_channels: unknown mode {mode!r} "
                            f"(expected one of {sorted(DELIVERY_MODES)})"
                        )
                    elif not isinstance(chans, list) or not all(isinstance(c, str) for c in chans):
                        errors.append(f"ambient_channels[{mode!r}] must be a list of strings")
                    else:
                        unknown = set(chans) - known
                        if unknown:
                            errors.append(
                                f"ambient_channels[{mode!r}]: unknown channel(s) "
                                f"{sorted(unknown)} — registered: {sorted(known)}"
                            )
        elif key in {"ambient_digest_times", "ambient_quiet_hours"}:
            import re as _re

            ok = isinstance(value, list) and all(
                isinstance(v, str) and _re.fullmatch(r"[0-2]\d:[0-5]\d", v) for v in value
            )
            if not ok:
                errors.append(f"{key} must be a list of HH:MM strings")
        elif key == "memory_half_life_days" and (
            not isinstance(value, int | float) or float(value) <= 0
        ):
            errors.append("memory_half_life_days must be a positive number")
        elif key == "memory_community_budget_tokens" and (not isinstance(value, int) or value < 0):
            errors.append("memory_community_budget_tokens must be an integer ≥ 0 (0 disables)")
        elif key in _BOOL_KEYS and not isinstance(value, bool):
            errors.append(f"{key} must be a boolean")
        elif key in _STR_KEYS and not isinstance(value, str):
            errors.append(f"{key} must be a string")
        elif key in _MODEL_KEYS and value is not None and not isinstance(value, str):
            errors.append(f"{key} must be a 'provider:model' string or null")
        elif key in _PARAMS_KEYS and value is not None and not isinstance(value, dict):
            errors.append(f"{key} must be an object or null")
    if errors:
        return errors

    merged = dict(current)
    merged.update(updates)
    touched_pairs = {
        k.removesuffix("_params") if k in _PARAMS_KEYS else k
        for k in updates
        if k in _MODEL_KEYS or k in _PARAMS_KEYS
    }
    for model_key in touched_pairs:
        # only validate refs being set (or whose params changed); null planner/
        # aggregator fall back to default_model
        if merged.get(model_key) is None and merged.get(f"{model_key}_params") is None:
            continue
        _validate_pair(merged, model_key, errors)
    return errors


async def update_settings(session: AsyncSession, updates: dict[str, Any]) -> dict[str, Any]:
    """Apply validated partial updates; returns the merged settings."""
    current = await get_settings(session)
    errors = validate_updates(current, updates)
    if errors:
        raise SettingsValidationError(errors)
    if updates.get("registry_cache_mode") == "redis":
        await _ping_redis()
    for key, value in updates.items():
        row = await session.get(AppSetting, key)
        if row is None:
            session.add(AppSetting(key=key, value={"value": value}))
        else:
            row.value = {"value": value}
    await session.commit()
    # spec §7.3: settings are a cached registry; every write invalidates
    from app.registry_cache import get_cache

    await get_cache().invalidate("settings")
    # spec §5b/§10: log_level and otlp_endpoint apply live, no restart
    if "log_level" in updates:
        from app.obs import configure_logging

        configure_logging(str(updates["log_level"]))
    if "otlp_endpoint" in updates:
        from app.obs import apply_otlp_endpoint

        apply_otlp_endpoint(str(updates["otlp_endpoint"]))
    return await get_settings(session)


async def _ping_redis() -> None:
    """Selecting the redis cache mode pings Redis and rejects on failure."""
    from app.config import get_config

    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(str(get_config().redis_url))
        try:
            await client.ping()
        finally:
            await client.aclose()
    except SettingsValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 — any failure means unusable backend
        raise SettingsValidationError([f"redis unreachable: {exc}"]) from exc
