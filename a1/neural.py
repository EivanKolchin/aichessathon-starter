"""Original, small positional residual network. No external chess weights or engine code.

768 piece-square inputs -> clipped-ReLU hidden layer -> one bounded residual. Inputs use
the side-to-move's perspective, so colour swapping and rank mirroring preserve the score.
This first implementation refreshes sparse inputs at each leaf; it is not incremental NNUE.
"""

import math
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from zipfile import ZipFile

import chess
import numpy as np
from numpy.typing import NDArray

from a1.board import TURN, IntArray
from a1.evaluation import evaluate
from a1.jit import compiled
from a1.search import Evaluator

type FloatArray = NDArray[np.float64]
INPUTS = 768
RESIDUAL_CP = 600.0
SCORE_SCALE = 400.0


def features(board: chess.Board) -> FloatArray:
    result = np.zeros(INPUTS, dtype=np.float64)
    for square, piece in board.piece_map().items():
        relative_square = square if board.turn else square ^ 56
        kind = piece.piece_type - 1 + (0 if piece.color == board.turn else 6)
        result[kind * 64 + relative_square] = 1.0
    return result


@dataclass(frozen=True)
class Weights:
    w1: FloatArray
    b1: FloatArray
    w2: FloatArray
    b2: FloatArray

    def validate(self) -> None:
        hidden = self.b1.size
        if not 1 <= hidden <= 128 or (
            self.w1.shape != (INPUTS, hidden)
            or self.b1.shape != (hidden,)
            or self.w2.shape != (hidden,)
            or self.b2.shape != (1,)
        ):
            raise ValueError("Invalid positional network shape")
        for array in (self.w1, self.b1, self.w2, self.b2):
            if array.dtype != np.float64 or not np.isfinite(array).all():
                raise ValueError("Weights must be finite float64 arrays")
            if np.max(np.abs(array)) > 10000:
                raise ValueError("Weights exceed supported range")

    def save(self, path: Path) -> None:
        self.validate()
        with path.open("xb") as stream:
            np.savez_compressed(stream, w1=self.w1, b1=self.b1, w2=self.w2, b2=self.b2)


def load_weights(path: Path) -> Weights:
    # Source-controlled checkpoint assets are data, never pickle or executable objects.
    with ZipFile(path) as archive:
        if sum(item.file_size for item in archive.infolist()) > 2_000_000:
            raise ValueError("Model exceeds this architecture's size limit")
    with np.load(path, allow_pickle=False) as data:
        if set(data.files) != {"w1", "b1", "w2", "b2"}:
            raise ValueError("Unknown positional model format")
        weights = Weights(*(np.ascontiguousarray(data[key]) for key in ("w1", "b1", "w2", "b2")))
    weights.validate()
    return weights


def predict(weights: Weights, inputs: FloatArray, baseline: FloatArray) -> FloatArray:
    hidden = np.clip(inputs @ weights.w1 + weights.b1, 0.0, 1.0)
    return cast(FloatArray, baseline + RESIDUAL_CP * np.tanh(hidden @ weights.w2 + weights.b2[0]))


def create_evaluator(weights: Weights, blend: float = 1.0) -> Evaluator:
    weights.validate()
    if not math.isfinite(blend) or not 0 <= blend <= 1:
        raise ValueError("Neural blend must be between 0 and 1")
    if blend == 0:
        return evaluate
    # Each compiled evaluator owns immutable copies. A search never sees training mutations.
    w1, b1, w2, b2 = (value.copy() for value in (weights.w1, weights.b1, weights.w2, weights.b2))
    for array in (w1, b1, w2, b2):
        array.flags.writeable = False

    @compiled
    def hybrid(board: IntArray, state: IntArray) -> int:
        hidden = b1.copy()
        for square in range(120):
            if square & 0x88 or not board[square]:
                continue
            piece = board[square]
            relative_square = (square // 16) * 8 + square % 16
            if state[TURN] == -1:
                relative_square ^= 56
            kind = abs(piece) - 1 + (0 if piece * state[TURN] > 0 else 6)
            feature = kind * 64 + relative_square
            for neuron in range(len(b1)):
                hidden[neuron] += w1[feature, neuron]
        raw = b2[0]
        for neuron in range(len(b1)):
            raw += min(1.0, max(0.0, hidden[neuron])) * w2[neuron]
        value = evaluate(board, state) + blend * RESIDUAL_CP * math.tanh(raw)
        # Learned static scores must never masquerade as a search-proven mate.
        return round(min(12000.0, max(-12000.0, value)))

    return hybrid
