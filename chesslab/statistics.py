"""Exploratory results. Complete colour pairs are the sampling unit."""

import math
from collections import defaultdict
from typing import Any

from harness.referee import FAILED_TERMINATIONS


def summarise(games: list[dict[str, Any]], candidate: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for game in games:
        grouped[game["opponent"]].append(game)
    rows = []
    for opponent, scheduled in grouped.items():
        completed = [g for g in scheduled if g["status"] == "completed"]
        scored = [g for g in completed if g["result"] != "void"]
        scores: dict[str, list[float]] = defaultdict(list)
        wins = draws = losses = failures = 0
        for game in scored:
            score = 0.5 if game["result"] == "draw" else float(game[game["result"]] == candidate)
            wins += score == 1
            draws += score == 0.5
            losses += score == 0
            failures += game["termination"] in FAILED_TERMINATIONS and score == 0
            scores[game["pair_id"]].append(score)
        pairs = [sum(values) / 2 for values in scores.values() if len(values) == 2]
        mean = sum(pairs) / len(pairs) if pairs else None
        # Hoeffding avoids a false zero-width interval for all wins/draws/losses.
        # Conditional on independent sampled opening pairs; this is not an SPRT or
        # a confidence claim for an unseen opponent population. No auto-promotion.
        radius = math.sqrt(math.log(40) / (2 * len(pairs))) if pairs else 1
        rows.append(
            {
                "opponent": opponent,
                "games": len(scored),
                "scheduled": len(scheduled),
                "wins": wins,
                "draws": draws,
                "losses": losses,
                "candidate_failures": failures,
                "void": len(completed) - len(scored),
                "pairs": len(pairs),
                "score": mean,
                "interval": None
                if mean is None
                else [max(0, mean - radius), min(1, mean + radius)],
                "interval_method": "95% Hoeffding bound over complete opening pairs (exploratory)",
                "families": len({g["opening"]["family"] for g in scored}),
            }
        )
    return rows
