"""Reproducible development searches, optionally with a Python call profile.

Run as: python -m chesslab.experiments.profile_a0 --nodes 10000
This measures search effort; it does not estimate playing strength.
"""

import argparse
import cProfile
import json
import platform
from dataclasses import asdict
from pathlib import Path
from typing import Any

import chess

from a0.search import Search, SearchConfig
from chesslab.openings import catalog
from chesslab.registry import ROOT, EngineSpec, freeze


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, default=10_000)
    parser.add_argument("--openings", nargs="+", default=["italian", "french", "qgd"])
    parser.add_argument("--output", type=Path, default=Path(".chesslab/a0-profile.json"))
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--no-pvs", action="store_true")
    parser.add_argument("--no-tt", action="store_true")
    parser.add_argument("--no-quiescence", action="store_true")
    args = parser.parse_args()
    if args.nodes <= 0:
        parser.error("--nodes must be positive")
    openings = {opening.id: opening for opening in catalog() if opening.split == "dev"}
    if unknown := set(args.openings) - openings.keys():
        parser.error(f"Unknown development openings: {sorted(unknown)}")
    config = SearchConfig(
        max_nodes=args.nodes,
        pvs=not args.no_pvs,
        use_tt=not args.no_tt,
        quiescence=not args.no_quiescence,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.profile:
        args.profile.parent.mkdir(parents=True, exist_ok=True)
    # Keep a copy of the exact measured source beside the report.
    build = freeze(
        EngineSpec("a0-profile", "A0 profile", "Original A0 alpha-beta"),
        args.output.parent / (args.output.stem + "-build"),
    )
    profiler = cProfile.Profile() if args.profile else None
    rows: list[dict[str, Any]] = []
    if profiler:
        profiler.enable()
    try:
        for key in args.openings:
            opening = openings[key]
            result = Search(config).analyse(chess.Board(opening.fen), 3_600_000, 3_600_000)
            row = asdict(result)
            row["move"] = result.move.uci()
            row.update(
                opening=opening.data(),
                nodes_per_second=result.nodes * 1000 / max(0.001, result.elapsed_ms),
            )
            rows.append(row)
    finally:
        if profiler:
            profiler.disable()
            profiler.dump_stats(str(args.profile))
    report = {
        "config": asdict(config),
        "searches": rows,
        "build": build,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "chess": chess.__version__,
        "instrumented": profiler is not None,
        "workspace": str(ROOT),
        "note": "Fresh search per opening; node cap counts quiescence nodes. Timing is local "
        "and includes profiling overhead when enabled. This is not a strength test.",
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "searches": [
                    {key: row[key] for key in ("move", "depth", "nodes", "elapsed_ms", "tt_hits")}
                    for row in rows
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
