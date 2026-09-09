"""Skill document parsing (spec §3.3): frontmatter + markdown body,
{tool:...} mention extraction/validation, .skill.md directory scan."""

from pathlib import Path

import pytest

from app.skilldoc import (
    SkillDocError,
    extract_tool_mentions,
    parse_skill_document,
    scan_skill_files,
    validate_mentions,
)

DOC = """---
name: web-research
description: Research the web and cite sources.
persona: You are a careful researcher. Always cite sources.
tools:
  - fetch.fetch
  - summarize-and-structure
direct_exposure: true
---
# Purpose
Research a topic.

## Steps
1. Fetch relevant pages with {tool:fetch.fetch}.
2. Structure findings with {tool:summarize-and-structure}.
3. Synthesize an answer with citations (no tool).
"""


def test_parse_full_document() -> None:
    doc = parse_skill_document(DOC)
    assert doc.name == "web-research"
    assert doc.description == "Research the web and cite sources."
    assert doc.persona.startswith("You are a careful researcher")
    assert doc.tools == ["fetch.fetch", "summarize-and-structure"]
    assert doc.direct_exposure is True
    assert doc.instructions.startswith("# Purpose")
    assert "Synthesize" in doc.instructions


def test_parse_defaults() -> None:
    doc = parse_skill_document("---\nname: minimal\n---\nBody only.")
    assert doc.name == "minimal"
    assert doc.tools == []
    assert doc.direct_exposure is False
    assert doc.instructions == "Body only."


@pytest.mark.parametrize(
    "text",
    [
        "no frontmatter at all",
        "---\ndescription: missing name\n---\nbody",
        "---\nname: [not, a, string]\n---\nbody",
    ],
)
def test_parse_invalid_documents_rejected(text: str) -> None:
    with pytest.raises(SkillDocError):
        parse_skill_document(text)


def test_extract_tool_mentions() -> None:
    body = "Use {tool:fetch.fetch} then {tool:fs.read_file}; ignore {nottool:x}."
    assert extract_tool_mentions(body) == ["fetch.fetch", "fs.read_file"]


def test_validate_mentions_all_bound() -> None:
    doc = parse_skill_document(DOC)
    assert validate_mentions(doc.instructions, doc.tools) == []


def test_validate_mention_of_untagged_tool_rejected() -> None:
    errors = validate_mentions("Call {tool:not.bound} now.", ["fetch.fetch"])
    assert errors and "not.bound" in errors[0]


def test_scan_native_skill_files() -> None:
    skills_dir = Path(__file__).resolve().parents[1] / "app" / "native" / "skills"
    docs = scan_skill_files(skills_dir)
    names = {d.name for d in docs}
    assert {"web-research", "file-ops"} <= names
    web = next(d for d in docs if d.name == "web-research")
    assert "summarize-and-structure" in web.tools
    file_ops = next(d for d in docs if d.name == "file-ops")
    assert file_ops.persona
    assert file_ops.tools
