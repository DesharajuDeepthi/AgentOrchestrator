"""Agent document parsing (spec §3.4, declarative static custom agents).

`.agent.md` = YAML frontmatter (name, description, persona, model,
model_params, direct_exposure, workflow) + markdown body that is
non-functional documentation. The workflow is the §3.5 schema with one
sugar: skill nodes may reference skills by NAME (`skill:`), resolved to
registry uuids at seed time.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


class AgentDocError(ValueError):
    """Malformed agent document."""


@dataclass
class AgentDoc:
    name: str
    description: str = ""
    persona: str = ""
    model: str | None = None
    model_params: dict[str, Any] | None = None
    direct_exposure: bool = False
    workflow: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    filename: str = ""


def parse_agent_document(text: str, filename: str = "") -> AgentDoc:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise AgentDocError("agent document must start with YAML frontmatter (---)")
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise AgentDocError(f"invalid frontmatter YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise AgentDocError("frontmatter must be a YAML mapping")
    name = meta.get("name")
    if not isinstance(name, str) or not name.strip():
        raise AgentDocError("frontmatter requires a string 'name'")
    workflow = meta.get("workflow")
    if not isinstance(workflow, dict):
        raise AgentDocError("frontmatter requires a 'workflow' mapping (nodes + edges)")
    nodes = workflow.get("nodes")
    edges = workflow.get("edges")
    if not isinstance(nodes, list) or not nodes:
        raise AgentDocError("'workflow.nodes' must be a non-empty list")
    if not isinstance(edges, list) or not edges:
        raise AgentDocError("'workflow.edges' must be a non-empty list")
    for node in nodes:
        if not isinstance(node, dict) or not node.get("id"):
            raise AgentDocError("every workflow node needs an 'id'")
        if node.get("type") == "skill" and not (node.get("skill") or node.get("skill_id")):
            raise AgentDocError(f"skill node {node.get('id')!r} needs 'skill' (name) or 'skill_id'")
    params = meta.get("model_params")
    if params is not None and not isinstance(params, dict):
        raise AgentDocError("'model_params' must be a mapping")
    return AgentDoc(
        name=name.strip(),
        description=str(meta.get("description") or "").strip(),
        persona=str(meta.get("persona") or "").strip(),
        model=str(meta["model"]) if meta.get("model") else None,
        model_params=params,
        direct_exposure=bool(meta.get("direct_exposure", False)),
        workflow={"nodes": nodes, "edges": edges},
        notes=text[match.end() :].strip(),
        filename=filename,
    )


def skill_name_refs(workflow: dict[str, Any]) -> list[str]:
    """Skill NAMES referenced via the by-name sugar (uuid refs pass through)."""
    return [
        str(n["skill"])
        for n in workflow.get("nodes", [])
        if n.get("type") == "skill" and n.get("skill")
    ]


def scan_agent_files(directory: Path) -> tuple[list[AgentDoc], list[str]]:
    """Parse every *.agent.md in a directory. Parse failures never abort the
    scan — they return as error strings so the seed can log them loudly while
    healthy files still load (a broken file must not take down boot)."""
    docs: list[AgentDoc] = []
    errors: list[str] = []
    if not directory.exists():
        return docs, errors
    for path in sorted(directory.glob("*.agent.md")):
        try:
            docs.append(parse_agent_document(path.read_text(encoding="utf-8"), path.name))
        except AgentDocError as exc:
            errors.append(f"{path.name}: {exc}")
    return docs, errors
