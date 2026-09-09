"""Workflow DAG structural validation (spec §3.5).

Validation at save (reject, don't warn): exactly one START edge, at least one
path to END, no cycles, all skill_ids resolve to active skills, unique node
ids, edges reference existing nodes, at most one error edge per node.
"""

import re
from collections import defaultdict
from typing import Any

START = "START"
END = "END"
NODE_TYPES = {"skill", "hitl"}
# the factory synthesizes a router node per branching node as
# f"{ROUTER_PREFIX}{node_id}" (worker.py); a user node id that collides with
# one makes the compiled graph ambiguous, and LangGraph reserves its own
# dunder sentinels (__start__/__end__) — reject the whole shape
ROUTER_PREFIX = "__route__"
_RESERVED_ID_RE = re.compile(r"^__.*__$")


def validate_workflow(
    workflow: dict[str, Any] | None,
    active_skill_ids: set[str],
) -> list[str]:
    """Return the full list of structural errors (empty = valid)."""
    errors: list[str] = []
    if not isinstance(workflow, dict):
        return ["workflow must be an object with 'nodes' and 'edges'"]
    nodes = workflow.get("nodes")
    edges = workflow.get("edges")
    if not isinstance(nodes, list) or not nodes:
        errors.append("workflow needs a non-empty 'nodes' list")
        nodes = []
    if not isinstance(edges, list) or not edges:
        errors.append("workflow needs a non-empty 'edges' list")
        edges = []

    node_ids: list[str] = []
    for n in nodes:
        if not isinstance(n, dict) or not isinstance(n.get("id"), str) or not n.get("id"):
            errors.append("every node needs a string 'id'")
            continue
        nid = n["id"]
        node_ids.append(nid)
        ntype = n.get("type")
        if ntype not in NODE_TYPES:
            errors.append(f"node {nid!r}: type must be one of {sorted(NODE_TYPES)}")
        elif ntype == "skill":
            skill_id = n.get("skill_id")
            if not isinstance(skill_id, str) or not skill_id:
                errors.append(f"node {nid!r}: skill nodes require an explicit 'skill_id'")
            elif skill_id not in active_skill_ids:
                errors.append(
                    f"node {nid!r}: skill_id {skill_id!r} does not resolve to an active skill"
                )
        elif ntype == "hitl":
            prompt = n.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                errors.append(f"node {nid!r}: hitl nodes require a non-empty 'prompt'")
            questions = n.get("questions")
            if questions is not None:
                # form gates (spec §3.5): ids unique, choice needs >=2 options
                if not isinstance(questions, list) or not questions:
                    errors.append(f"node {nid!r}: 'questions' must be a non-empty list")
                else:
                    qids: list[str] = []
                    for q in questions:
                        if not isinstance(q, dict) or not str(q.get("id") or "").strip():
                            errors.append(f"node {nid!r}: every question needs a non-empty 'id'")
                            continue
                        qids.append(str(q["id"]))
                        if q.get("kind") not in {"approve", "choice", "text"}:
                            errors.append(
                                f"node {nid!r} question {q['id']!r}: kind must be "
                                "approve|choice|text"
                            )
                        if q.get("kind") == "choice":
                            opts = q.get("options")
                            if not isinstance(opts, list) or len(opts) < 2:
                                # a scalar `options: yes` has len()>=2 as a
                                # string — type-check before measuring
                                errors.append(
                                    f"node {nid!r} question {q['id']!r}: choice needs an "
                                    "'options' list with >=2 entries"
                                )
                            elif not all(isinstance(o, str) and o.strip() for o in opts):
                                # YAML 1.1 reads [yes, no] / [on, off] as
                                # booleans — the chips would render blank
                                errors.append(
                                    f"node {nid!r} question {q['id']!r}: choice options must be "
                                    "non-empty strings (quote yes/no/on/off — YAML reads them "
                                    "as booleans)"
                                )
                            elif len(set(opts)) != len(opts):
                                errors.append(
                                    f"node {nid!r} question {q['id']!r}: duplicate choice options"
                                )
                    if len(set(qids)) != len(qids):
                        errors.append(f"node {nid!r}: question ids must be unique")
        if nid in {START, END}:
            errors.append(f"node id {nid!r} is reserved")
        elif _RESERVED_ID_RE.match(nid) or nid.startswith(ROUTER_PREFIX):
            # LangGraph's own __start__/__end__ sentinels, plus the factory's
            # __route__<node> namespace (which does NOT end in __)
            errors.append(
                f"node id {nid!r} is reserved (ids may not be __dunder__ or start with "
                f"{ROUTER_PREFIX!r})"
            )

    seen: set[str] = set()
    for nid in node_ids:
        if nid in seen:
            errors.append(f"node ids must be unique: duplicate {nid!r}")
        seen.add(nid)
    known = set(node_ids)

    start_edges = 0
    error_edge_count: dict[str, int] = defaultdict(int)
    adjacency: dict[str, set[str]] = defaultdict(set)
    for e in edges:
        if not isinstance(e, dict) or "from" not in e or "to" not in e:
            errors.append("every edge needs 'from' and 'to'")
            continue
        src, dst = e["from"], e["to"]
        on = e.get("on", "success")
        if on not in {"success", "error"}:
            errors.append(f"edge {src}->{dst}: 'on' must be 'success' or 'error'")
        if src == START:
            start_edges += 1
        elif src not in known:
            errors.append(f"edge references unknown node {src!r}")
        if dst != END and dst not in known:
            errors.append(f"edge references unknown node {dst!r}")
        if src == END:
            errors.append("edges cannot leave END")
        if dst == START:
            errors.append("edges cannot enter START")
        if on == "error" and src not in {START, END}:
            error_edge_count[src] += 1
        adjacency[src].add(dst)

    if start_edges != 1:
        errors.append(f"workflow must have exactly one START edge (found {start_edges})")

    for nid, count in error_edge_count.items():
        if count > 1:
            errors.append(f"node {nid!r} has {count} error edges; at most one is allowed")

    # cycle detection over real nodes (START/END are acyclic by construction)
    state: dict[str, int] = {}  # 0=unvisited 1=in-stack 2=done

    def has_cycle(node: str) -> bool:
        state[node] = 1
        for nxt in adjacency.get(node, ()):
            if nxt in {END, START} or nxt not in known:
                continue
            s = state.get(nxt, 0)
            if s == 1 or (s == 0 and has_cycle(nxt)):
                return True
        state[node] = 2
        return False

    if any(state.get(n, 0) == 0 and has_cycle(n) for n in known):
        errors.append("workflow contains a cycle; the DAG must be acyclic")

    # at least one path START -> END
    frontier = [START]
    reachable: set[str] = set()
    while frontier:
        cur = frontier.pop()
        for nxt in adjacency.get(cur, ()):
            if nxt not in reachable:
                reachable.add(nxt)
                frontier.append(nxt)
    if END not in reachable:
        errors.append("workflow has no path from START to END")

    return errors


def workflow_skill_ids(workflow: dict[str, Any]) -> list[str]:
    """Distinct skill ids referenced by the DAG (for sub_agent_skills)."""
    out: list[str] = []
    for n in workflow.get("nodes", []):
        sid = n.get("skill_id")
        if isinstance(sid, str) and sid and sid not in out:
            out.append(sid)
    return out
