"""Measure a trained residual model against the identical search with classical evaluation."""

import argparse
import json
import statistics
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import chess

from a1.board import IntArray, from_board
from a1.evaluation import evaluate
from a1.jit import compiled
from a1.neural import create_evaluator, load_weights
from a1.search import Evaluator, Search, SearchConfig
from chesslab.registry import file_hash, write_json


@compiled
def evaluations(board: IntArray, state: IntArray, count: int, evaluator: Evaluator) -> int:
    total = 0
    for _ in range(count):
        total += evaluator(board, state)
    return total


def benchmark(model: Path, dataset: Path, output: Path, milliseconds: int) -> dict[str, Any]:
    if output.exists() or milliseconds < 1:
        raise ValueError("Choose a new output file and positive time budget")
    started = time.perf_counter()
    hybrid = create_evaluator(load_weights(model))
    cases = json.loads(dataset.read_text(encoding="utf-8"))["positions"]
    selected = []
    for split in ("train", "validation", "test"):
        selected.extend(
            [
                row
                for row in cases
                if row["split"] == split and row["teacher"]["score_cp"] is not None
            ][:4]
        )
    evaluators = {"classical": evaluate, "neural": hybrid}
    for evaluator in evaluators.values():
        Search(SearchConfig(max_depth=2), evaluator).analyse(chess.Board(), 60000, 60000)
        evaluations(*from_board(chess.Board()), 1, evaluator)
    warmup_seconds = time.perf_counter() - started
    rows = []
    for index, case in enumerate(selected):
        reference = chess.Board(case["fen"])
        board, state = from_board(reference)
        row: dict[str, Any] = {"id": case["id"], "split": case["split"], "fen": case["fen"]}
        # Alternate ordering and always use fresh caches. Nominal depth is descriptive only.
        for name in ("classical", "neural") if index % 2 == 0 else ("neural", "classical"):
            evaluator = evaluators[name]
            times = []
            for _ in range(3):
                tick = time.perf_counter()
                checksum = evaluations(board, state, 10000, evaluator)
                times.append((time.perf_counter() - tick) * 1e6 / 10000)
            result = Search(evaluator=evaluator).analyse(reference, milliseconds, milliseconds)
            row[name] = {
                **asdict(result),
                "move": result.move.uci(),
                "eval_us": statistics.median(times),
                "checksum": checksum,
            }
        rows.append(row)
        print(f"Benchmarked {index + 1}/{len(selected)}", flush=True)
    report = {
        "schema": 1,
        "model_sha256": file_hash(model),
        "dataset_sha256": file_hash(dataset),
        "search_source_sha256": file_hash(Path(__file__).parents[2] / "a1" / "search.py"),
        "warmup_seconds": warmup_seconds,
        "search_ms": milliseconds,
        "rows": rows,
        "median_eval_cost_ratio": statistics.median(
            row["neural"]["eval_us"] / row["classical"]["eval_us"] for row in rows
        ),
        "median_search_nodes_ratio": statistics.median(
            row["neural"]["nodes"] / max(1, row["classical"]["nodes"]) for row in rows
        ),
        "limitations": "Same clock/search options, different leaf scores and explored trees."
        " Node ratios and nominal depth do not measure strength.",
    }
    write_json(output, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--ms", type=int, default=500)
    args = parser.parse_args()
    report = benchmark(args.model, args.dataset, args.out, args.ms)
    print(json.dumps({key: value for key, value in report.items() if key != "rows"}, indent=2))


if __name__ == "__main__":
    main()
