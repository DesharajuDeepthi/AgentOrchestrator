"""Memory probe suite (spec §16.9) — deterministic grading, no LLM judges.

Ability categories follow LongMemEval (research 03 §6): information
extraction, multi-session reasoning, knowledge updates, temporal reasoning,
abstention, preference following. Every probe is graded mechanically
(required/forbidden substrings, format counts) so the experiment matrix is
reproducible and judge-bias-free.

A probe is a list of turns. Each turn runs in a named conversation slot
("A", "B", …) — slots map to REAL conversations created per probe per config,
so cross-conversation recall is genuinely cross-conversation. `expect` turns
are graded; `setup` turns just need to complete (and be consolidated).
"""

from dataclasses import dataclass, field


@dataclass
class Turn:
    conv: str
    message: str
    # grading (only on expect turns)
    require_any: list[list[str]] = field(default_factory=list)  # CNF: each group needs one hit
    forbid: list[str] = field(default_factory=list)
    min_bullets: int = 0
    kind: str = "setup"  # 'setup' | 'expect'
    settle: bool = True  # wait for post-run consolidation before the next turn


@dataclass
class Probe:
    name: str
    ability: str
    turns: list[Turn]


PROBES: list[Probe] = [
    Probe(
        name="single_fact_recall",
        ability="information_extraction",
        turns=[
            Turn(
                "A",
                "For the record: my dog is named Biscuit, my favorite editor is Neovim, "
                "and our staging cluster is called aurora.",
            ),
            Turn(
                "B",
                "What's my dog's name? One short sentence.",
                kind="expect",
                require_any=[["biscuit"]],
            ),
        ],
    ),
    Probe(
        name="second_fact_recall",
        ability="information_extraction",
        turns=[
            # relies on single_fact_recall's setup having run first (same config
            # phase) — a second question over the same stored facts
            Turn(
                "B",
                "Which code editor do I prefer? One short sentence.",
                kind="expect",
                require_any=[["neovim"]],
            ),
        ],
    ),
    Probe(
        name="multi_session_reasoning",
        ability="multi_session",
        turns=[
            Turn("A", "Note for the quarter: our Q3 revenue target is 2 million dollars."),
            Turn("B", "Update from finance: Q3 revenue actually came in at 1.5 million dollars."),
            Turn(
                "C",
                "Did we hit the Q3 revenue target? Answer with the target, the actual, "
                "and yes or no.",
                kind="expect",
                require_any=[["1.5", "1,500,000"], ["2 million", "2.0", "2m", "$2", "2,000,000"]],
            ),
        ],
    ),
    Probe(
        name="knowledge_update",
        ability="knowledge_update",
        turns=[
            Turn("A", "Our deploy branch is 'main'."),
            Turn("B", "Correction: the deploy branch is now 'release-2026'."),
            Turn(
                "C",
                "Which branch do we deploy from? Just the branch name.",
                kind="expect",
                require_any=[["release-2026"]],
            ),
        ],
    ),
    Probe(
        name="temporal_before_after",
        ability="temporal",
        turns=[
            # stretch probe: needs superseded history, the field's weakest
            # ability — expected to fail without as_of tooling exposed
            Turn(
                "C",
                "What was our deploy branch BEFORE the recent change? Just the old name.",
                kind="expect",
                require_any=[["main"]],
                forbid=[],
            ),
        ],
    ),
    Probe(
        name="abstention",
        ability="abstention",
        turns=[
            Turn(
                "B",
                "What is my cat's name? One short sentence.",
                kind="expect",
                require_any=[
                    [
                        "don't know",
                        "do not know",
                        "don't have",
                        "do not have",
                        "no record",
                        "not sure",
                        "never mentioned",
                        "haven't mentioned",
                        "haven't told",
                        "no memory",
                        "not stated",
                        "didn't mention",
                        "don't recall",
                        "no information",
                        "cat's name yet",
                        "don't actually know",
                    ]
                ],
                # the DOG's name must not be transplanted onto the cat (merely
                # mentioning "your dog is Biscuit" while abstaining is correct)
                forbid=["cat's name is biscuit", "cat is named biscuit", "cat is biscuit"],
            ),
        ],
    ),
    Probe(
        name="preference_following",
        ability="preference",
        turns=[
            Turn("A", "I prefer concise bullet-point answers, usually three bullets."),
            Turn(
                "C",
                "Give me tips for naming variables in Python.",
                kind="expect",
                min_bullets=2,
            ),
        ],
    ),
]


def grade(turn: Turn, answer: str) -> tuple[bool, str]:
    """Deterministic pass/fail with a reason string."""
    a = answer.lower()
    for group in turn.require_any:
        if not any(needle.lower() in a for needle in group):
            return False, f"missing any of {group}"
    for needle in turn.forbid:
        if needle.lower() in a:
            return False, f"forbidden '{needle}' present"
    if turn.min_bullets:
        bullets = sum(
            1
            for line in answer.splitlines()
            if line.strip().startswith(("-", "•", "*"))
            or (line.strip()[:2].rstrip(".").isdigit() and len(line.strip()) > 3)
        )
        if bullets < turn.min_bullets:
            return False, f"{bullets} bullets < {turn.min_bullets}"
    return True, "ok"
