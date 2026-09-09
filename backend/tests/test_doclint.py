"""Seed-document lint (spec §13): the build/dev gate for `.skill.md` and
`.agent.md`.

The first test is the standing regression gate — the documents this repo
actually ships must always lint clean. The rest prove the linter rejects
what the seed would reject, and only warns where a document is legal but
worth fixing."""

from pathlib import Path

from app.doclint import lint_all, lint_skills, main

SKILL = """---
name: {name}
description: does a thing
persona: You are a thing doer.
tools:
{tools}
---
# Purpose
Use {{tool:{mention}}} to do the thing.
"""

AGENT = """---
name: {name}
description: an agent from a file
persona: You are an agent.
{extra}workflow:
  nodes:
    - id: work
      type: skill
      skill: {skill}
  edges:
    - {{ from: START, to: work }}
    - {{ from: work, to: END }}
---
# Notes
"""


def write_skill(
    d: Path, name: str, *, tools: str = "  - fs.read", mention: str = "fs.read"
) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.skill.md"
    p.write_text(SKILL.format(name=name, tools=tools, mention=mention))
    return p


def write_agent(d: Path, name: str, *, skill: str = "s1", extra: str = "") -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.agent.md"
    p.write_text(AGENT.format(name=name, skill=skill, extra=extra))
    return p


def test_shipped_documents_lint_clean() -> None:
    """Standing gate: every seed document in this repo is valid."""
    base = Path(__file__).resolve().parents[1] / "app" / "native"
    errors, warnings = lint_all(base / "skills", base / "sub_agents")
    assert errors == [], f"shipped seed documents have lint errors: {errors}"
    assert warnings == [], f"shipped seed documents have lint warnings: {warnings}"


def test_missing_directories_are_not_errors(tmp_path: Path) -> None:
    errors, warnings = lint_all(tmp_path / "nope", tmp_path / "also-nope")
    assert errors == [] and warnings == []


class TestSkillErrors:
    def test_missing_frontmatter(self, tmp_path: Path) -> None:
        (tmp_path / "x.skill.md").write_text("# just a body")
        errors, _, _ = lint_skills(tmp_path)
        assert any("frontmatter" in e for e in errors)

    def test_unresolved_tool_mention(self, tmp_path: Path) -> None:
        write_skill(tmp_path, "s1", tools="  - fs.read", mention="fs.write")
        errors, _, _ = lint_skills(tmp_path)
        assert any("fs.write" in e and "not a bound tool" in e for e in errors)

    def test_duplicate_names_across_files(self, tmp_path: Path) -> None:
        write_skill(tmp_path, "dup")
        (tmp_path / "other.skill.md").write_text(
            SKILL.format(name="dup", tools="  - fs.read", mention="fs.read")
        )
        errors, _, _ = lint_skills(tmp_path)
        assert any("duplicate skill name" in e for e in errors)

    def test_whitespace_and_duplicate_tool_keys(self, tmp_path: Path) -> None:
        write_skill(tmp_path, "s1", tools="  - 'fs read'\n  - fs.read\n  - fs.read")
        errors, _, _ = lint_skills(tmp_path)
        assert any("whitespace" in e for e in errors)
        assert any("duplicate entries" in e for e in errors)

    def test_bad_max_tool_iterations(self, tmp_path: Path) -> None:
        (tmp_path / "s.skill.md").write_text(
            "---\nname: s\ntools: []\nmax_tool_iterations: 0\n---\nbody"
        )
        errors, _, _ = lint_skills(tmp_path)
        assert any("max_tool_iterations" in e for e in errors)


class TestSkillWarnings:
    def test_filename_mismatch_and_empty_prose_warn_only(self, tmp_path: Path) -> None:
        (tmp_path / "wrong-name.skill.md").write_text("---\nname: s1\ntools: []\n---\n")
        errors, warnings, names = lint_skills(tmp_path)
        assert errors == []
        assert names == {"s1"}
        joined = " ".join(warnings)
        assert "filename does not match" in joined
        assert "empty description" in joined
        assert "empty persona" in joined
        assert "empty instructions" in joined

    def test_unknown_frontmatter_key_warns(self, tmp_path: Path) -> None:
        (tmp_path / "s1.skill.md").write_text(
            "---\nname: s1\ndescription: d\npersona: p\ntools: []\ntoolz: []\n---\nbody"
        )
        errors, warnings, _ = lint_skills(tmp_path)
        assert errors == []
        assert any("unknown frontmatter keys" in w and "toolz" in w for w in warnings)


class TestAgentErrors:
    def test_unknown_skill_name(self, tmp_path: Path) -> None:
        skills, agents = tmp_path / "s", tmp_path / "a"
        write_skill(skills, "s1")
        write_agent(agents, "a1", skill="ghost-skill")
        errors, _ = lint_all(skills, agents)
        assert any("ghost-skill" in e and "not a scanned .skill.md" in e for e in errors)

    def test_missing_workflow(self, tmp_path: Path) -> None:
        agents = tmp_path / "a"
        agents.mkdir()
        (agents / "a1.agent.md").write_text("---\nname: a1\ndescription: d\n---\nbody")
        errors, _ = lint_all(tmp_path / "s", agents)
        assert any("workflow" in e for e in errors)

    def test_bad_model_reference(self, tmp_path: Path) -> None:
        skills, agents = tmp_path / "s", tmp_path / "a"
        write_skill(skills, "s1")
        write_agent(agents, "a1", extra="model: claude-sonnet-4-6\n")
        errors, _ = lint_all(skills, agents)
        assert any("provider:model" in e for e in errors)

    def test_bad_model_params_against_runtime_contract(self, tmp_path: Path) -> None:
        skills, agents = tmp_path / "s", tmp_path / "a"
        write_skill(skills, "s1")
        write_agent(
            agents,
            "a1",
            extra="model: anthropic:claude-sonnet-4-6\nmodel_params: { effort: extreme }\n",
        )
        errors, _ = lint_all(skills, agents)
        assert any("invalid model_params" in e for e in errors)

        write_agent(
            agents,
            "a2",
            extra="model: anthropic:claude-sonnet-4-6\nmodel_params: { efort: low }\n",
        )
        errors, _ = lint_all(skills, agents)
        assert any("invalid model_params" in e for e in errors)

    def test_dag_structure_is_enforced(self, tmp_path: Path) -> None:
        skills, agents = tmp_path / "s", tmp_path / "a"
        write_skill(skills, "s1")
        agents.mkdir(parents=True, exist_ok=True)
        # cycle with no path to END
        (agents / "cyc.agent.md").write_text(
            "---\nname: cyc\ndescription: d\nworkflow:\n"
            "  nodes:\n"
            "    - { id: a, type: skill, skill: s1 }\n"
            "    - { id: b, type: skill, skill: s1 }\n"
            "  edges:\n"
            "    - { from: START, to: a }\n"
            "    - { from: a, to: b }\n"
            "    - { from: b, to: a }\n"
            "---\n"
        )
        errors, _ = lint_all(skills, agents)
        assert errors, "a cyclic workflow with no END must be rejected"

    def test_malformed_form_gate(self, tmp_path: Path) -> None:
        skills, agents = tmp_path / "s", tmp_path / "a"
        write_skill(skills, "s1")
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "g.agent.md").write_text(
            "---\nname: g\ndescription: d\nworkflow:\n"
            "  nodes:\n"
            "    - { id: w, type: skill, skill: s1 }\n"
            "    - id: gate\n"
            "      type: hitl\n"
            "      prompt: 'ok?'\n"
            "      questions:\n"
            "        - { id: q1, kind: choice, prompt: pick, options: [only-one] }\n"
            "  edges:\n"
            "    - { from: START, to: w }\n"
            "    - { from: w, to: gate }\n"
            "    - { from: gate, to: END }\n"
            "---\n"
        )
        errors, _ = lint_all(skills, agents)
        assert any("options' list with >=2" in e for e in errors)

    def test_duplicate_agent_names(self, tmp_path: Path) -> None:
        skills, agents = tmp_path / "s", tmp_path / "a"
        write_skill(skills, "s1")
        write_agent(agents, "a1")
        (agents / "clone.agent.md").write_text(AGENT.format(name="a1", skill="s1", extra=""))
        errors, _ = lint_all(skills, agents)
        assert any("duplicate agent name" in e for e in errors)


class TestAdversarialHoles:
    """Each case was confirmed live against a scratch Postgres by the M12b
    adversarial audit: the document linted clean, then broke the seed."""

    def test_required_static_skills_guarded_in_shipped_tree(self, tmp_path: Path) -> None:
        # renaming web-research.skill.md lints clean but aborts boot, because
        # seed_sub_agents composes research-concierge from it by name
        from app.doclint import lint_all as _lint
        from app.seed.loader import REQUIRED_STATIC_SKILLS, SKILLS_DIR

        errors, _ = _lint(SKILLS_DIR, tmp_path)
        assert errors == []  # the real tree has them
        assert set(REQUIRED_STATIC_SKILLS) <= {
            p.stem.removesuffix(".skill") for p in SKILLS_DIR.glob("*.skill.md")
        }
        # an arbitrary directory is NOT held to the shipped contract
        write_skill(tmp_path / "s", "unrelated")
        errors, _ = _lint(tmp_path / "s", tmp_path / "a")
        assert errors == []

    def test_name_exceeding_registry_column(self, tmp_path: Path) -> None:
        (tmp_path / "long.skill.md").write_text(
            f"---\nname: {'x' * 300}\ndescription: d\npersona: p\ntools: []\n---\nbody"
        )
        errors, _, _ = lint_skills(tmp_path)
        assert any("registry column is 255" in e for e in errors)

    def test_control_characters_in_name(self, tmp_path: Path) -> None:
        (tmp_path / "nul.skill.md").write_text(
            '---\nname: "bad\\0name"\ndescription: d\npersona: p\ntools: []\n---\nbody'
        )
        errors, _, _ = lint_skills(tmp_path)
        assert any("control characters" in e for e in errors)

    def test_bool_max_tool_iterations_rejected(self, tmp_path: Path) -> None:
        # isinstance(True, int) — a YAML bool used to seed a budget of 1
        (tmp_path / "b.skill.md").write_text(
            "---\nname: b\ndescription: d\npersona: p\ntools: []\n"
            "max_tool_iterations: true\n---\nbody"
        )
        errors, _, _ = lint_skills(tmp_path)
        assert any("max_tool_iterations" in e for e in errors)

    def test_int32_overflow_max_tool_iterations(self, tmp_path: Path) -> None:
        (tmp_path / "o.skill.md").write_text(
            "---\nname: o\ndescription: d\npersona: p\ntools: []\n"
            "max_tool_iterations: 10000000000\n---\nbody"
        )
        errors, _, _ = lint_skills(tmp_path)
        assert any("INTEGER column" in e for e in errors)

    def test_non_scalar_prose_and_quoted_bool(self, tmp_path: Path) -> None:
        (tmp_path / "p.skill.md").write_text(
            "---\nname: p\ndescription:\n  short: s\n  long: l\n"
            "persona:\n  - line one\n  - line two\n"
            'direct_exposure: "false"\ntools: []\n---\nbody'
        )
        errors, _, _ = lint_skills(tmp_path)
        joined = " ".join(errors)
        assert "'description' must be a string" in joined
        assert "'persona' must be a string" in joined
        assert "must be a YAML boolean" in joined

    def test_skill_ref_on_non_skill_node(self, tmp_path: Path) -> None:
        """The seed pops `skill` off ANY node, so a dangling ref on a hitl
        node errors the row — the linter used to only look at skill nodes."""
        skills, agents = tmp_path / "s", tmp_path / "a"
        write_skill(skills, "s1")
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "x.agent.md").write_text(
            "---\nname: x\ndescription: d\npersona: p\nworkflow:\n"
            "  nodes:\n"
            "    - { id: w, type: skill, skill: s1 }\n"
            "    - { id: gate, type: hitl, prompt: 'ok?', skill: ghost }\n"
            "  edges:\n"
            "    - { from: START, to: w }\n"
            "    - { from: w, to: gate }\n"
            "    - { from: gate, to: END }\n"
            "---\n"
        )
        errors, warnings = lint_all(skills, agents)
        assert any("ghost" in e for e in errors)
        assert any("the runtime ignores it" in w for w in warnings)

    def test_agent_name_colliding_with_code_registered_agent(self, tmp_path: Path) -> None:
        skills, agents = tmp_path / "s", tmp_path / "a"
        write_skill(skills, "s1")
        write_agent(agents, "workspace-warden")  # a @native_sub_agent name
        errors, _ = lint_all(skills, agents)
        assert any("already owned by a code-registered" in e for e in errors)

        write_agent(agents, "research-concierge")  # the static seed's own
        errors, _ = lint_all(skills, agents)
        assert any("research-concierge" in e for e in errors)

    def test_reserved_and_router_colliding_node_ids(self, tmp_path: Path) -> None:
        skills, agents = tmp_path / "s", tmp_path / "a"
        write_skill(skills, "s1")
        agents.mkdir(parents=True, exist_ok=True)
        for bad_id in ("__start__", "__route__w"):
            (agents / "r.agent.md").write_text(
                "---\nname: r\ndescription: d\npersona: p\nworkflow:\n"
                "  nodes:\n"
                "    - { id: w, type: skill, skill: s1 }\n"
                f"    - {{ id: {bad_id}, type: skill, skill: s1 }}\n"
                "  edges:\n"
                "    - { from: START, to: w }\n"
                f"    - {{ from: w, to: {bad_id} }}\n"
                f"    - {{ from: {bad_id}, to: END }}\n"
                "---\n"
            )
            errors, _ = lint_all(skills, agents)
            assert any("reserved" in e for e in errors), f"{bad_id} must be rejected"

    def test_choice_options_shapes(self, tmp_path: Path) -> None:
        skills, agents = tmp_path / "s", tmp_path / "a"
        write_skill(skills, "s1")
        agents.mkdir(parents=True, exist_ok=True)

        def gate_doc(options: str) -> str:
            return (
                "---\nname: g\ndescription: d\npersona: p\nworkflow:\n"
                "  nodes:\n"
                "    - { id: w, type: skill, skill: s1 }\n"
                "    - id: gate\n      type: hitl\n      prompt: 'ok?'\n"
                "      questions:\n"
                f"        - {{ id: q, kind: choice, prompt: pick, options: {options} }}\n"
                "  edges:\n"
                "    - { from: START, to: w }\n"
                "    - { from: w, to: gate }\n"
                "    - { from: gate, to: END }\n"
                "---\n"
            )

        # scalar string: len('yes') >= 2 used to pass the old length check
        (agents / "g.agent.md").write_text(gate_doc("yes"))
        errors, _ = lint_all(skills, agents)
        assert any("options' list with >=2" in e for e in errors)

        # YAML 1.1 reads these as booleans → blank, indistinguishable chips
        (agents / "g.agent.md").write_text(gate_doc("[yes, no]"))
        errors, _ = lint_all(skills, agents)
        assert any("YAML reads them" in e for e in errors)

        # duplicates render two identical chips
        (agents / "g.agent.md").write_text(gate_doc("['a', 'a']"))
        errors, _ = lint_all(skills, agents)
        assert any("duplicate choice options" in e for e in errors)

        # the correct form still passes
        (agents / "g.agent.md").write_text(gate_doc("['yes', 'no']"))
        errors, _ = lint_all(skills, agents)
        assert errors == []


class TestCli:
    def test_exit_codes(self, tmp_path: Path, capsys: object) -> None:
        skills, agents = tmp_path / "s", tmp_path / "a"
        write_skill(skills, "s1")
        write_agent(agents, "a1")
        assert main(["doclint", str(skills), str(agents)]) == 0
        write_agent(agents, "bad", skill="ghost")
        assert main(["doclint", str(skills), str(agents)]) == 1
