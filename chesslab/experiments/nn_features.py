"""Sparse form of the a1.neural feature map, for bulk offline dataset work.

`a1.neural.features` materialises a dense 768-wide float64 vector per position, which is the
right shape for the small pilot trainer and far too slow for tens of millions of rows. This
module emits the same map as a sorted array of set-feature indices. `verify_against_dense`
asserts the two agree, so training inputs cannot drift from what the compiled evaluator reads.
"""

from typing import Final

import chess
import numpy as np
from numpy.typing import NDArray

MAX_PIECES: Final = 32
type IndexArray = NDArray[np.int16]


def sparse_features(board: chess.Board) -> list[int]:
    """The indices set by `a1.neural.features(board)`, in ascending order."""
    turn = board.turn
    result = []
    for square, piece in board.piece_map().items():
        relative_square = square if turn else square ^ 56
        kind = piece.piece_type - 1 + (0 if piece.color == turn else 6)
        result.append(kind * 64 + relative_square)
    result.sort()
    return result


def verify_against_dense(boards: list[chess.Board]) -> int:
    """Raise unless the sparse map reproduces the dense one exactly. Returns positions checked.

    Imported lazily: `a1.neural` pulls in the compiled search, which costs far more to import
    than the bulk dataset workers can afford to pay once each.
    """
    from a1.neural import INPUTS, features

    for board in boards:
        dense = features(board)
        expected = np.flatnonzero(dense).tolist()
        if dense.shape != (INPUTS,) or not np.array_equal(dense[expected], np.ones(len(expected))):
            raise ValueError("Dense feature map is not a 0/1 indicator vector")
        if sparse_features(board) != expected:
            raise ValueError(f"Sparse features drifted from a1.neural.features on {board.fen()}")
    return len(boards)
