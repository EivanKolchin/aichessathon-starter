"""Original tapered material/placement evaluation with reversible incremental state.

All values are hand-chosen starting hypotheses, not weights from another engine.
The tables are generated from the geometric formulas below at import time.
"""

from dataclasses import dataclass

import chess

MG_VALUE = (0, 100, 315, 330, 505, 950, 0)
EG_VALUE = (0, 120, 305, 330, 525, 930, 0)
PHASE_VALUE = (0, 0, 1, 1, 2, 4, 0)
MAX_PHASE = 24
TEMPO = 8


def placement(piece: int, square: int) -> tuple[int, int]:
    file, rank = chess.square_file(square), chess.square_rank(square)
    distance = abs(2 * file - 7) + abs(2 * rank - 7)
    centre = 14 - distance
    if piece == chess.PAWN:
        return 5 * rank + 2 * centre, 10 * rank + centre
    if piece == chess.KNIGHT:
        return 5 * centre - (12 if rank == 0 else 0), 3 * centre
    if piece == chess.BISHOP:
        return 3 * centre + (8 if rank > 0 else 0), 2 * centre
    if piece == chess.ROOK:
        return (18 if rank == 6 else 0) + rank, 2 * centre
    if piece == chess.QUEEN:
        return centre - (6 if rank > 2 else 0), 3 * centre
    # Kings prefer shelter during the middlegame and central squares in the ending.
    return -3 * centre - rank * 5 + (18 if rank == 0 and file in (1, 2, 6) else 0), 5 * centre


MG_TABLE: tuple[tuple[int, ...], ...] = tuple(
    tuple(MG_VALUE[piece] + placement(piece, square)[0] for square in range(64))
    for piece in range(7)
)
EG_TABLE: tuple[tuple[int, ...], ...] = tuple(
    tuple(EG_VALUE[piece] + placement(piece, square)[1] for square in range(64))
    for piece in range(7)
)
ADJACENT_FILES = tuple(
    (chess.BB_FILES[file - 1] if file else 0) | (chess.BB_FILES[file + 1] if file < 7 else 0)
    for file in range(8)
)
PASSED_MASKS = tuple(
    tuple(
        sum(
            chess.BB_SQUARES[target]
            for target in range(64)
            if abs(chess.square_file(target) - chess.square_file(square)) <= 1
            and ((target // 8 > square // 8) if color else (target // 8 < square // 8))
        )
        for square in range(64)
    )
    for color in (chess.BLACK, chess.WHITE)
)


class PawnCache:
    def __init__(self, capacity: int = 4096) -> None:
        self.capacity = capacity
        self.entries: dict[tuple[int, int], tuple[int, int]] = {}

    def score(self, white: int, black: int) -> tuple[int, int]:
        key = white, black
        cached = self.entries.get(key)
        if cached is not None:
            return cached
        mg = eg = 0
        for color, pawns, enemy in ((True, white, black), (False, black, white)):
            sign = 1 if color else -1
            for file in range(8):
                count = (pawns & chess.BB_FILES[file]).bit_count()
                if count > 1:
                    mg -= sign * 13 * (count - 1)
                    eg -= sign * 19 * (count - 1)
            for square in chess.scan_forward(pawns):
                file, rank = square % 8, square // 8 if color else 7 - square // 8
                if not pawns & ADJACENT_FILES[file]:
                    mg -= sign * 12
                    eg -= sign * 9
                if not enemy & PASSED_MASKS[color][square]:
                    mg += sign * (rank * rank + 3)
                    eg += sign * (4 * rank * rank + 5)
                if chess.BB_PAWN_ATTACKS[not color][square] & pawns:
                    mg += sign * 7
                    eg += sign * 10
        if len(self.entries) >= self.capacity:
            self.entries.clear()
        self.entries[key] = mg, eg
        return mg, eg


@dataclass
class EvalState:
    mg: int = 0
    eg: int = 0
    phase: int = 0

    @classmethod
    def from_board(cls, board: chess.Board) -> "EvalState":
        state = cls()
        for square, piece in board.piece_map().items():
            state.adjust(piece.piece_type, piece.color, square, 1)
        return state

    def adjust(self, piece: int, color: chess.Color, square: int, direction: int) -> None:
        relative = square if color else square ^ 56
        sign = direction if color else -direction
        self.mg += sign * MG_TABLE[piece][relative]
        self.eg += sign * EG_TABLE[piece][relative]
        self.phase += direction * PHASE_VALUE[piece]

    def push(self, board: chess.Board, move: chess.Move) -> tuple[int, int, int]:
        """Apply evaluation deltas before Board.push; save an exact undo record."""
        previous = self.mg, self.eg, self.phase
        piece = board.piece_type_at(move.from_square)
        assert piece is not None
        color = board.turn
        capture_square = move.to_square
        if board.is_en_passant(move):
            capture_square += -8 if color else 8
        captured = board.piece_type_at(capture_square)
        if captured is not None:
            self.adjust(captured, not color, capture_square, -1)
        self.adjust(piece, color, move.from_square, -1)
        self.adjust(move.promotion or piece, color, move.to_square, 1)
        if board.is_castling(move):
            rank = 0 if color else 56
            kingside = move.to_square > move.from_square
            self.adjust(chess.ROOK, color, rank + (7 if kingside else 0), -1)
            self.adjust(chess.ROOK, color, rank + (5 if kingside else 3), 1)
        return previous

    def restore(self, previous: tuple[int, int, int]) -> None:
        self.mg, self.eg, self.phase = previous

    def evaluate(self, board: chess.Board, pawns: PawnCache) -> int:
        white = board.occupied_co[chess.WHITE]
        black = board.occupied_co[chess.BLACK]
        pawn_mg, pawn_eg = pawns.score(board.pawns & white, board.pawns & black)
        mg, eg = self.mg + pawn_mg, self.eg + pawn_eg
        for color, own, enemy in ((True, white, black), (False, black, white)):
            sign = 1 if color else -1
            if (board.bishops & own).bit_count() >= 2:
                mg += sign * 24
                eg += sign * 36
            own_pawns, enemy_pawns = board.pawns & own, board.pawns & enemy
            for square in chess.scan_forward(board.rooks & own):
                file = chess.BB_FILES[square % 8]
                if not own_pawns & file:
                    mg += sign * (20 if not enemy_pawns & file else 11)
                    eg += sign * 7
            king = board.king(color)
            if king is not None:
                shield_rank = king // 8 + (1 if color else -1)
                if 0 <= shield_rank < 8:
                    nearby = ADJACENT_FILES[king % 8] | chess.BB_FILES[king % 8]
                    shield = (own_pawns & nearby & chess.BB_RANKS[shield_rank]).bit_count()
                    mg += sign * 12 * shield
        phase = min(MAX_PHASE, self.phase)
        numerator = mg * phase + eg * (MAX_PHASE - phase)
        white_score = numerator // MAX_PHASE if numerator >= 0 else -((-numerator) // MAX_PHASE)
        return (white_score if board.turn else -white_score) + TEMPO
