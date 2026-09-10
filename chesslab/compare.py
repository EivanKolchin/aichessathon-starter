"""Matched experiment comparison; never pool different opponents or budgets."""

import math
from collections import defaultdict
from typing import Any


def pair_scores(run: dict[str, Any]) -> dict[tuple[str, str, str, int], float]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for game in run["games"]:
        grouped[game["pair_id"]].append(game)
    scores = {}
    for games in grouped.values():
        if len(games) != 2 or any(
            g["status"] != "completed" or g["result"] == "void" for g in games
        ):
            continue
        first = games[0]
        opponent = run["engines"][first["opponent"]]
        key = (first["opponent"], opponent["sha256"], first["opening"]["fen"], first["seed"])
        values = [
            0.5 if g["result"] == "draw" else float(g[g["result"]] == run["candidate"])
            for g in games
        ]
        scores[key] = sum(values) / 2
    return scores


def compare(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    for run in (baseline, candidate):
        if run["status"] not in {"completed", "stopped"}:
            raise ValueError("Compare only finished, valid experiments")
    for key in ("base_ms", "increment_ms", "ply_cap"):
        if baseline["limits"][key] != candidate["limits"][key]:
            raise ValueError(f"Cannot compare different {key}")
    if baseline["split"] != candidate["split"]:
        raise ValueError("Cannot compare different opening splits")
    if baseline["environment"] != candidate["environment"]:
        raise ValueError("Cannot compare different recorded environments")
    for key in ("harness_sha256", "lab_sha256"):
        if baseline[key] != candidate[key]:
            raise ValueError(
                "Evaluator code changed; rerun both candidates with the same evaluator"
            )
    left, right = pair_scores(baseline), pair_scores(candidate)
    matched = sorted(left.keys() & right.keys())
    if not matched:
        raise ValueError("No complete pairs share the same opponent build, opening and seed")
    grouped: dict[str, list[float]] = defaultdict(list)
    for pair_key in matched:
        grouped[pair_key[0]].append(right[pair_key] - left[pair_key])
    rows = []
    for opponent, differences in grouped.items():
        mean = sum(differences) / len(differences)
        radius = 2 * math.sqrt(math.log(40) / (2 * len(differences)))
        rows.append(
            {
                "opponent": opponent,
                "matched_pairs": len(differences),
                "score_delta": mean,
                "interval": [max(-1, mean - radius), min(1, mean + radius)],
            }
        )
    return {
        "baseline": baseline["id"],
        "candidate": candidate["id"],
        "opponents": rows,
        "unmatched_baseline_pairs": len(left) - len(matched),
        "unmatched_candidate_pairs": len(right) - len(matched),
        "note": "Exploratory matched score differences; 95% Hoeffding bounds assume independent "
        "opening pairs. Do not promote a model from these intervals or repeated peeking.",
    }
