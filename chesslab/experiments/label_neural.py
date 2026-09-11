"""Label our recorded games offline with an explicitly selected local UCI teacher.

Game exports are read as data; their engine configurations are never executed. This pilot
uses only public development families, leaving the lab's validation opening set untouched.
"""

import argparse
import io
import json
import random
import re
from collections import Counter
from pathlib import Path
from typing import Any

import chess.engine
import chess.pgn

from a1.neural import features
from chesslab.experiments.audit_run import verify_game
from chesslab.experiments.position_probe import board_for, query
from chesslab.registry import file_hash, write_json

SPLITS = {
    "Open games": "train",
    "Sicilian": "train",
    "French": "train",
    "Queen's gambit": "validation",
    "Flank openings": "test",
}


def collect(directories: list[Path], seed: int, per_game: int = 24) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    rows = []
    for directory in directories:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        for entry in manifest["games"]:
            if entry["status"] != "completed":
                continue
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", entry["id"]):
                raise ValueError("Invalid game id")
            path = directory / "games" / f"{entry['id']}.json"
            detail = json.loads(path.read_text(encoding="utf-8"))
            verify_game(detail)
            family = detail["opening"]["family"]
            if family not in SPLITS:
                continue
            game = chess.pgn.read_game(io.StringIO(detail["pgn"]))
            assert game is not None
            board = game.board()
            played: list[str] = []
            candidates = []
            for ply, move in enumerate(game.mainline_moves()):
                # Sparse sampling, both colours, and no terminal/mate/in-check leaf labels.
                if (
                    ply >= 8
                    and ply % 5 == 3
                    and len(board.piece_map()) >= 8
                    and not board.is_check()
                    and not board.is_game_over(claim_draw=True)
                ):
                    candidates.append(
                        {
                            "id": f"{directory.name}-{entry['id']}-p{ply}",
                            "game": f"{directory.name}/{entry['id']}",
                            "ply": ply,
                            "opening_group": family,
                            "opening": detail["opening"]["id"],
                            "split": SPLITS[family],
                            "fen": board.fen(),
                            "start_fen": game.board().fen(),
                            "moves": played.copy(),
                            "source_sha256": file_hash(path),
                            "source": str(path.resolve()),
                        }
                    )
                board.push(move)
                played.append(move.uci())
            rng.shuffle(candidates)
            rows.extend(candidates[:per_game])
    rng.shuffle(rows)
    # Same neural input (including colour mirrors) cannot leak across splits or dominate one.
    seen: set[bytes] = set()
    unique = []
    for row in rows:
        key = features(chess.Board(row["fen"])).tobytes()
        if key not in seen:
            unique.append(row)
            seen.add(key)
    return unique


def label(
    directories: list[Path],
    executable: Path,
    output: Path,
    nodes: int,
    max_train: int,
    max_holdout: int,
    seed: int,
) -> dict[str, Any]:
    if output.exists():
        raise ValueError("Choose a new dataset directory")
    if min(nodes, max_train, max_holdout) < 1:
        raise ValueError("Budgets must be positive")
    executable = executable.resolve(strict=True)
    pool = collect(directories, seed)
    chosen = []
    for split, limit in (("train", max_train), ("validation", max_holdout), ("test", max_holdout)):
        chosen.extend([row for row in pool if row["split"] == split][:limit])
    if {row["split"] for row in chosen} != {"train", "validation", "test"}:
        raise ValueError("Need games from training, Queen's gambit and Flank opening families")
    output.mkdir(parents=True)
    with chess.engine.SimpleEngine.popen_uci([str(executable)], timeout=20) as teacher:
        teacher.configure({"Threads": 1, "Hash": 32})
        teacher_id = dict(teacher.id)
        with (output / "progress.jsonl").open("x", encoding="utf-8") as progress:
            for index, row in enumerate(chosen, 1):
                row["teacher"] = query(teacher, board_for(row), nodes)
                progress.write(json.dumps(row) + "\n")
                progress.flush()
                if index % 50 == 0 or index == len(chosen):
                    print(f"Labelled {index}/{len(chosen)}", flush=True)
    report = {
        "schema": 1,
        "seed": seed,
        "split_by": "whole development opening family",
        "teacher": {
            "path": str(executable),
            "sha256": file_hash(executable),
            "id": teacher_id,
            "options": {"Threads": 1, "Hash": 32},
            "nodes_per_position": nodes,
            "fresh_game_per_query": True,
        },
        "sources": [str(path.resolve()) for path in directories],
        "counts": dict(Counter(row["split"] for row in chosen)),
        "positions": chosen,
        "limitations": "Search scores are finite-budget teacher labels, not ground truth."
        " Test families are development data, not a private final benchmark.",
        "source_sha256": file_hash(Path(__file__)),
    }
    write_json(output / "dataset.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--nodes", type=int, default=10000)
    parser.add_argument("--max-train", type=int, default=600)
    parser.add_argument("--max-holdout", type=int, default=150)
    parser.add_argument("--seed", type=int, default=20260911)
    args = parser.parse_args()
    result = label(
        args.runs, args.engine, args.out, args.nodes, args.max_train, args.max_holdout, args.seed
    )
    print(json.dumps(result["counts"]))


if __name__ == "__main__":
    main()
