# Skill Document Format (`.skill.md`)

A skill is the atomic unit of capability: a persona, multi-step instructions, and a strict set of bound tools, executed as one bounded tool loop. There is **one document format with two homes** (`backend/app/skilldoc.py`):

- **Native skills** — `.skill.md` files in `backend/app/native/skills/`, scanned and seeded at startup.
- **Custom skills** — the same document shape authored in the UI and saved via [`POST /api/v1/skills`](rest-api.md).

## Document shape

A skill document is YAML frontmatter followed by a markdown body:

```markdown
---
name: ...
description: ...
persona: ...
tools:
  - <tool_key>
direct_exposure: false
max_tool_iterations: 20
---
<instructions body>
```

The document **must** start with a `---` frontmatter fence (`parse_skill_document` raises `skill document must start with YAML frontmatter (---)` otherwise), and the frontmatter must be a YAML mapping.

### Frontmatter schema

Every key recognized by `parse_skill_document` (`backend/app/skilldoc.py`); anything else is ignored:

| Key | Required | Type | Default | Meaning |
|---|---|---|---|---|
| `name` | **yes** | non-empty string | — | Registry name. For native skills this is the stable identity across re-seeds (matched on `name` + `source="static"`). |
| `description` | no | string | `""` | What the skill does — this is what the planner/router reads, so make it discriminating. |
| `persona` | no | string | `""` | System-prompt persona for the skill's loop. Assembled after the sub-agent persona and before the instructions (`assemble_skill_prompt`, `backend/app/factory/worker.py`). |
| `tools` | no | list of strings | `[]` | **Tool bindings by `tool_key`** (e.g. `filesystem.read_file`, `fetch.fetch`, `summarize-and-structure`). Must be a list of strings (`'tools' must be a list of tool_key strings`). |
| `direct_exposure` | no | bool | `false` | When `true`, the orchestrator may resolve the skill on the `direct_skill` ladder rung and run it inline, without a sub-agent. |
| `max_tool_iterations` | no | positive int | unset | Per-skill tool-loop budget override. Must be `>= 1` (`'max_tool_iterations' must be a positive integer`). When unset, the `max_tool_iterations` app setting applies (`_max_tool_iterations`, `backend/app/factory/worker.py`). |

Note: a **model override** (`model`, `model_params`) exists on the skill registry record and the API, but is *not* a frontmatter key — native skills always resolve models via the sub-agent override or settings defaults.

### Body conventions

Everything after the frontmatter is the skill's `instructions`. The native skills follow a consistent structure worth copying: a `# Purpose` line, a `## Steps` numbered list, and an `## Output format` section (see `backend/app/native/skills/web-research.skill.md`).

Steps reference bound tools with **`{tool:<tool_key>}` mentions**. These are validated, not decorative: every mention must resolve to a tool listed in `tools` (frontmatter) or `tool_ids` (API). Violations fail the startup scan (`SkillDocError`) and return 422 on API create/patch: `instructions mention {tool:x} but 'x' is not a bound tool` (`validate_mentions`). Steps that are pure reasoning simply use no mention.

## Two homes, one registry

**Native (files on disk).** At startup (and on `POST /api/v1/seed/reload`), `scan_skill_files` parses every `*.skill.md` in `backend/app/native/skills/`, validates mentions, and `seed_native_skills` (`backend/app/seed/loader.py`) upserts each into the skills registry with `kind: "native"`, `source: "static"`. Tool bindings resolve by `tool_key` — MCP tools not yet ingested reconcile on a later seed run. Re-seeding updates `description`, `persona`, `instructions`, `max_tool_iterations`, and bindings in place; the registry `id` never changes. Because the record is `static`, API writes are limited to toggling `status` and `direct_exposure`, and deletes are rejected — edit the file and reload instead ([rest-api.md](rest-api.md), static-record rules).

**Custom (UI/API-authored).** `POST /api/v1/skills` takes the same fields with `tool_ids` (registry UUIDs) instead of `tool_key` strings, plus optional `model`/`model_params`. All referenced tools must exist and be `active` (422 otherwise), and `{tool:...}` mentions are validated against the resolved bindings. Custom skills are `kind: "custom"`, `source: "dynamic"` — fully editable and soft-deletable (409 if active sub-agents still reference them).

## The strict tool-binding rule

**Binding = availability.** A skill's tool loop sees *exactly* its bound tools — nothing else (`resolve_skill_tools`, `backend/app/factory/worker.py`; enforced at runtime by the scoped ToolsRegistry middleware from `build_middleware_stack`). Tools resolve live through the registry cache at node-execution time, so only tools that are still `active` and non-deleted materialize; a tool that went inactive after binding simply doesn't appear. There is no ambient tool access — if a step needs a tool, bind it.

Skills are executed inside sub-agent workflow nodes ([workflow-dsl.md](workflow-dsl.md)), inline on the `direct_skill` rung, or composed into rung-4 ephemeral workers; in every case the same loop budget (`max_tool_iterations`) and the same binding rule apply.

## Complete annotated example

```markdown
---
# Registry identity — stable across re-seeds for native skills.
name: changelog-scribe

# What the planner reads when deciding to route work here.
description: Turn raw commit notes into a polished changelog file in the workspace.

# System-prompt persona for this skill's loop (after the sub-agent persona).
persona: You are a meticulous release-notes editor. Group changes by theme,
  keep entries terse, and never invent changes that are not in the input.

# Strict bindings by tool_key — the loop sees ONLY these tools.
tools:
  - summarize-and-structure     # native tool (kind: native)
  - filesystem.write_file       # MCP tool (kind: mcp), key = server.tool
  - filesystem.list_directory

# Allow the orchestrator to run this skill directly (direct_skill rung).
direct_exposure: true

# Loop budget override; omit to inherit the max_tool_iterations setting.
max_tool_iterations: 12
---
# Purpose

Convert raw commit notes into a `CHANGELOG.md` entry in the workspace.

## Steps

1. Structure the raw notes into themed groups with {tool:summarize-and-structure}.
2. Check for an existing changelog with {tool:filesystem.list_directory} so you
   append rather than clobber.
3. Write the formatted entry with {tool:filesystem.write_file} and state the
   exact path written.
4. Review the result for invented items — remove anything not present in the
   input. This step uses no tool; it is pure reasoning.

## Output format

The changelog entry as markdown, followed by a one-line confirmation of the
file path written.
```

Every `{tool:...}` mention above appears in `tools`, so the document passes `validate_mentions`. Drop `filesystem.list_directory` from the frontmatter and the scan (or API save) fails with `instructions mention {tool:filesystem.list_directory} but 'filesystem.list_directory' is not a bound tool`.
