"""Stateful competition entry point, board-history reconstruction and clock allocation."""

import json
import time
from dataclasses import dataclass

import chess

from a0 import __version__
from a0.search import Search, SearchResult


@dataclass(frozen=True)
class Budget:
    soft_ms: float
    hard_ms: float
    reserve_ms: float


class TimeControl:
    def __init__(self) -> None:
        self.last_before: int | None = None
        self.last_elapsed = 0.0
        self.increment_estimate = 0.0

    def observe(self, remaining: int) -> None:
        if self.last_before is not None:
            observed = max(0.0, remaining - self.last_before + self.last_elapsed)
            # Use only clock evidence. The contract supplies no increment argument.
            observed = max(0.0, observed - 5.0)
            self.increment_estimate = 0.5 * self.increment_estimate + 0.5 * observed

    def allocate(self, remaining: int, board: chess.Board) -> Budget:
        reserve = min(80.0, max(12.0, remaining * 0.03))
        available = max(0.0, remaining - reserve)
        horizon = 30 if board.occupied.bit_count() > 12 else 22
        soft = min(available, available / horizon + 0.65 * self.increment_estimate)
        hard = min(available, 1.9 * soft)
        return Budget(soft, hard, reserve)

    def finish(self, before: int, elapsed: float) -> None:
        self.last_before = before
        self.last_elapsed = elapsed


class ChessAgent:
    def __init__(self, log: bool = True) -> None:
        self.search = Search()
        self.clock = TimeControl()
        self.board: chess.Board | None = None
        self.last_result: SearchResult | None = None
        self.log = log

    def synchronise(self, fen: str) -> chess.Board:
        if self.board is not None:
            if self.board.fen() == fen:
                return self.board
            # Our previous chosen move is already pushed. Recover the opponent's one move.
            for move in list(self.board.legal_moves):
                self.board.push(move)
                if self.board.fen() == fen:
                    return self.board
                self.board.pop()
        self.board = chess.Board(fen)
        if not self.board.is_valid() or self.board.chess960:
            raise ValueError("A0 expects a valid standard-chess FEN")
        self.search.table.clear()
        self.search.hints.clear()
        self.clock = TimeControl()
        return self.board

    def get_move(self, fen: str, time_left_ms: int) -> str:
        started = time.monotonic()
        board = self.synchronise(fen)
        self.clock.observe(time_left_ms)
        if time_left_ms <= 60:
            move = next(iter(board.legal_moves), None)
            if move is None:
                raise ValueError("No legal move available")
            board.push(move)
            self.last_result = None
            self.clock.finish(time_left_ms, (time.monotonic() - started) * 1000)
            return move.uci()
        budget = self.clock.allocate(time_left_ms, board)
        preparation_ms = (time.monotonic() - started) * 1000
        self.last_result = self.search.analyse(
            board,
            max(0, budget.soft_ms - preparation_ms),
            max(0, budget.hard_ms - preparation_ms),
        )
        move = self.last_result.move
        board.push(move)
        elapsed_ms = (time.monotonic() - started) * 1000
        self.clock.finish(time_left_ms, elapsed_ms)
        if self.log:
            result = self.last_result
            print(
                json.dumps(
                    {
                        "engine": f"A0 {__version__}",
                        "move": move.uci(),
                        "depth": result.depth,
                        "score_cp": result.score,
                        "nodes": result.nodes,
                        "qnodes": result.qnodes,
                        "tt_hits": result.tt_hits,
                        "elapsed_ms": round(elapsed_ms, 1),
                        "pv": result.pv,
                    }
                ),
                flush=True,
            )
        return move.uci()
