"""Prompt loading — all LLM prompts live here as files, never inline (spec §13)."""

from functools import cache
from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent


@cache
def load_prompt(name: str) -> str:
    """Load a prompt file by stem (e.g. load_prompt('summarize_and_structure'))."""
    path = _PROMPTS_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8").strip()
