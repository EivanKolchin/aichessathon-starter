"""Build a large training set from a locally stored Lichess evaluation export.

The export is CC0 Stockfish analysis of user-submitted positions, read here as data. Its FENs
carry only four fields, so there is no move counter to filter opening plies with; ``--max-home``
stands in for that, bounding how many men may still sit on their game-start squares.

Scores in the export are relative to White. Every A1 score is relative to the side to move, so
``stm_score`` negates them for Black. ``verify_perspective`` asserts that on known positions and
runs before any extraction starts.
"""

import argparse
import hashlib
import json
import multiprocessing as mp
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import chess
import numpy as np
import zstandard
from numpy.typing import NDArray

from chesslab.experiments.nn_features import MAX_PIECES, sparse_features
from chesslab.registry import write_json

CHUNK_BYTES: Final = 1 << 22
MAX_ABS_CP: Final = 2000
START_MEN: Final[dict[int, chess.Piece]] = dict(chess.Board().piece_map())
# One fixed random word per feature; the key is order-independent over a position's men.
FEATURE_KEYS: Final[NDArray[np.uint64]] = np.frombuffer(
    hashlib.shake_128(b"a1-nn-canonical-key").digest(768 * 8), dtype=np.uint64
)

_MAX_HOME: int = 24


def stm_score(white_relative: int, board: chess.Board) -> int:
    """Convert a White-relative centipawn score to the side to move's own point of view."""
    return white_relative if board.turn else -white_relative


def best_line(evals: list[dict[str, Any]], white_to_move: bool) -> tuple[int, str] | None:
    """The deepest evaluation's best line, as a White-relative centipawn score.

    Returns None when the deepest evaluation reports a forced mate anywhere, or when its score
    leaves the trusted centipawn band.
    """
    entry = max(evals, key=lambda item: (item.get("depth", 0), item.get("knodes", 0)))
    pvs = entry.get("pvs") or []
    if not pvs or any("cp" not in pv or "mate" in pv for pv in pvs):
        return None
    # Export order puts the best line first, but the comparison is explicit rather than assumed.
    choose = max if white_to_move else min
    best = choose(pvs, key=lambda pv: int(pv["cp"]))
    score = int(best["cp"])
    line = str(best.get("line", "")).split(" ", 1)[0]
    if abs(score) > MAX_ABS_CP or len(line) not in (4, 5):
        return None
    return score, line


def home_men(board: chess.Board) -> int:
    """How many men still stand on the square they start a game on."""
    men = board.piece_map()
    return sum(1 for square, piece in START_MEN.items() if men.get(square) == piece)


def canonical_key(sparse: list[int]) -> int:
    """An order-independent 64-bit key over exactly the features the evaluator reads."""
    return int(np.bitwise_xor.reduce(FEATURE_KEYS[sparse]))


def extract(payload: bytes, max_home: int) -> tuple[NDArray[Any], ...]:
    """Filter one block of export lines into packed per-position arrays."""
    indices: list[list[int]] = []
    scores: list[int] = []
    keys: list[int] = []
    seen = 0
    for raw in payload.split(b"\n"):
        if not raw:
            continue
        seen += 1
        record = json.loads(raw)
        fen = str(record["fen"])
        found = best_line(record["evals"], " w " in fen)
        if found is None:
            continue
        white_relative, uci = found
        board = chess.Board(fen)
        # Stockfish's own smart_fen_skipping: quiet, legal, out-of-check positions only.
        if board.is_check() or len(uci) == 5 or home_men(board) > max_home:
            continue
        move = chess.Move.from_uci(uci)
        if board.is_capture(move) or not board.is_legal(move):
            continue
        sparse = sparse_features(board)
        if not 2 <= len(sparse) <= MAX_PIECES:
            continue
        indices.append(sparse)
        scores.append(stm_score(white_relative, board))
        keys.append(canonical_key(sparse))
    packed = np.full((len(indices), MAX_PIECES), -1, dtype=np.int16)
    for row, sparse in enumerate(indices):
        packed[row, : len(sparse)] = sparse
    return (
        packed,
        np.array(scores, dtype=np.int16),
        np.array(keys, dtype=np.uint64),
        np.array([seen, len(indices)], dtype=np.int64),
    )


def _initialise(max_home: int) -> None:
    """Configure a worker. Workers deliberately import neither numba nor a1: paying the
    compiled-search import once per worker dominated the whole extraction. The classical
    baseline every label needs is added afterwards, in one pass, by nn_baseline."""
    global _MAX_HOME
    _MAX_HOME = max_home


def _work(payload: bytes) -> tuple[NDArray[Any], ...]:
    return extract(payload, _MAX_HOME)


def blocks(path: Path, max_bytes: int) -> Iterator[bytes]:
    """Whole decompressed lines, in blocks, from a possibly truncated export."""
    remainder = b""
    produced = 0
    with path.open("rb") as handle:
        reader = zstandard.ZstdDecompressor().stream_reader(handle)
        while produced < max_bytes:
            try:
                chunk = reader.read(CHUNK_BYTES)
            except zstandard.ZstdError:
                break  # A range-limited download ends mid-frame; keep whatever decoded.
            if not chunk:
                break
            remainder += chunk
            cut = remainder.rfind(b"\n")
            if cut < 0:
                continue
            payload, remainder = remainder[:cut], remainder[cut + 1 :]
            produced += len(payload)
            yield payload


def verify_perspective() -> None:
    """Assert the White-relative convention and its negation, on positions with a known winner."""
    # Black has just lost a queen for nothing; a White-relative export would score this near +900.
    losing_for_black = chess.Board("rnb1kbnr/pppp1ppp/8/4p3/6Q1/8/PPPP1PPP/RNB1KBNR b KQkq -")
    if losing_for_black.turn:
        raise ValueError("Fixture must have Black to move")
    if stm_score(900, losing_for_black) != -900:
        raise ValueError("A White-relative score must be negated for Black to move")
    winning_for_white = chess.Board("rnb1kbnr/pppp1ppp/8/4p3/6Q1/8/PPPP1PPP/RNB1KBNR w KQkq -")
    if stm_score(900, winning_for_white) != 900:
        raise ValueError("A White-relative score must be kept for White to move")
    if sparse_features(losing_for_black) == sparse_features(winning_for_white):
        raise ValueError("Features must distinguish the side to move")
    record = {
        "fen": losing_for_black.fen(),
        "evals": [{"depth": 30, "knodes": 1, "pvs": [{"cp": 900, "line": "e8e7 g4g7"}]}],
    }
    found = best_line(record["evals"], False)
    if found is None or stm_score(found[0], losing_for_black) != -900:
        raise ValueError("End-to-end label must be negative for the side that is losing")


def build(
    source: Path, output: Path, max_bytes: int, max_home: int, workers: int
) -> dict[str, Any]:
    if output.exists():
        raise ValueError("Choose a new dataset directory")
    verify_perspective()
    output.mkdir(parents=True)
    started = time.perf_counter()
    totals = np.zeros(2, dtype=np.int64)
    names = ("idx", "score", "key")
    streams = {name: (output / f"{name}.bin").open("xb") for name in names}
    reported = 0
    with mp.get_context("spawn").Pool(workers, _initialise, (max_home,)) as pool:
        for *arrays, counts in pool.imap(_work, blocks(source, max_bytes), 4):
            totals += counts
            for name, array in zip(names, arrays, strict=True):
                streams[name].write(array.tobytes())
            if totals[0] - reported >= 2_000_000:
                reported = int(totals[0])
                rate = totals[0] / max(1e-9, time.perf_counter() - started)
                print(f"read {totals[0]:,} kept {totals[1]:,} ({rate:,.0f}/s)", flush=True)
    for stream in streams.values():
        stream.close()
    report = {
        "schema": 1,
        "source": str(source.resolve()),
        "source_bytes_compressed": source.stat().st_size,
        "positions_read": int(totals[0]),
        "positions_kept": int(totals[1]),
        "seconds": time.perf_counter() - started,
        "score_perspective": "side to move; the export is White-relative and is negated for Black",
        "filters": {
            "deepest_evaluation_only": True,
            "drop_mate_scores": True,
            "max_abs_cp": MAX_ABS_CP,
            "drop_in_check": True,
            "drop_capture_or_promotion_best_move": True,
            "max_men_on_start_squares": max_home,
        },
        "limitations": "Export FENs carry no move counters, so the conventional 16-ply opening"
        " cut is approximated by a bound on men still standing on their game-start squares."
        " Teacher scores are finite-depth search output, not ground truth.",
    }
    write_json(output / "extract.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-bytes", type=int, default=1 << 62)
    parser.add_argument("--max-home", type=int, default=24)
    parser.add_argument("--workers", type=int, default=max(1, mp.cpu_count() - 2))
    args = parser.parse_args()
    report = build(args.source, args.out, args.max_bytes, args.max_home, args.workers)
    print(json.dumps({key: value for key, value in report.items() if key != "filters"}, indent=2))


if __name__ == "__main__":
    main()
