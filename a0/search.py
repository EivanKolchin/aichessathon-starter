"""Original iterative deepening PVS with history-aware transposition bounds.

Search and evaluation are our code. python-chess supplies board state and legal moves.
No engine binaries, pretrained weights, engine-labelled lookup data or network access.
"""

import time
from collections import Counter
from dataclasses import dataclass
from typing import Literal

import chess

from a0.evaluation import MG_VALUE, EvalState, PawnCache

MATE = 30_000
INFINITY = 32_000
MAX_PLY = 96
MATE_BOUND = MATE - MAX_PLY
type PositionKey = tuple[int, int, int, int, int, int, int, int, bool, int, int | None]
type TableKey = tuple[PositionKey, int, int]
type Bound = Literal["exact", "lower", "upper"]


def position_key(board: chess.Board) -> PositionKey:
    return (
        board.pawns,
        board.knights,
        board.bishops,
        board.rooks,
        board.queens,
        board.kings,
        board.occupied_co[chess.WHITE],
        board.occupied_co[chess.BLACK],
        board.turn,
        board.castling_rights,
        board.ep_square if board.has_legal_en_passant() else None,
    )


def pack_mate(score: int, ply: int) -> int:
    return score + ply if score >= MATE_BOUND else score - ply if score <= -MATE_BOUND else score


def unpack_mate(score: int, ply: int) -> int:
    return score - ply if score >= MATE_BOUND else score + ply if score <= -MATE_BOUND else score


def quiescence_moves(board: chess.Board) -> list[chess.Move]:
    """Captures followed by quiet promotions; callers search all evasions when checked."""
    moves = list(board.generate_legal_captures())
    promotion_rank = chess.BB_RANK_7 if board.turn else chess.BB_RANK_2
    pawns = board.pawns & board.occupied_co[board.turn] & promotion_rank
    if pawns:
        # Other pawn moves cannot promote. Empty destinations exclude capture promotions,
        # which are already in the capture list, while retaining every underpromotion.
        moves.extend(board.generate_legal_moves(from_mask=pawns, to_mask=~board.occupied))
    return moves


class SearchStopped(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Entry:
    depth: int
    score: int
    bound: Bound
    move: chess.Move


@dataclass(frozen=True)
class SearchResult:
    move: chess.Move
    score: int
    depth: int
    nodes: int
    qnodes: int
    elapsed_ms: float
    tt_hits: int
    stopped: bool
    pv: tuple[str, ...]


@dataclass(frozen=True)
class SearchConfig:
    max_depth: int = 48
    max_nodes: int = 0
    quiescence: bool = True
    pvs: bool = True
    use_tt: bool = True
    aspiration: bool = True
    table_capacity: int = 32_768


class Search:
    def __init__(self, config: SearchConfig | None = None) -> None:
        self.config = config or SearchConfig()
        self.table: dict[TableKey, Entry] = {}
        self.hints: dict[PositionKey, chess.Move] = {}
        self.pawns = PawnCache()
        self.history: dict[tuple[bool, int, int], int] = {}
        self.killers: list[list[chess.Move | None]] = [[None, None] for _ in range(MAX_PLY)]
        self.counts: Counter[PositionKey] = Counter()
        self.context = 0
        self.board = chess.Board()
        self.evaluation = EvalState()
        self.nodes = self.qnodes = self.tt_hits = 0
        self.deadline = 0.0

    def _seed_history(self, board: chess.Board) -> None:
        probe = board.copy(stack=True)
        self.counts.clear()
        reversible = probe.halfmove_clock
        steps = 0
        while True:
            self.counts[position_key(probe)] += 1
            if not probe.move_stack or steps >= reversible:
                break
            probe.pop()
            steps += 1
        self.context = 0
        for key, count in self.counts.items():
            self.context ^= hash((key, count))

    def _change_count(self, key: PositionKey, direction: int) -> None:
        before = self.counts.get(key, 0)
        if before:
            self.context ^= hash((key, before))
        after = before + direction
        if after:
            self.counts[key] = after
            self.context ^= hash((key, after))
        else:
            del self.counts[key]

    def _push(self, move: chess.Move) -> tuple[tuple[int, int, int], PositionKey]:
        previous = self.evaluation.push(self.board, move)
        self.board.push(move)
        key = position_key(self.board)
        self._change_count(key, 1)
        return previous, key

    def _pop(self, undo: tuple[tuple[int, int, int], PositionKey]) -> None:
        previous, key = undo
        self._change_count(key, -1)
        self.board.pop()
        self.evaluation.restore(previous)

    def _visit(self, quiescent: bool = False) -> None:
        self.nodes += 1
        self.qnodes += quiescent
        if self.config.max_nodes and self.nodes >= self.config.max_nodes:
            raise SearchStopped
        if self.nodes % 32 == 0 and time.monotonic() >= self.deadline:
            raise SearchStopped

    def _draw(self, key: PositionKey, ply: int) -> int | None:
        if (
            self.counts[key] >= 3
            or self.board.halfmove_clock >= 100
            or self.board.is_insufficient_material()
        ):
            # A checkmate on the last move precedes the fifty-move draw.
            return -MATE + ply if self.board.is_checkmate() else 0
        return None

    def _order(
        self, moves: list[chess.Move], hint: chess.Move | None, ply: int
    ) -> list[chess.Move]:
        board = self.board

        def priority(move: chess.Move) -> int:
            if move == hint:
                return 2_000_000
            victim = board.piece_type_at(move.to_square)
            if victim is not None or board.is_en_passant(move):
                attacker = board.piece_type_at(move.from_square) or 0
                return 1_000_000 + 16 * MG_VALUE[victim or chess.PAWN] - MG_VALUE[attacker]
            if move.promotion:
                return 900_000 + MG_VALUE[move.promotion]
            if ply < MAX_PLY:
                if move == self.killers[ply][0]:
                    return 800_000
                if move == self.killers[ply][1]:
                    return 790_000
            return self.history.get((board.turn, move.from_square, move.to_square), 0)

        return sorted(moves, key=priority, reverse=True)

    def _quiesce(self, alpha: int, beta: int, ply: int, qply: int = 0) -> int:
        self._visit(True)
        key = position_key(self.board)
        draw = self._draw(key, ply)
        if draw is not None:
            return draw
        checked = self.board.is_check()
        if not self.config.quiescence:
            if not any(self.board.legal_moves):
                return -MATE + ply if checked else 0
            return self.evaluation.evaluate(self.board, self.pawns)
        if checked:
            moves = list(self.board.legal_moves)
            if not moves:
                return -MATE + ply
        else:
            if not any(self.board.legal_moves):
                return 0
            stand = self.evaluation.evaluate(self.board, self.pawns)
            if stand >= beta:
                return stand
            alpha = max(alpha, stand)
            if qply >= 8:
                return stand
            moves = quiescence_moves(self.board)
        if ply >= MAX_PLY - 1:
            return self.evaluation.evaluate(self.board, self.pawns)
        for move in self._order(moves, None, ply):
            undo = self._push(move)
            try:
                score = -self._quiesce(-beta, -alpha, ply + 1, qply + 1)
            finally:
                self._pop(undo)
            if score >= beta:
                return score
            alpha = max(alpha, score)
        return alpha

    def _negamax(self, depth: int, alpha: int, beta: int, ply: int) -> int:
        if depth <= 0:
            return self._quiesce(alpha, beta, ply)
        self._visit()
        key = position_key(self.board)
        draw = self._draw(key, ply)
        if draw is not None:
            return draw
        original_alpha = alpha
        # A position's draw prospects depend on the halfmove clock and visited positions.
        # Bound reuse requires the same context fingerprint; move hints can be shared.
        table_key = key, self.board.halfmove_clock, self.context
        entry = self.table.get(table_key) if self.config.use_tt else None
        if entry is not None and entry.depth >= depth:
            value = unpack_mate(entry.score, ply)
            if (
                entry.bound == "exact"
                or (entry.bound == "lower" and value >= beta)
                or (entry.bound == "upper" and value <= alpha)
            ):
                self.tt_hits += 1
                return value
        moves = list(self.board.legal_moves)
        if not moves:
            return -MATE + ply if self.board.is_check() else 0
        if ply >= MAX_PLY - 1:
            return self.evaluation.evaluate(self.board, self.pawns)
        best, best_move = -INFINITY, moves[0]
        hint = entry.move if entry is not None else self.hints.get(key)
        for index, move in enumerate(self._order(moves, hint, ply)):
            quiet = not self.board.is_capture(move) and not move.promotion
            undo = self._push(move)
            try:
                if self.config.pvs and index > 0:
                    score = -self._negamax(depth - 1, -alpha - 1, -alpha, ply + 1)
                    if alpha < score < beta:
                        score = -self._negamax(depth - 1, -beta, -alpha, ply + 1)
                else:
                    score = -self._negamax(depth - 1, -beta, -alpha, ply + 1)
            finally:
                self._pop(undo)
            if score > best:
                best, best_move = score, move
            alpha = max(alpha, score)
            if alpha >= beta:
                if quiet:
                    if move != self.killers[ply][0]:
                        self.killers[ply] = [move, self.killers[ply][0]]
                    history_key = self.board.turn, move.from_square, move.to_square
                    old = self.history.get(history_key, 0)
                    self.history[history_key] = min(30_000, old + depth * depth)
                break
        self._store(key, table_key, depth, best, original_alpha, beta, best_move, ply)
        return best

    def _store(
        self,
        key: PositionKey,
        table_key: TableKey,
        depth: int,
        score: int,
        alpha: int,
        beta: int,
        move: chess.Move,
        ply: int,
    ) -> None:
        if len(self.hints) >= self.config.table_capacity:
            self.hints.clear()
        self.hints[key] = move
        if not self.config.use_tt:
            return
        if len(self.table) >= self.config.table_capacity:
            self.table.clear()
        bound: Bound = "upper" if score <= alpha else "lower" if score >= beta else "exact"
        self.table[table_key] = Entry(depth, pack_mate(score, ply), bound, move)

    def _root(self, depth: int, alpha: int, beta: int, hint: chess.Move) -> tuple[int, chess.Move]:
        best, best_move = -INFINITY, hint
        for index, move in enumerate(self._order(list(self.board.legal_moves), hint, 0)):
            if time.monotonic() >= self.deadline:
                raise SearchStopped
            undo = self._push(move)
            try:
                if self.config.pvs and index > 0:
                    score = -self._negamax(depth - 1, -alpha - 1, -alpha, 1)
                    if alpha < score < beta:
                        score = -self._negamax(depth - 1, -beta, -alpha, 1)
                else:
                    score = -self._negamax(depth - 1, -beta, -alpha, 1)
            finally:
                self._pop(undo)
            if score > best:
                best, best_move = score, move
            alpha = max(alpha, score)
            if alpha >= beta:
                break
        return best, best_move

    def _pv(self, first: chess.Move, depth: int) -> tuple[str, ...]:
        probe = self.board.copy(stack=False)
        result = []
        move: chess.Move | None = first
        for _ in range(depth):
            if move is None or move not in probe.legal_moves:
                break
            result.append(move.uci())
            probe.push(move)
            move = self.hints.get(position_key(probe))
        return tuple(result)

    def analyse(self, board: chess.Board, soft_ms: float, hard_ms: float) -> SearchResult:
        started = time.monotonic()
        self.deadline = started + max(0, hard_ms) / 1000
        self.board = board.copy(stack=True)
        self.evaluation = EvalState.from_board(self.board)
        self._seed_history(self.board)
        self.nodes = self.qnodes = self.tt_hits = 0
        self.history = {key: value // 2 for key, value in self.history.items() if value > 1}
        moves = list(self.board.legal_moves)
        if not moves:
            raise ValueError("Cannot search a terminal position with no legal moves")
        best = self._order(moves, self.hints.get(position_key(board)), 0)[0]
        score = self.evaluation.evaluate(self.board, self.pawns)
        completed = 0
        stopped = False
        for depth in range(1, self.config.max_depth + 1):
            if depth > 1 and (time.monotonic() - started) * 1000 >= soft_ms:
                break
            window = 40 if depth > 2 and self.config.aspiration else INFINITY
            try:
                while True:
                    low, high = (
                        (-INFINITY, INFINITY)
                        if window >= INFINITY
                        else (max(-INFINITY, score - window), min(INFINITY, score + window))
                    )
                    current_score, current_move = self._root(depth, low, high, best)
                    if low < current_score < high or window >= INFINITY:
                        break
                    window = min(INFINITY, window * 4)
                best, score, completed = current_move, current_score, depth
            except SearchStopped:
                stopped = True
                break
            if abs(score) >= MATE_BOUND:
                break
        return SearchResult(
            best,
            score,
            completed,
            self.nodes,
            self.qnodes,
            (time.monotonic() - started) * 1000,
            self.tt_hits,
            stopped,
            self._pv(best, max(1, completed)),
        )
