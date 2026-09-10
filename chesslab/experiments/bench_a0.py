"""Compare frozen A0 builds at identical node budgets with alternating timing order."""

import argparse
import json
import math
import platform
import random
import shutil
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

import chess

from chesslab.openings import catalog
from chesslab.registry import ROOT, EngineSpec, file_hash, freeze, write_json

WORKER = """
import json
import sys
import time
import chess
from a0 import __version__
from a0.search import Search, SearchConfig
request = json.load(sys.stdin)
Search(SearchConfig(max_nodes=1000)).analyse(chess.Board(), 3600000, 3600000)
board = chess.Board(request['fen'])
for move in request['moves']:
    board.push_uci(move)
search = Search(SearchConfig(max_nodes=request['nodes']))
started = time.perf_counter()
result = search.analyse(board, 3600000, 3600000)
elapsed = (time.perf_counter() - started) * 1000
print(json.dumps({
    'version':__version__, 'move':result.move.uci(), 'score':result.score,
    'depth':result.depth, 'nodes':result.nodes, 'qnodes':result.qnodes,
    'tt_hits':result.tt_hits, 'stopped':result.stopped, 'pv':result.pv,
    'elapsed_ms':elapsed
}))
"""
IDENTITY_FIELDS = ("move", "score", "depth", "nodes", "qnodes", "tt_hits", "stopped", "pv")


def fixtures() -> list[dict[str, Any]]:
    cases = [
        {"id": o.id, "fen": o.fen, "moves": [], "timing": True, "family": o.family}
        for o in catalog()
        if o.split == "dev"
    ]
    for key, fen in (
        ("white-promotion", "7k/P7/8/8/8/8/8/7K w - - 0 1"),
        ("black-promotion", "7k/8/8/8/8/8/p7/7K b - - 0 1"),
        ("capture-promotion", "r6k/1P6/8/8/8/8/8/7K w - - 0 1"),
        ("en-passant", "7k/8/8/3pP3/8/8/8/7K w - d6 0 2"),
        ("pinned-en-passant", "4r2k/8/8/3pP3/8/8/8/4K3 w - d6 0 2"),
        ("underpromotion", "8/k1P5/2K5/8/8/8/8/8 w - - 0 1"),
        ("check-evasion", "7k/7R/6K1/8/8/8/8/8 b - - 0 1"),
        ("mate", "7k/5Q2/6K1/8/8/8/8/8 w - - 0 1"),
    ):
        cases.append({"id": key, "fen": fen, "moves": [], "timing": False})
    cases.append(
        {
            "id": "repetition-history",
            "fen": chess.STARTING_FEN,
            "moves": ["g1f3", "g8f6", "f3g1", "f6g8"],
            "timing": False,
        }
    )
    return cases


def measure(build: Path, case: dict[str, Any], nodes: int) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-c", WORKER],
        cwd=build,
        input=json.dumps({**case, "nodes": nodes}),
        text=True,
        capture_output=True,
        check=True,
        timeout=60,
    )
    result: dict[str, Any] = json.loads(completed.stdout)
    return result


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    timings = []
    for case_id in sorted({row["case"] for row in rows if row["timing"]}):
        samples = [row for row in rows if row["case"] == case_id]
        before = statistics.median(row["reference"]["elapsed_ms"] for row in samples)
        after = statistics.median(row["candidate"]["elapsed_ms"] for row in samples)
        timings.append(
            {
                "case": case_id,
                "reference_median_ms": before,
                "candidate_median_ms": after,
                "speedup": before / after,
                "repeats": len(samples),
            }
        )
    return {
        "comparisons": len(rows),
        "identical": all(row["identical"] for row in rows),
        "geomean_speedup": math.exp(statistics.mean(math.log(row["speedup"]) for row in timings)),
        "per_case": timings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, default=ROOT / ".chesslab/checkpoints/a0-0.1.0")
    parser.add_argument("--candidate", type=Path, default=ROOT)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--nodes", type=int, default=5000)
    parser.add_argument("--repeats", type=int, default=4)
    args = parser.parse_args()
    if args.nodes <= 0 or args.repeats <= 0:
        parser.error("Nodes and repeats must be positive")
    directory = args.directory.resolve()
    if directory.exists():
        parser.error("Use a new directory; benchmark records are never overwritten")
    directory.mkdir(parents=True)
    builds = {
        key: freeze(
            EngineSpec(key, key, "Original A0 alpha-beta", path=str(source.resolve())),
            directory / "builds" / key,
        )
        for key, source in (("reference", args.reference), ("candidate", args.candidate))
    }
    shutil.copy2(__file__, directory / "benchmark-script.py")
    cases = fixtures()
    report: dict[str, Any] = {
        "status": "running",
        "nodes": args.nodes,
        "repeats": args.repeats,
        "builds": builds,
        "fixtures": cases,
        "rows": [],
        "python": sys.version,
        "platform": platform.platform(),
        "processor": platform.processor(),
        "chess": chess.__version__,
        "script_sha256": file_hash(Path(__file__)),
        "method": "Fresh process per trial, untimed warmup, fresh search caches, perf_counter "
        "timing. Alternating build order; deterministic shuffled case order. Special "
        "fixtures check equivalence but are excluded from aggregate timing. No Elo claim.",
    }
    try:
        for repeat in range(args.repeats):
            order = list(cases)
            random.Random(20260910 + repeat).shuffle(order)
            for index, case in enumerate(order):
                keys = (
                    ("reference", "candidate")
                    if (repeat + index) % 2 == 0
                    else ("candidate", "reference")
                )
                measured = {
                    key: measure(Path(builds[key]["frozen_path"]), case, args.nodes) for key in keys
                }
                equal = all(
                    measured["reference"][key] == measured["candidate"][key]
                    for key in IDENTITY_FIELDS
                )
                report["rows"].append(
                    {
                        "repeat": repeat,
                        "case": case["id"],
                        "timing": case["timing"],
                        "order": keys,
                        "identical": equal,
                        **measured,
                    }
                )
            write_json(directory / "report.json", report)
            print(f"Completed repeat {repeat + 1}/{args.repeats}", flush=True)
        for build in builds.values():
            for name, expected in build["files"].items():
                if file_hash(Path(build["frozen_path"]) / name) != expected:
                    raise ValueError(f"Frozen build changed: {name}")
        report["summary"] = summary(report["rows"])
        report["status"] = "completed"
    except Exception as error:
        report["status"] = "failed"
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        write_json(directory / "report.json", report)
    print(json.dumps(report["summary"], indent=2))
    if not report["summary"]["identical"]:
        raise SystemExit("Search results changed; inspect the retained comparisons")


if __name__ == "__main__":
    main()
