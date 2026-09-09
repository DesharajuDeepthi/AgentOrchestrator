"""Memory layers (spec §16) — semantic store, episodic digests, retrieval.

Layout:
- store.py  — L2 writes: admission gate, provenance/quarantine rules,
              bi-temporal supersession, soft/hard delete, embedding write-through
- rank.py   — retrieval: hybrid lexical+vector candidates, composite scoring
              (relevance × recency × importance), score floor, pinned rows,
              point-in-time (as_of) queries, access bookkeeping
"""

from app.memory.rank import RecallHit, recall
from app.memory.store import (
    MemoryWriteError,
    forget,
    gate_candidates,
    hard_delete,
    remember,
    supersede,
)

__all__ = [
    "MemoryWriteError",
    "RecallHit",
    "forget",
    "gate_candidates",
    "hard_delete",
    "recall",
    "remember",
    "supersede",
]
