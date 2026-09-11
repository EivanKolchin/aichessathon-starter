"""Shared game state and clock management; entrypoints warm their selected evaluator."""

import json
import time

import chess

from a0.engine import TimeControl
from a0.search import SearchResult
from a1 import __version__
from a1.search import Search


class ChessAgent:
    def __init__(self, log: bool = True, search: Search | None = None) -> None:
        self.search = search or Search()
        self.clock = TimeControl()
        self.board: chess.Board | None = None
        self.last_result: SearchResult | None = None
        self.log = log

    def synchronise(self, fen: str) -> chess.Board:
        if self.board is not None:
            if self.board.fen() == fen:
                return self.board
            for move in list(self.board.legal_moves):
                self.board.push(move)
                if self.board.fen() == fen:
                    return self.board
                self.board.pop()
        self.board = chess.Board(fen)
        if not self.board.is_valid() or self.board.chess960:
            raise ValueError("A1 expects a valid standard-chess FEN")
        self.search.reset()
        self.clock = TimeControl()
        return self.board

    def get_move(self, fen: str, time_left_ms: int) -> str:
        started = time.perf_counter()
        board = self.synchronise(fen)
        self.clock.observe(time_left_ms)
        self.last_result = None
        if time_left_ms <= 60:
            move = next(iter(board.legal_moves), None)
            if move is None:
                raise ValueError("No legal move available")
        else:
            budget = self.clock.allocate(time_left_ms, board)
            preparation_ms = (time.perf_counter() - started) * 1000
            self.last_result = self.search.analyse(
                board,
                max(0, budget.soft_ms - preparation_ms),
                max(0, budget.hard_ms - preparation_ms),
            )
            move = self.last_result.move
        board.push(move)
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.clock.finish(time_left_ms, elapsed_ms)
        if self.log and self.last_result is not None:
            result = self.last_result
            print(
                json.dumps(
                    {
                        "engine": f"A1 {__version__}",
                        "move": move.uci(),
                        "depth": result.depth,
                        "score_cp": result.score,
                        "nodes": result.nodes,
                        "qnodes": result.qnodes,
                        "elapsed_ms": round(elapsed_ms, 1),
                        "pv": result.pv,
                    }
                ),
                flush=True,
            )
        return move.uci()
