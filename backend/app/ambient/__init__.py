"""Ambient mode (spec §17) — trigger/decide/execute/deliver planes.

M20 ships the substrate: event store with chaining-guard provenance,
NOTIFY-wake drain, presence + the real idle detector, routines API with
hashed fire tokens. Dark by default: `ambient_enabled=false` is
byte-identical (the loop no-ops, endpoints refuse)."""

from app.ambient.store import AmbientDisabledError, ChainGuardError, emit_event

__all__ = ["AmbientDisabledError", "ChainGuardError", "emit_event"]
