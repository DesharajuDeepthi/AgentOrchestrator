# ADR-0009: Skills as markdown documents — one format, two homes

Status: Accepted

Date: 2026-08-04

## Context

Skills are the middle tier of the architecture: a minor persona plus tool
bindings plus a multi-step instruction body that guides a single tool loop.
Two authoring populations exist — developers shipping native skills with the
codebase, and admins composing custom skills in the UI at runtime. Giving
each a different representation (say, Python registrations for native,
database rows for custom) would fork validation, preview, and execution
into two code paths and make it impossible to promote a UI-authored skill
into the repo by copy-paste.

## Decision

A skill **is** a markdown document (spec §3.3): YAML frontmatter + markdown
body, parsed by one parser (`backend/app/skilldoc.py`).

- **Frontmatter carries the bindings and knobs**: `name`, `description`,
  `persona`, `tools: [tool_keys]`, `direct_exposure`, and per-skill
  `max_tool_iterations` (nullable — inherits the global setting; the static
  `web-research` skill ships with 20 because research-class loops
  legitimately run deeper than the default).
- **The body is the instructions**: a soft workflow — free-form steps that
  guide the LLM inside one tool-loop node, not machine-enforced (the hard,
  machine-executed workflow is the sub agent DAG, §3.5). Steps may reference
  bound tools inline via `{tool:server.tool_name}` mentions;
  `validate_mentions()` rejects any mention that does not resolve to a
  bound tool, at startup for files and at save for UI edits.
- **Two homes, one shape**: native skills are `*.skill.md` files in
  `backend/app/native/skills/` scanned into the registry at startup
  (`scan_skill_files`); custom skills are the same document shape authored
  in the UI from a template and stored in the registry. `kind` distinguishes
  them; the execution path does not.

## Consequences

Positive:

- One parser, one validator, one preview renderer, one execution path for
  both populations; the UI editor and the on-disk format can never drift.
- Skills are diffable, reviewable prose — a native skill change is a normal
  code review, and a good custom skill can be promoted to native by saving
  the document as a file.
- The frontmatter/body split cleanly separates machine-read configuration
  (bindings, budgets, exposure) from model-read instruction prose.
- `{tool:...}` mention validation catches broken tool references at author
  time instead of mid-run.

Negative:

- YAML frontmatter is a loose schema; `skilldoc.py` must hand-validate types
  (and does), and new frontmatter keys silently pass until a validator
  learns them.
- Soft workflow means no execution guarantees: the model may skip or
  reorder steps, which is by design but surprises authors expecting the DAG
  semantics of sub agents.
- Instructions and configuration travel together, so editing a budget knob
  churns the same document as editing prose.

## References

- spec.md §3.3 (skills, skill document format, `max_tool_iterations`)
- /home/user/concierge-agent/backend/app/skilldoc.py
- /home/user/concierge-agent/backend/app/native/skills/web-research.skill.md,
  file-ops.skill.md
- Related: ADR-0003 (the scoped stack a skill loop runs under), ADR-0010
  (skills as the unit both orchestrator modes dispatch over)
