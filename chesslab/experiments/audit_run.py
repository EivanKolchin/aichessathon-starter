"""Audit a Chess Lab export as data, without executing its engine configuration."""

import argparse
import io
import json
import re
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

import chess
import chess.pgn

from chesslab.registry import file_hash, write_json
from chesslab.statistics import candidate_failed, summarise
from harness.referee import RESULT_HEADERS


def verify_game(detail: dict[str, Any]) -> chess.Board:
    """Reconstruct accepted moves, checking PGN, frames and terminal board evidence."""
    game = chess.pgn.read_game(io.StringIO(detail["pgn"]))
    if game is None or game.errors:
        raise ValueError("Invalid or missing PGN")
    board = chess.Board(detail["opening"]["fen"])
    frames = detail["frames"]
    moves = list(game.mainline_moves())
    if len(frames) != len(moves) + 1 or detail["plies"] != len(moves):
        raise ValueError("PGN/frame/ply counts disagree")
    if game.board().fen() != board.fen() or frames[0]["fen"] != board.fen():
        raise ValueError("Opening positions disagree")
    if game.headers["Result"] != RESULT_HEADERS[detail["result"]]:
        raise ValueError("PGN result disagrees with game record")
    if game.headers.get("Termination") != detail["termination"]:
        raise ValueError("PGN termination disagrees with game record")
    for move, frame in zip(moves, frames[1:], strict=True):
        if move not in board.legal_moves:
            raise ValueError("Illegal recorded move")
        if frame["uci"] != move.uci() or frame["san"] != board.san(move):
            raise ValueError("PGN move disagrees with frame")
        board.push(move)
        if board.fen() != frame["fen"]:
            raise ValueError("PGN position disagrees with frame")
    if detail["termination"] == "checkmate":
        winner = "black" if board.turn else "white"
        if not board.is_checkmate() or detail["result"] != winner:
            raise ValueError("Checkmate result disagrees with board")
    if detail["termination"] == "threefold_repetition" and not board.is_repetition(3):
        raise ValueError("Threefold repetition is not present in recorded history")
    if (
        detail["termination"] == "flag"
        and detail["result"] == "draw"
        and not board.has_insufficient_material(not board.turn)
    ):
        raise ValueError("Drawn flag disagrees with this referee's material rule")
    return board


def audit(directory: Path) -> dict[str, Any]:
    manifest_path = directory / "manifest.json"
    run = json.loads(manifest_path.read_text(encoding="utf-8"))
    candidate = run["candidate"]
    details = []
    sources = {"manifest.json": file_hash(manifest_path)}
    nonwins = []
    telemetry: dict[str, list[dict[str, Any]]] = {}
    versions: Counter[str] = Counter()
    seen: set[str] = set()
    for entry in run["games"]:
        if entry["id"] in seen:
            raise ValueError("Duplicate game id")
        seen.add(entry["id"])
        if entry["status"] != "completed":
            continue
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", entry["id"]):
            raise ValueError("Invalid game id")
        relative = f"games/{entry['id']}.json"
        path = directory / relative
        detail = json.loads(path.read_text(encoding="utf-8"))
        for field in (
            "id",
            "pair_id",
            "white",
            "black",
            "opening",
            "result",
            "termination",
            "plies",
        ):
            if detail[field] != entry[field]:
                raise ValueError(f"{entry['id']}: manifest and detail disagree on {field}")
        board = verify_game(detail)
        sources[relative] = file_hash(path)
        details.append(detail)
        colour = "white" if entry["white"] == candidate else "black"
        logs = []
        for line in detail.get("logs", {}).get(colour, "").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # The referee retains only the beginning and end of long logs.
            if isinstance(row, dict) and all(
                isinstance(row.get(key), (float, int))
                for key in ("nodes", "qnodes", "depth", "elapsed_ms")
            ):
                logs.append(row)
                versions[str(row.get("engine", "unknown"))] += 1
        telemetry.setdefault(entry["opponent"], []).extend(logs)
        if entry["result"] != colour:
            mover = "white" if board.turn else "black"
            nonwins.append(
                {
                    "game": entry["id"],
                    "opponent": entry["opponent"],
                    "opening": entry["opening"]["id"],
                    "candidate_colour": colour,
                    "result": entry["result"],
                    "termination": entry["termination"],
                    "candidate_failed": candidate_failed(entry, candidate),
                    "plies": entry["plies"],
                    "final_fen": board.fen(),
                    "side_to_move": mover,
                    "remaining_before_rejected_move_ms": detail["frames"][-1][f"{mover}_ms"]
                    if entry["termination"] == "flag"
                    else None,
                    "last_retained_search": logs[-1] if logs else None,
                    "material": {
                        side: {
                            symbol: len(board.pieces(piece, turn))
                            for piece, symbol in enumerate(chess.PIECE_SYMBOLS[1:], 1)
                        }
                        for side, turn in (("white", chess.WHITE), ("black", chess.BLACK))
                    },
                }
            )
    combined_pgn = directory / "games.pgn"
    if combined_pgn.exists():
        if combined_pgn.read_text(encoding="utf-8").strip() != "\n\n".join(
            detail["pgn"] for detail in details
        ).strip():
            raise ValueError("Combined PGN disagrees with individual game records")
        sources["games.pgn"] = file_hash(combined_pgn)
    return {
        "schema": 1,
        "source_directory": str(directory.resolve()),
        "run_id": run["id"],
        "status": run["status"],
        "limits": run["limits"],
        "scheduled": len(run["games"]),
        "statuses": dict(Counter(g["status"] for g in run["games"])),
        "verified_games": len(details),
        "verified_accepted_plies": sum(g["plies"] for g in details),
        "combined_pgn_verified": combined_pgn.exists(),
        "source_hashes": sources,
        "recorded_candidate_build": run["engines"][candidate],
        "logged_versions": dict(versions),
        "original_summary": run["summary"],
        "corrected_summary": summarise(run["games"], candidate),
        "nonwins": nonwins,
        "retained_telemetry": {
            opponent: {
                "rows": len(rows),
                "depth_counts": dict(Counter(row["depth"] for row in rows)),
                "median_depth": statistics.median(row["depth"] for row in rows),
                "quiescence_node_fraction": sum(row["qnodes"] for row in rows)
                / max(1, sum(row["nodes"] for row in rows)),
            }
            for opponent, rows in telemetry.items()
            if rows
        },
        "limitations": [
            "Interrupted or queued games are unscored; this is not a complete league.",
            "Log telemetry is truncated and is not an unbiased sample of all moves.",
            "Replay verifies recorded legal moves/results, not execution or wall-clock accuracy.",
            "Build hashes identify the declared build; no engine paths or commands were executed.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise SystemExit("Choose a new output file; audits are retained as evidence")
    report = audit(args.directory)
    write_json(args.out, report)
    print(
        json.dumps({key: report[key] for key in ("verified_games", "corrected_summary")}, indent=2)
    )


if __name__ == "__main__":
    main()
