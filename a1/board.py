"""Original standard-chess board operations on a 0x88 mailbox.

Squares use rank * 16 + file; square & 0x88 detects the border. Positive pieces
are White, negative pieces Black, with pawn=1 through king=6. Search owns its
arrays and preallocated undo rows. python-chess is used only at the boundary.
"""

import chess
import numpy as np
from numpy.typing import NDArray

from a0.evaluation import EG_TABLE, MG_TABLE, PHASE_VALUE
from a1.jit import compiled

type IntArray = NDArray[np.int64]
TURN, RIGHTS, EP, HALF, FULL, WK, BK, MG, EG, PHASE = range(10)
PACKED = 10
META_SIZE = 15
UNDO_SIZE = META_SIZE + 3
MAX_MOVES = 512
KNIGHT_STEPS = (-33, -31, -18, -14, 14, 18, 31, 33)
KING_STEPS = (-17, -16, -15, -1, 1, 15, 16, 17)
RAYS = (-17, -15, 15, 17, -16, -1, 1, 16)
MG_VALUES = np.array(MG_TABLE, dtype=np.int64)
EG_VALUES = np.array(EG_TABLE, dtype=np.int64)
PHASE_VALUES = np.array(PHASE_VALUE, dtype=np.int64)


def encode(move: chess.Move) -> int:
    source = (move.from_square // 8) * 16 + move.from_square % 8
    target = (move.to_square // 8) * 16 + move.to_square % 8
    return source | (target << 7) | ((move.promotion or 0) << 14)


def decode(move: int) -> chess.Move:
    source, target = move & 127, (move >> 7) & 127
    return chess.Move(
        (source // 16) * 8 + source % 16,
        (target // 16) * 8 + target % 16,
        promotion=(move >> 14) or None,
    )


@compiled
def replace(board: IntArray, state: IntArray, square: int, piece: int) -> None:
    """Update a square, incremental evaluation and an exact packed board identity."""
    index = (square // 16) * 8 + square % 16
    old = board[square]
    for value, direction in ((old, -1), (piece, 1)):
        if value:
            sign = 1 if value > 0 else -1
            relative = index if sign == 1 else index ^ 56
            state[MG] += direction * sign * MG_VALUES[abs(value), relative]
            state[EG] += direction * sign * EG_VALUES[abs(value), relative]
            state[PHASE] += direction * PHASE_VALUES[abs(value)]
    # Fifteen four-bit pieces per word keep every packed word below 2**60.
    before = old if old >= 0 else 6 - old
    after = piece if piece >= 0 else 6 - piece
    state[PACKED + index // 15] ^= (before ^ after) << (4 * (index % 15))
    board[square] = piece


def from_board(reference: chess.Board) -> tuple[IntArray, IntArray]:
    if reference.chess960 or not reference.is_valid():
        raise ValueError("A1 expects a valid standard-chess position")
    board = np.zeros(128, dtype=np.int64)
    state = np.zeros(META_SIZE, dtype=np.int64)
    state[TURN] = 1 if reference.turn else -1
    state[RIGHTS] = (
        int(reference.has_kingside_castling_rights(chess.WHITE))
        | (int(reference.has_queenside_castling_rights(chess.WHITE)) << 1)
        | (int(reference.has_kingside_castling_rights(chess.BLACK)) << 2)
        | (int(reference.has_queenside_castling_rights(chess.BLACK)) << 3)
    )
    ep = reference.ep_square
    state[EP] = -1 if ep is None else (ep // 8) * 16 + ep % 8
    state[HALF], state[FULL] = reference.halfmove_clock, reference.fullmove_number
    for square, piece in reference.piece_map().items():
        cell = (square // 8) * 16 + square % 8
        replace(board, state, cell, piece.piece_type * (1 if piece.color else -1))
        if piece.piece_type == chess.KING:
            state[WK if piece.color else BK] = cell
    return board, state


@compiled
def attacked(board: IntArray, square: int, by: int) -> bool:
    for delta in (-1, 1):
        source = square - by * 16 + delta
        if not source & 0x88 and board[source] == by:
            return True
    for delta in KNIGHT_STEPS:
        source = square + delta
        if not source & 0x88 and board[source] == 2 * by:
            return True
    for delta in KING_STEPS:
        source = square + delta
        if not source & 0x88 and board[source] == 6 * by:
            return True
    for index in range(8):
        source = square + RAYS[index]
        while not source & 0x88:
            piece = board[source]
            if piece:
                if piece * by > 0 and (abs(piece) == 5 or abs(piece) == (3 if index < 4 else 4)):
                    return True
                break
            source += RAYS[index]
    return False


@compiled
def in_check(board: IntArray, state: IntArray) -> bool:
    return attacked(board, state[WK if state[TURN] == 1 else BK], -state[TURN])


@compiled
def make(board: IntArray, state: IntArray, move: int, undo: IntArray) -> None:
    source, target, promotion = move & 127, (move >> 7) & 127, move >> 14
    side, piece = state[TURN], board[source]
    capture = target
    if abs(piece) == 1 and target == state[EP] and not board[target]:
        capture -= side * 16
    for i in range(META_SIZE):
        undo[i] = state[i]
    undo[META_SIZE] = piece
    undo[META_SIZE + 1] = board[capture]
    undo[META_SIZE + 2] = capture
    replace(board, state, source, 0)
    if board[capture]:
        replace(board, state, capture, 0)
    replace(board, state, target, side * promotion if promotion else piece)
    if abs(piece) == 6:
        state[WK if side == 1 else BK] = target
        state[RIGHTS] &= 12 if side == 1 else 3
        if abs(target - source) == 2:
            rook_source = source + 3 if target > source else source - 4
            rook_target = source + 1 if target > source else source - 1
            replace(board, state, rook_source, 0)
            replace(board, state, rook_target, side * 4)
    for square, mask in ((0, 2), (7, 1), (112, 8), (119, 4)):
        if source == square or capture == square:
            state[RIGHTS] &= 15 ^ mask
    state[EP] = source + side * 16 if abs(piece) == 1 and abs(target - source) == 32 else -1
    state[HALF] = 0 if abs(piece) == 1 or undo[META_SIZE + 1] else state[HALF] + 1
    state[FULL] += int(side == -1)
    state[TURN] = -side


@compiled
def make_null(state: IntArray, undo: IntArray) -> None:
    """Pass the move. Placement does not change, so only the metadata is saved."""
    for i in range(META_SIZE):
        undo[i] = state[i]
    state[EP] = -1
    state[HALF] += 1
    state[FULL] += int(state[TURN] == -1)
    state[TURN] = -state[TURN]


@compiled
def unmake_null(state: IntArray, undo: IntArray) -> None:
    for i in range(META_SIZE):
        state[i] = undo[i]


@compiled
def has_material(board: IntArray, side: int) -> bool:
    """Whether this side has a piece other than pawns and its king.

    A null move is unsound in zugzwang, and zugzwang needs a side with nothing safe to move.
    Requiring a real piece is the standard, cheap guard against it.
    """
    for square in range(120):
        if square & 0x88:
            continue
        piece = board[square]
        if piece * side > 0 and abs(piece) != 1 and abs(piece) != 6:
            return True
    return False


@compiled
def unmake(board: IntArray, state: IntArray, move: int, undo: IntArray) -> None:
    source, target = move & 127, (move >> 7) & 127
    piece = undo[META_SIZE]
    board[source], board[target] = piece, 0
    board[undo[META_SIZE + 2]] = undo[META_SIZE + 1]
    if abs(piece) == 6 and abs(target - source) == 2:
        rook_source = source + 3 if target > source else source - 4
        rook_target = source + 1 if target > source else source - 1
        board[rook_source], board[rook_target] = undo[TURN] * 4, 0
    for i in range(META_SIZE):
        state[i] = undo[i]


@compiled
def append_move(moves: IntArray, count: int, source: int, target: int, promotion: bool) -> int:
    if count + (4 if promotion else 1) > len(moves):
        raise ValueError("Move buffer capacity exceeded")
    if promotion:
        for piece in (5, 4, 3, 2):
            moves[count] = source | (target << 7) | (piece << 14)
            count += 1
    else:
        moves[count] = source | (target << 7)
        count += 1
    return count


@compiled
def generate(board: IntArray, state: IntArray, moves: IntArray, undo: IntArray) -> int:
    """Generate pseudo-legal moves, then reject moves exposing the moving king."""
    count, side = 0, state[TURN]
    for source in range(119, -1, -1):
        if source & 0x88 or board[source] * side <= 0:
            continue
        piece = abs(board[source])
        if piece == 1:
            target = source + side * 16
            if not target & 0x88 and not board[target]:
                count = append_move(moves, count, source, target, target // 16 in (0, 7))
                second = source + side * 32
                if source // 16 == (1 if side == 1 else 6) and not board[second]:
                    count = append_move(moves, count, source, second, False)
            for delta in (-1, 1):
                target = source + side * 16 + delta
                if not target & 0x88 and (board[target] * side < 0 or target == state[EP]):
                    count = append_move(moves, count, source, target, target // 16 in (0, 7))
        elif piece in (2, 6):
            for i in range(8):
                target = source + (KNIGHT_STEPS[i] if piece == 2 else KING_STEPS[i])
                if not target & 0x88 and board[target] * side <= 0:
                    count = append_move(moves, count, source, target, False)
        else:
            for i in range(8):
                if (piece == 3 and i >= 4) or (piece == 4 and i < 4):
                    continue
                target = source + RAYS[i]
                while not target & 0x88:
                    if board[target] * side > 0:
                        break
                    count = append_move(moves, count, source, target, False)
                    if board[target]:
                        break
                    target += RAYS[i]
    king = 4 if side == 1 else 116
    shift = 0 if side == 1 else 2
    if board[king] == side * 6 and not attacked(board, king, -side):
        for direction, right in ((1, 1), (-1, 2)):
            rook = king + (3 if direction == 1 else -4)
            if not state[RIGHTS] & (right << shift) or board[rook] != side * 4:
                continue
            if board[king + direction] or board[king + 2 * direction]:
                continue
            if direction == -1 and board[king - 3]:
                continue
            # Vacate the source king square when testing attacks on its path.
            board[king] = 0
            safe = not attacked(board, king + direction, -side) and not attacked(
                board, king + 2 * direction, -side
            )
            board[king] = side * 6
            if safe:
                count = append_move(moves, count, king, king + 2 * direction, False)
    legal = 0
    for i in range(count):
        move = moves[i]
        make(board, state, move, undo)
        safe = not attacked(board, state[WK if side == 1 else BK], -side)
        unmake(board, state, move, undo)
        if safe:
            moves[legal] = move
            legal += 1
    return int(legal)


@compiled
def legal_ep(board: IntArray, state: IntArray) -> int:
    target, side = state[EP], state[TURN]
    if target < 0:
        return -1
    capture = target - side * 16
    for delta in (-1, 1):
        source = capture + delta
        if not source & 0x88 and board[source] == side:
            board[source], board[capture], board[target] = 0, 0, side
            safe = not attacked(board, state[WK if side == 1 else BK], -side)
            board[source], board[capture], board[target] = side, -side, 0
            if safe:
                return int(target)
    return -1


@compiled
def identity(board: IntArray, state: IntArray, target: IntArray) -> None:
    """Exact repetition identity, excluding non-capturable en-passant targets."""
    for i in range(5):
        target[i] = state[PACKED + i]
    target[5] = ((state[TURN] + 1) << 12) | (state[RIGHTS] << 8) | (legal_ep(board, state) + 1)


@compiled
def insufficient(board: IntArray) -> bool:
    knights, bishops, other, occupied = 0, 0, 0, 0
    bishop_colours = 0
    for square in range(120):
        if square & 0x88:
            continue
        piece = abs(board[square])
        occupied += int(piece != 0)
        knights += int(piece == 2)
        if piece == 3:
            bishops += 1
            bishop_colours |= 1 << ((square // 16 + square % 16) % 2)
        if piece in (1, 4, 5):
            other += 1
    if other:
        return False
    if knights:
        return occupied <= 3
    return bishops == 0 or bishop_colours in (1, 2)


@compiled
def perft(
    board: IntArray, state: IntArray, depth: int, moves: IntArray, undos: IntArray, ply: int = 0
) -> int:
    if depth == 0:
        return 1
    count = generate(board, state, moves[ply], undos[ply])
    if depth == 1:
        return count
    total = 0
    for i in range(count):
        move = moves[ply, i]
        make(board, state, move, undos[ply])
        total += perft(board, state, depth - 1, moves, undos, ply + 1)
        unmake(board, state, move, undos[ply])
    return total
