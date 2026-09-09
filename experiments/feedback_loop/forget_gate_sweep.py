"""FLE-2: quantitative check of the M44 hybrid forget gate.

M44 shipped the hybrid gate on two live-measured data points (paraphrase
cosines 0.876 and 0.847). This sweep builds deterministic populations
SEEDED at those measurements and asks whether the shipped operating point
(threshold 0.85, gray band ≥ 0.70 with a shared payload-token anchor) is
actually on the frontier — or just where two anecdotes pushed it.

Populations (cosine vs the tombstone, anchor-share = how often the
candidate carries the forgotten fact's payload token):
  paraphrases  — MUST suppress; centered on the live pair, anchored 0.9
  value-updates — MUST admit (a new value for the same topic), anchored 0.05
  unrelated     — MUST admit, anchored 0.02

Metrics per (threshold, band, anchors on/off): false-admit rate over
paraphrases, false-suppress rate over updates+unrelated.

Usage: python experiments/feedback_loop/forget_gate_sweep.py
       (writes result_forget_gate_sweep.json beside this file)
"""

import json
from pathlib import Path

# deterministic populations, 40 each — paraphrases centered on the two
# live-measured points (0.876, 0.847) with realistic scatter
PARAPHRASES = [
    (0.820 + 0.004 * i, i % 10 != 0)  # cosine 0.820..0.976, 90% anchored
    for i in range(40)
]
UPDATES = [
    (0.700 + 0.004 * i, i % 20 == 0)  # 0.700..0.856 — overlaps the paraphrase band
    for i in range(40)
]
UNRELATED = [
    (0.050 + 0.010 * i, i == 0)  # 0.05..0.44
    for i in range(40)
]


def suppressed(cos: float, anchored: bool, threshold: float, band: float, use_anchor: bool) -> bool:
    if cos >= threshold:
        return True
    if use_anchor and anchored and cos >= band:
        return True
    return False


def evaluate(threshold: float, band: float, use_anchor: bool) -> dict[str, float]:
    fa = sum(1 for c, a in PARAPHRASES if not suppressed(c, a, threshold, band, use_anchor))
    fs = sum(1 for c, a in UPDATES + UNRELATED if suppressed(c, a, threshold, band, use_anchor))
    return {
        "threshold": threshold,
        "band": band,
        "anchors": use_anchor,
        "false_admit_paraphrase": round(fa / len(PARAPHRASES), 3),
        "false_suppress_other": round(fs / (len(UPDATES) + len(UNRELATED)), 3),
    }


def main() -> None:
    grid = []
    for t10 in range(80, 93):  # threshold 0.80..0.92
        for b100 in (65, 70, 75, 80):
            for use_anchor in (False, True):
                grid.append(evaluate(t10 / 100, b100 / 100, use_anchor))
    shipped = evaluate(0.85, 0.70, True)
    threshold_only_085 = evaluate(0.85, 1.0, False)
    threshold_only_080 = evaluate(0.80, 1.0, False)
    # the frontier: configurations no other configuration strictly beats
    frontier = [
        g
        for g in grid
        if not any(
            o["false_admit_paraphrase"] < g["false_admit_paraphrase"]
            and o["false_suppress_other"] <= g["false_suppress_other"]
            or o["false_admit_paraphrase"] <= g["false_admit_paraphrase"]
            and o["false_suppress_other"] < g["false_suppress_other"]
            for o in grid
        )
    ]
    out = {
        "shipped_operating_point": shipped,
        "threshold_only_at_085": threshold_only_085,
        "threshold_only_at_080": threshold_only_080,
        "shipped_is_on_frontier": any(
            f["threshold"] == 0.85 and f["band"] == 0.70 and f["anchors"] for f in frontier
        ),
        "pareto_frontier": sorted(
            frontier, key=lambda g: (g["false_admit_paraphrase"], g["false_suppress_other"])
        ),
    }
    path = Path(__file__).with_name("result_forget_gate_sweep.json")
    path.write_text(json.dumps(out, indent=2))
    print(json.dumps({k: v for k, v in out.items() if k != "pareto_frontier"}, indent=2))
    print(f"frontier size: {len(out['pareto_frontier'])}")


if __name__ == "__main__":
    main()
