"""Small, original diagnostic opponents; these are not strong engine benchmarks.

Style variants share one search family. This file is copied into each run's build.
The lab-only environment variables never belong in a competition submission.
"""

import math
import os
import random
import time

import chess

STYLE = os.environ.get("CHESSLAB_STYLE", "tactical")
RNG = random.Random(os.environ.get("HARNESS_SEED", "0"))
VALUES = (0, 100, 320, 335, 500, 900, 0)
MATE = 100_000.0


def evaluate(board: chess.Board) -> float:
    if board.is_checkmate():
        return -MATE
    if board.is_stalemate() or board.is_insufficient_material():
        return 0.0
    score = 0.0
    for square, piece in board.piece_map().items():
        rank = chess.square_rank(square)
        advance = rank if piece.color else 7 - rank
        centre = 7 - abs(3.5 - chess.square_file(square)) - abs(3.5 - rank)
        bonus = 0.0
        if piece.piece_type in (chess.KNIGHT, chess.BISHOP):
            bonus += centre * (9 if STYLE == "active" else 5)
        if piece.piece_type == chess.PAWN:
            bonus += advance * 4 + centre * 2
            if STYLE == "solid":
                own_pawns = board.pieces(chess.PAWN, piece.color)
                if len(own_pawns & chess.SquareSet(chess.BB_FILES[chess.square_file(square)])) > 1:
                    bonus -= 15
        if STYLE == "active":
            bonus += len(board.attacks(square)) * 2
        score += (VALUES[piece.piece_type] + bonus) * (1 if piece.color == board.turn else -1)
    return score


def ordered(board: chess.Board) -> list[chess.Move]:
    def priority(move: chess.Move) -> float:
        victim = board.piece_type_at(move.to_square) or (
            chess.PAWN if board.is_en_passant(move) else 0
        )
        attacker = board.piece_type_at(move.from_square) or 0
        return 10 * VALUES[victim] - VALUES[attacker] + (VALUES[move.promotion or 0])

    return sorted(board.legal_moves, key=priority, reverse=True)


def search(board: chess.Board, depth: int, alpha: float, beta: float, deadline: float) -> float:
    if time.monotonic() >= deadline:
        raise TimeoutError
    if board.is_game_over() or board.is_repetition(2):
        return evaluate(board) if board.is_checkmate() else 0.0
    if depth <= 0:
        stand = evaluate(board)
        if depth <= -3:
            return stand
        if not board.is_check():
            if stand >= beta:
                return stand
            alpha = max(alpha, stand)
        moves = [
            m for m in ordered(board) if board.is_check() or board.is_capture(m) or m.promotion
        ]
        if not moves:
            return stand
    else:
        moves = ordered(board)
    for move in moves:
        board.push(move)
        try:
            value = -search(board, depth - 1, -beta, -alpha, deadline)
        finally:
            board.pop()
        if value >= beta:
            return value
        alpha = max(alpha, value)
    return alpha


def rollout(board: chess.Board, deadline: float) -> chess.Move:
    moves = list(board.legal_moves)
    RNG.shuffle(moves)
    totals = [0.0] * len(moves)
    counts = [0] * len(moves)
    index = 0
    while time.monotonic() < deadline:
        choice = index % len(moves)
        probe = board.copy(stack=False)
        probe.push(moves[choice])
        for _ in range(6):
            if probe.is_game_over() or time.monotonic() >= deadline:
                break
            probe.push(RNG.choice(list(probe.legal_moves)))
        value = evaluate(probe) * (1 if probe.turn == board.turn else -1)
        totals[choice] += math.tanh(value / 600)
        counts[choice] += 1
        index += 1
    return moves[max(range(len(moves)), key=lambda i: totals[i] / counts[i] if counts[i] else -2)]


def get_move(fen: str, time_left_ms: int) -> str:
    board = chess.Board(fen)
    moves = ordered(board)
    budget_ms = min(200.0, max(1.0, time_left_ms / 35), max(1.0, time_left_ms - 35))
    deadline = time.monotonic() + budget_ms / 1000
    if STYLE == "rollout":
        return rollout(board, deadline).uci()
    best = moves[0]
    for depth in range(1, 2 if STYLE == "positional" else 12):
        iteration_best, best_value = best, -math.inf
        try:
            for move in moves:
                board.push(move)
                try:
                    value = (
                        -evaluate(board)
                        if STYLE == "positional"
                        else -search(board, depth - 1, -math.inf, -best_value, deadline)
                    )
                finally:
                    board.pop()
                if value > best_value:
                    iteration_best, best_value = move, value
        except TimeoutError:
            break
        best = iteration_best
        moves.remove(best)
        moves.insert(0, best)
    return best.uci()
