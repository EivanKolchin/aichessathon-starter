"""The existing A0 evaluation formulas on the compiled mailbox representation."""

import numpy as np

from a1.board import BK, EG, MG, PHASE, TURN, WK, IntArray
from a1.jit import compiled


@compiled
def evaluate(board: IntArray, state: IntArray) -> int:
    files = np.zeros((2, 8), dtype=np.int64)
    bishops = np.zeros(2, dtype=np.int64)
    for square in range(120):
        if square & 0x88:
            continue
        piece = board[square]
        if abs(piece) == 1:
            files[0 if piece > 0 else 1, square % 16] += 1
        elif abs(piece) == 3:
            bishops[0 if piece > 0 else 1] += 1
    mg, eg = state[MG], state[EG]
    for color in range(2):
        side = 1 if color == 0 else -1
        if bishops[color] >= 2:
            mg += side * 24
            eg += side * 36
        for file in range(8):
            doubled = max(0, files[color, file] - 1)
            mg -= side * 13 * doubled
            eg -= side * 19 * doubled
        king = state[WK if color == 0 else BK]
        rank = king // 16 + side
        if 0 <= rank < 8:
            for file in range(max(0, king % 16 - 1), min(8, king % 16 + 2)):
                if board[rank * 16 + file] == side:
                    mg += side * 12
    for square in range(120):
        if square & 0x88:
            continue
        piece = board[square]
        if abs(piece) not in (1, 4):
            continue
        side, color = (1, 0) if piece > 0 else (-1, 1)
        file = square % 16
        if abs(piece) == 4:
            if not files[color, file]:
                mg += side * (20 if not files[1 - color, file] else 11)
                eg += side * 7
            continue
        rank = square // 16 if side == 1 else 7 - square // 16
        neighbours = (files[color, file - 1] if file > 0 else 0) + (
            files[color, file + 1] if file < 7 else 0
        )
        if not neighbours:
            mg -= side * 12
            eg -= side * 9
        passed = True
        for adjacent in range(max(0, file - 1), min(8, file + 2)):
            target = (square // 16 + side) * 16 + adjacent
            while not target & 0x88:
                if board[target] == -side:
                    passed = False
                    break
                target += side * 16
        if passed:
            mg += side * (rank * rank + 3)
            eg += side * (4 * rank * rank + 5)
        supported = False
        for delta in (-1, 1):
            target = square - side * 16 + delta
            if not target & 0x88 and board[target] == side:
                supported = True
        if supported:
            mg += side * 7
            eg += side * 10
    phase = min(24, state[PHASE])
    numerator = mg * phase + eg * (24 - phase)
    score = numerator // 24 if numerator >= 0 else -((-numerator) // 24)
    return int(state[TURN] * score + 8)
