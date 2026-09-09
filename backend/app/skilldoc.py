"""Skill document parsing (spec §3.3).

One format, two homes: native skills are `.skill.md` files scanned at startup;
custom skills are the same document shape authored in the UI. Frontmatter
(name, description, persona, tools, direct_exposure) + markdown body
(instructions). Steps may reference bound tools via `{tool:...}` mentions.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_MENTION_RE = re.compile(r"\{tool:([^}]+)\}")


class SkillDocError(ValueError):
    """Malformed skill document."""


@dataclass
class SkillDoc:
    name: str
    description: str = ""
    persona: str = ""
    tools: list[str] = field(default_factory=list)
    direct_exposure: bool = False
    max_tool_iterations: int | None = None
    instructions: str = ""


def parse_skill_document(text: str) -> SkillDoc:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise SkillDocError("skill document must start with YAML frontmatter (---)")
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise SkillDocError(f"invalid frontmatter YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise SkillDocError("frontmatter must be a YAML mapping")
    name = meta.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SkillDocError("frontmatter requires a string 'name'")
    tools = meta.get("tools") or []
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        raise SkillDocError("'tools' must be a list of tool_key strings")
    max_iter = meta.get("max_tool_iterations")
    # isinstance(True, int) is True and True >= 1 — a YAML bool would slip
    # through and seed a loop budget of exactly one tool call
    if max_iter is not None and (
        isinstance(max_iter, bool) or not isinstance(max_iter, int) or max_iter < 1
    ):
        raise SkillDocError("'max_tool_iterations' must be a positive integer")
    return SkillDoc(
        name=name.strip(),
        description=str(meta.get("description") or ""),
        persona=str(meta.get("persona") or ""),
        tools=list(tools),
        direct_exposure=bool(meta.get("direct_exposure", False)),
        max_tool_iterations=max_iter,
        instructions=text[match.end() :].strip(),
    )


def extract_tool_mentions(instructions: str) -> list[str]:
    return [m.strip() for m in _MENTION_RE.findall(instructions)]


def validate_mentions(instructions: str, bound_tool_keys: list[str]) -> list[str]:
    """Every {tool:...} mention must resolve to a bound tool. Returns errors."""
    bound = set(bound_tool_keys)
    return [
        f"instructions mention {{tool:{m}}} but {m!r} is not a bound tool"
        for m in extract_tool_mentions(instructions)
        if m not in bound
    ]


def scan_skill_files(directory: Path) -> list[SkillDoc]:
    """Parse every *.skill.md in a directory (native skill startup scan)."""
    docs: list[SkillDoc] = []
    for path in sorted(directory.glob("*.skill.md")):
        doc = parse_skill_document(path.read_text(encoding="utf-8"))
        errors = validate_mentions(doc.instructions, doc.tools)
        if errors:
            raise SkillDocError(f"{path.name}: {'; '.join(errors)}")
        docs.append(doc)
    return docs
