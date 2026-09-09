"""Seed-document lint (spec §13): offline validation for `.skill.md` and
`.agent.md` files — the same checks the seed applies, runnable at dev time,
in pytest, and as a Docker build gate, so a malformed document fails the
BUILD instead of landing as a status='error' row at boot.

Errors  = the seed would reject or error the document (build fails).
Warnings = legitimate but unverifiable-offline or advisable-to-fix.

Usage: python -m app.doclint  [skills_dir] [agents_dir]
"""

import re
import sys
from pathlib import Path
from uuid import UUID, uuid4

from app.agentdoc import AgentDocError, parse_agent_document
from app.factory.dag import validate_workflow
from app.skilldoc import SkillDocError, parse_skill_document, validate_mentions

_KNOWN_SKILL_KEYS = {
    "name",
    "description",
    "persona",
    "tools",
    "direct_exposure",
    "max_tool_iterations",
}
_KNOWN_AGENT_KEYS = {
    "name",
    "description",
    "persona",
    "model",
    "model_params",
    "direct_exposure",
    "workflow",
}
_MODEL_REF_RE = re.compile(r"^[a-z][a-z0-9_]*:[A-Za-z0-9._\-]+$")
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---", re.DOTALL)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
# registry columns the documents feed (spec §3): names are String(255) and
# max_tool_iterations is a Postgres INTEGER — over-running either aborts the
# seed transaction at boot, so the document must not be shippable
_NAME_MAX = 255
_INT32_MAX = 2_147_483_647


def _frontmatter(text: str) -> dict[str, object]:
    import yaml

    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return {}
    return meta if isinstance(meta, dict) else {}


def _frontmatter_keys(text: str) -> set[str]:
    return set(_frontmatter(text))


def _name_errors(filename: str, name: str) -> list[str]:
    out: list[str] = []
    if len(name) > _NAME_MAX:
        out.append(f"{filename}: name is {len(name)} chars; the registry column is {_NAME_MAX}")
    if _CONTROL_CHARS_RE.search(name):
        out.append(f"{filename}: name contains control characters (Postgres rejects NUL bytes)")
    return out


def _prose_errors(filename: str, raw: dict[str, object]) -> list[str]:
    """description/persona are str()-coerced by the parsers, so a nested YAML
    value would reach the registry as a Python repr — and the description is
    what the planner routes on. direct_exposure is bool()-coerced, so the
    string "false" would seed as EXPOSED, inverting the author's intent."""
    out: list[str] = []
    for key in ("description", "persona"):
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            out.append(
                f"{filename}: {key!r} must be a string — a {type(value).__name__} would be "
                "stored as its Python repr"
            )
    exposure = raw.get("direct_exposure")
    if exposure is not None and not isinstance(exposure, bool):
        out.append(
            f"{filename}: 'direct_exposure' must be a YAML boolean, not "
            f'{type(exposure).__name__} — quoted "false" would seed as exposed'
        )
    return out


def lint_skills(directory: Path) -> tuple[list[str], list[str], set[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    names: set[str] = set()
    for path in sorted(directory.glob("*.skill.md")) if directory.exists() else []:
        text = path.read_text(encoding="utf-8")
        try:
            doc = parse_skill_document(text)
        except SkillDocError as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        if doc.name in names:
            errors.append(f"{path.name}: duplicate skill name {doc.name!r}")
        names.add(doc.name)
        errors.extend(_name_errors(path.name, doc.name))
        errors.extend(_prose_errors(path.name, _frontmatter(text)))
        if doc.max_tool_iterations is not None and doc.max_tool_iterations > _INT32_MAX:
            errors.append(f"{path.name}: max_tool_iterations exceeds the registry INTEGER column")
        for err in validate_mentions(doc.instructions, doc.tools):
            errors.append(f"{path.name}: {err}")
        for key in doc.tools:
            if not key.strip() or key != key.strip() or " " in key:
                errors.append(f"{path.name}: tool key {key!r} has whitespace")
        if len(set(doc.tools)) != len(doc.tools):
            errors.append(f"{path.name}: duplicate entries in 'tools'")
        if path.name != f"{doc.name}.skill.md":
            warnings.append(f"{path.name}: filename does not match name {doc.name!r}")
        if not doc.description.strip():
            warnings.append(f"{path.name}: empty description — the planner routes on it")
        if not doc.instructions.strip():
            warnings.append(f"{path.name}: empty instructions body")
        if not doc.persona.strip():
            warnings.append(f"{path.name}: empty persona")
        unknown = _frontmatter_keys(text) - _KNOWN_SKILL_KEYS
        if unknown:
            warnings.append(f"{path.name}: unknown frontmatter keys {sorted(unknown)}")
    return errors, warnings, names


def _reserved_agent_names() -> set[str]:
    """Names owned by code: @native_sub_agent registrations plus the static
    seed's own agent. Import failures degrade to the hardcoded set rather
    than failing the lint over an unrelated import error."""
    from app.seed.loader import STATIC_SEED_AGENT_NAMES

    names = set(STATIC_SEED_AGENT_NAMES)
    try:
        from app.native.provider import native_sub_agents, scan_native

        scan_native()
        names |= set(native_sub_agents())
    except Exception:  # noqa: BLE001 - lint must not die on an import problem
        pass
    return names


def lint_agents(directory: Path, skill_names: set[str]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    seen: set[str] = set()
    reserved_agent_names = _reserved_agent_names()
    for path in sorted(directory.glob("*.agent.md")) if directory.exists() else []:
        text = path.read_text(encoding="utf-8")
        try:
            doc = parse_agent_document(text, path.name)
        except AgentDocError as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        if doc.name in seen:
            errors.append(f"{path.name}: duplicate agent name {doc.name!r}")
        seen.add(doc.name)
        if doc.model is not None and not _MODEL_REF_RE.match(doc.model):
            errors.append(f"{path.name}: model {doc.model!r} is not a provider:model reference")
        if doc.model_params is not None:
            # validate against the REAL runtime contract (extra='forbid', so
            # typos and bad effort values fail here instead of at first run)
            from pydantic import ValidationError

            from app.llm import ModelParams

            try:
                ModelParams.model_validate(doc.model_params)
            except ValidationError as exc:
                detail = "; ".join(
                    f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
                )
                errors.append(f"{path.name}: invalid model_params — {detail}")
        if doc.model_params is not None and doc.model is None:
            warnings.append(f"{path.name}: model_params set without an explicit model")
        errors.extend(_name_errors(path.name, doc.name))
        errors.extend(_prose_errors(path.name, _frontmatter(text)))
        if doc.name in reserved_agent_names:
            errors.append(
                f"{path.name}: name {doc.name!r} is already owned by a code-registered or "
                "static-seeded sub agent — the two would fight over one registry row"
            )
        if path.name != f"{doc.name}.agent.md":
            warnings.append(f"{path.name}: filename does not match name {doc.name!r}")
        if not doc.persona.strip():
            warnings.append(f"{path.name}: empty persona")
        if not doc.description.strip():
            warnings.append(f"{path.name}: empty description — the planner routes on it")
        unknown = _frontmatter_keys(text) - _KNOWN_AGENT_KEYS
        if unknown:
            warnings.append(f"{path.name}: unknown frontmatter keys {sorted(unknown)}")
        # resolve by-name refs against the scanned .skill.md set; uuid refs
        # point at dynamic records and cannot be verified offline. Collect
        # from EVERY node, not just type='skill' ones — the seed pops `skill`
        # off any node, so a stray ref on a hitl node still has to resolve
        for node in doc.workflow["nodes"]:
            if not isinstance(node, dict):
                continue
            ref = node.get("skill")
            if ref is None:
                continue
            if not isinstance(ref, str) or ref not in skill_names:
                errors.append(
                    f"{path.name}: node {node.get('id')!r} references skill {ref!r}, which is "
                    "not a scanned .skill.md — by-name references must resolve to static "
                    "skill files"
                )
            if node.get("type") != "skill":
                warnings.append(
                    f"{path.name}: node {node.get('id')!r} is type "
                    f"{node.get('type')!r} but declares a skill — the runtime ignores it"
                )
        synthetic: dict[str, str] = {n: str(uuid4()) for n in skill_names}
        active_ids = set(synthetic.values())
        workflow = {"nodes": [], "edges": list(doc.workflow["edges"])}
        for node in doc.workflow["nodes"]:
            node = dict(node)
            ref = node.pop("skill", None)
            if ref is not None and ref in synthetic:
                node["skill_id"] = synthetic[ref]
            elif node.get("skill_id"):
                sid = str(node["skill_id"])
                try:
                    UUID(sid)
                    warnings.append(
                        f"{path.name}: node {node.get('id')!r} references skill by uuid — "
                        "unverifiable offline (verified at seed)"
                    )
                    active_ids.add(sid)
                except ValueError:
                    errors.append(f"{path.name}: node {node.get('id')!r} skill_id is not a uuid")
            workflow["nodes"].append(node)
        for err in validate_workflow(workflow, active_ids):
            # unresolved by-name refs already errored above with a better message
            if "skill nodes require an explicit" in err and any(
                n.get("type") == "skill" and not n.get("skill_id") for n in workflow["nodes"]
            ):
                continue
            errors.append(f"{path.name}: {err}")
    return errors, warnings


def lint_all(skills_dir: Path, agents_dir: Path) -> tuple[list[str], list[str]]:
    skill_errors, skill_warnings, names = lint_skills(skills_dir)
    agent_errors, agent_warnings = lint_agents(agents_dir, names)
    errors = skill_errors + agent_errors
    # the shipped skills directory additionally owes the seed the two skills
    # research-concierge is composed from — renaming one lints clean and then
    # aborts boot, so the shipped tree is held to a stricter contract than an
    # arbitrary directory under test
    from app.seed.loader import REQUIRED_STATIC_SKILLS, SKILLS_DIR

    if skills_dir.resolve() == SKILLS_DIR.resolve():
        for required in REQUIRED_STATIC_SKILLS:
            if required not in names:
                errors.append(
                    f"skills directory is missing {required!r} — the static seed composes "
                    "research-concierge from it and aborts boot without it"
                )
    return errors, skill_warnings + agent_warnings


def main(argv: list[str]) -> int:
    base = Path(__file__).resolve().parent / "native"
    skills_dir = Path(argv[1]) if len(argv) > 1 else base / "skills"
    agents_dir = Path(argv[2]) if len(argv) > 2 else base / "sub_agents"
    errors, warnings = lint_all(skills_dir, agents_dir)
    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")
    n_skills = len(list(skills_dir.glob("*.skill.md"))) if skills_dir.exists() else 0
    n_agents = len(list(agents_dir.glob("*.agent.md"))) if agents_dir.exists() else 0
    print(
        f"doclint: {n_skills} skill file(s), {n_agents} agent file(s) — "
        f"{len(errors)} error(s), {len(warnings)} warning(s)"
    )
    return 1 if errors else 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    sys.exit(main(sys.argv))
