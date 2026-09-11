"""Add the classical baseline that every residual label is measured against.

`a1.evaluation.evaluate` reads only the men on the board and the side to move: no castling
rights, no en passant file, no clocks. It is also written from the side to move's point of
view. The 768-feature map encodes exactly that much, with the side to move's men mirrored onto
White's half of the numbering, so the baseline can be rebuilt from the features alone and the
whole dataset labelled in one compiled pass instead of one interpreted call per position.

`verify_rebuild` checks that claim against `from_board`/`evaluate` on real positions and is
run before any baseline is written. Nothing here is imported by `a1` or by `agent.py`.
"""

import argparse
import json
import random
import time
from pathlib import Path
from typing import Any

import chess
import numba
import numpy as np
from numpy.typing import NDArray

from a1.board import BK, META_SIZE, TURN, WK, from_board, replace
from a1.evaluation import evaluate
from chesslab.experiments.nn_features import MAX_PIECES, sparse_features
from chesslab.registry import write_json

type I16 = NDArray[np.int16]


@numba.njit(cache=True)
def rebuild(features: I16, board: NDArray[np.int64], state: NDArray[np.int64]) -> None:
    """Place one position's men as if the side to move were White, and derive its state."""
    board[:] = 0
    state[:] = 0
    state[TURN] = 1
    for slot in range(features.shape[0]):
        feature = features[slot]
        if feature < 0:
            break
        kind = feature // 64
        square = feature % 64
        piece_type = kind % 6 + 1
        piece = piece_type if kind < 6 else -piece_type
        cell = (square // 8) * 16 + square % 8
        replace(board, state, cell, piece)
        if piece_type == 6:
            state[WK if kind < 6 else BK] = cell


@numba.njit(cache=True, parallel=True)
def baselines(idx: I16, out: I16) -> None:
    for row in numba.prange(idx.shape[0]):
        board = np.zeros(128, dtype=np.int64)
        state = np.zeros(META_SIZE, dtype=np.int64)
        rebuild(idx[row], board, state)
        out[row] = evaluate(board, state)


def verify_rebuild(count: int = 600, seed: int = 20260911) -> int:
    """Raise unless the feature rebuild reproduces the real classical score exactly."""
    rng = random.Random(seed)
    boards = []
    while len(boards) < count:
        board = chess.Board()
        for _ in range(rng.randint(2, 80)):
            moves = list(board.legal_moves)
            if not moves:
                break
            board.push(rng.choice(moves))
        if not board.is_game_over() and len(board.piece_map()) >= 2:
            boards.append(board.copy())
    packed = np.full((len(boards), MAX_PIECES), -1, dtype=np.int16)
    for row, board in enumerate(boards):
        sparse = sparse_features(board)
        packed[row, : len(sparse)] = sparse
    got = np.zeros(len(boards), dtype=np.int16)
    baselines(packed, got)
    for row, board in enumerate(boards):
        expected = evaluate(*from_board(board))
        if int(got[row]) != int(expected):
            raise ValueError(
                f"Feature rebuild scored {got[row]} against {expected} on {board.fen()}"
            )
    return len(boards)


def annotate(dataset: Path, batch: int = 1 << 21) -> dict[str, Any]:
    target = dataset / "base.bin"
    if target.exists():
        raise ValueError("Baseline already written for this dataset")
    checked = verify_rebuild()
    started = time.perf_counter()
    count = (dataset / "key.bin").stat().st_size // 8
    idx = np.memmap(dataset / "idx.bin", dtype=np.int16, mode="r").reshape(count, MAX_PIECES)
    if idx.shape[0] != count:
        raise ValueError("Extraction files disagree on how many positions they hold")
    with target.open("xb") as stream:
        for start in range(0, count, batch):
            chunk = np.ascontiguousarray(idx[start : start + batch])
            out = np.zeros(chunk.shape[0], dtype=np.int16)
            baselines(chunk, out)
            stream.write(out.tobytes())
            print(f"baselined {min(start + batch, count):,}/{count:,}", flush=True)
    report = {
        "schema": 1,
        "positions": int(count),
        "seconds": time.perf_counter() - started,
        "verified_against_from_board": checked,
        "claim": "a1.evaluation.evaluate is a function of the men on the board and the side to"
        " move only, so it can be rebuilt from the 768-feature map alone",
        "source_sha256_of": str(Path(__file__).name),
    }
    write_json(dataset / "baseline.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    args = parser.parse_args()
    print(json.dumps(annotate(args.dataset), indent=2))


if __name__ == "__main__":
    main()
