"""One human-versus-engine game, played a move at a time from the browser.

Both sides keep a real clock, and the engine is spoken to exactly as a batch speaks to it, so
what you feel here is what it does under time. A clock that runs out loses the game the way the
referee would score it. Nothing here is scored and nothing is written to disk; sparring is for
looking at an engine, not for measuring it.
"""

import random
import threading
import time
from typing import Any

import chess
import chess.pgn

from chesslab.players import ObservedAgent, Trace, make_player
from chesslab.registry import EngineSpec
from harness.referee import FAILED_TERMINATIONS, RESULT_HEADERS
from harness.rules import INIT_BUDGET_S, PLY_CAP
from harness.sandbox import AgentFailure

HUMAN = "you"


def side(colour: chess.Color) -> str:
    return "white" if colour == chess.WHITE else "black"


def adjudicate(board: chess.Board, ply_cap: int) -> tuple[str, str] | None:
    """The finished-position questions harness/referee.py asks, in the same order.

    Sparring cannot call the referee, which drives both sides itself in one loop. python-chess
    still decides every rule; this only asks it the same things at the same points, so a game
    you play ends exactly where a batch game would have ended.
    """
    finish = board.outcome()
    if finish is not None:
        winner = finish.winner
        result = "draw" if winner is None else side(winner)
        return result, finish.termination.name.lower()
    if board.is_repetition(3):
        return "draw", "threefold_repetition"
    if board.is_fifty_moves():
        return "draw", "fifty_moves"
    if board.ply() >= ply_cap:
        return "draw", "ply_cap"
    return None


class Sparring:
    def __init__(
        self,
        spec: EngineSpec,
        engine_colour: chess.Color,
        base_ms: int,
        increment_ms: int,
        opening: dict[str, str],
        python: str,
        ply_cap: int = PLY_CAP,
    ) -> None:
        self.spec = spec
        self.engine_colour = engine_colour
        self.opening = opening
        self.ply_cap = ply_cap
        self.python = python
        self.base_ms = base_ms
        self.seed = random.randrange(2**31)
        self.trace = Trace(opening["fen"], base_ms, increment_ms)
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.player: ObservedAgent | None = None
        self.worker: threading.Thread | None = None
        self.status = "starting"
        self.result: str | None = None
        self.termination: str | None = None
        self.log = ""
        self.thinking = False
        self.created_at = time.time()
        self.turn_started = time.monotonic()

    # ── lifecycle ──────────────────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._spawn(self._open)

    def _open(self) -> None:
        data = self.spec.data()
        # Sparring runs the engine where it lives. A batch freezes a build because it has to be
        # able to say later what played; here you want the file you just edited.
        data["frozen_path"] = str(self.spec.directory())
        player = make_player(data, self.seed, self.trace, self.python)
        try:
            player.start(INIT_BUDGET_S)
            player.suspend()
        except AgentFailure as failure:
            player.stop()
            self._finish(side(not self.engine_colour), failure.reason, player.stderr_log)
            return
        with self.lock:
            self.player = player
            if self.closed.is_set():
                player.stop()
                return
            self.status = "playing"
            self.turn_started = time.monotonic()
        self._wake()

    def close(self) -> None:
        self.closed.set()
        with self.lock:
            player, self.player = self.player, None
            self.status = "finished"
        if player is not None:
            player.stop()
        if self.worker is not None:
            self.worker.join(timeout=2)

    def finished(self) -> bool:
        with self.lock:
            return self.status == "finished"

    # ── moves ──────────────────────────────────────────────────────────────────────────────

    def play(self, uci: str) -> None:
        with self.lock:
            if self.status != "playing":
                raise ValueError("This game is not taking moves")
            if self.thinking or self.trace.board.turn == self.engine_colour:
                raise ValueError("It is the engine's move")
            if self._flagged():
                return
            try:
                move = chess.Move.from_uci(uci)
            except ValueError:
                raise ValueError("That is not a move") from None
            if move not in self.trace.board.legal_moves:
                raise ValueError("That move is not legal in this position")
            you = not self.engine_colour
            spent = (time.monotonic() - self.turn_started) * 1000
            self.trace.clocks[you] += self.trace.increment_ms - spent
            self._push(move, spent)
            if self._settled():
                return
        self._wake()

    def resign(self) -> None:
        with self.lock:
            if self.status != "playing":
                raise ValueError("This game is already over")
            self._finish(side(self.engine_colour), "resignation")

    def undo(self) -> None:
        """Step back to your own move again, taking the engine's reply with it."""
        with self.lock:
            if self.thinking:
                raise ValueError("Wait for the engine to finish its move")
            if self.status == "starting":
                raise ValueError("The engine is still starting")
            if self.termination in FAILED_TERMINATIONS:
                raise ValueError("The engine stopped; start a new game")
            board = self.trace.board
            take = 2 if board.turn != self.engine_colour else 1
            if len(self.trace.frames) - 1 < take:
                raise ValueError("There is nothing to take back")
            for _ in range(take):
                board.pop()
                self.trace.frames.pop()
            frame = self.trace.frames[-1]
            self.trace.clocks[chess.WHITE] = float(frame["white_ms"])
            self.trace.clocks[chess.BLACK] = float(frame["black_ms"])
            self.status = "playing"
            self.result = self.termination = None
            self.turn_started = time.monotonic()

    def _push(self, move: chess.Move, elapsed_ms: float) -> None:
        """Your move. The engine's own moves reach the trace through ObservedAgent."""
        board = self.trace.board
        san = board.san(move)
        board.push(move)
        self.trace.frames.append(self.trace.frame(san, move.uci(), elapsed_ms))

    def _flag_result(self, mover: chess.Color) -> str:
        """FIDE, and harness/referee.py: a flag fall is a draw when the other side cannot mate."""
        if self.trace.board.has_insufficient_material(not mover):
            return "draw"
        return side(not mover)

    def _flagged(self) -> bool:
        """A clock that runs out while nobody is moving still ends the game."""
        if self.status != "playing" or self.thinking:
            return False
        turn = self.trace.board.turn
        if turn == self.engine_colour:
            return False
        if (time.monotonic() - self.turn_started) * 1000 <= self.trace.clocks[turn]:
            return False
        self.trace.clocks[turn] = 0.0
        self._finish(self._flag_result(turn), "flag")
        return True

    def _wake(self) -> None:
        with self.lock:
            if self.status != "playing" or self.thinking:
                return
            self.turn_started = time.monotonic()
            if self.trace.board.turn != self.engine_colour:
                return
            self.thinking = True
        self._spawn(self._think)

    def _think(self) -> None:
        with self.lock:
            player = self.player
            fen = self.trace.board.fen()
            remaining = self.trace.clocks[self.engine_colour]
        if player is None:
            self._finish(side(not self.engine_colour), "init")
            return
        loser = side(not self.engine_colour)
        # Outside the lock: a move can take the whole clock, and the browser still polls.
        player.resume()
        started = time.monotonic()
        try:
            player.move(fen, int(remaining))
        except AgentFailure as failure:
            if failure.reason == "flag":
                with self.lock:
                    self.trace.clocks[self.engine_colour] = 0.0
                    self._finish(self._flag_result(self.engine_colour), "flag", player.stderr_log)
                return
            self._finish(loser, failure.reason, player.stderr_log)
            return
        elapsed = (time.monotonic() - started) * 1000
        with self.lock:
            before = len(self.trace.frames)
            player.suspend()  # ObservedAgent hands the move to Trace, which vets it.
            self.thinking = False
            if len(self.trace.frames) == before:
                if elapsed > remaining:
                    self.trace.clocks[self.engine_colour] = 0.0
                    self._finish(self._flag_result(self.engine_colour), "flag")
                else:
                    self._finish(loser, "illegal")
                return
            self._settled()
            self.turn_started = time.monotonic()

    def _settled(self) -> bool:
        verdict = adjudicate(self.trace.board, self.ply_cap)
        if verdict is None:
            return False
        self._finish(*verdict)
        return True

    def _finish(self, result: str, termination: str, log: str = "") -> None:
        with self.lock:
            self.thinking = False
            if self.closed.is_set() or self.status == "finished":
                return
            self.status = "finished"
            self.result = result
            self.termination = termination
            if log:
                self.log = log

    def _spawn(self, target: Any) -> None:
        self.worker = threading.Thread(target=target, name="chesslab-sparring", daemon=True)
        self.worker.start()

    # ── view ───────────────────────────────────────────────────────────────────────────────

    def pgn(self) -> str:
        game = chess.pgn.Game.from_board(self.trace.board)
        game.headers["Event"] = "Chess Lab sparring"
        game.headers["White"] = self.spec.name if self.engine_colour == chess.WHITE else "You"
        game.headers["Black"] = self.spec.name if self.engine_colour == chess.BLACK else "You"
        game.headers["Result"] = RESULT_HEADERS[self.result or "void"]
        if self.termination:
            game.headers["Termination"] = self.termination
        return str(game)

    def state(self) -> dict[str, Any]:
        with self.lock:
            self._flagged()
            board = self.trace.board
            human = not self.engine_colour
            your_turn = self.status == "playing" and not self.thinking and board.turn == human
            # Both clocks are shown as time remaining, and the side to move is spending it now.
            remaining = {side(colour): value for colour, value in self.trace.clocks.items()}
            running = side(board.turn) if self.status == "playing" else None
            if running is not None:
                spent = (time.monotonic() - self.turn_started) * 1000
                remaining[running] = max(0.0, remaining[running] - spent)
            return {
                "id": "sparring",
                "engine": self.spec.id,
                "engine_name": self.spec.name,
                "engine_colour": side(self.engine_colour),
                "your_colour": side(human),
                "white": self.spec.id if self.engine_colour == chess.WHITE else HUMAN,
                "black": self.spec.id if self.engine_colour == chess.BLACK else HUMAN,
                "status": self.status,
                "thinking": self.thinking,
                "your_turn": your_turn,
                "clocks": {colour: round(value, 1) for colour, value in remaining.items()},
                "running": running,
                "result": self.result,
                "termination": self.termination,
                "opening": self.opening,
                "limits": {
                    "base_ms": self.base_ms,
                    "increment_ms": self.trace.increment_ms,
                    "ply_cap": self.ply_cap,
                },
                "legal": sorted(move.uci() for move in board.legal_moves) if your_turn else [],
                "frames": self.trace.frames[:],
                "pgn": self.pgn(),
                "engine_info": dict(self.trace.engine_info),
                "log": self.log,
                "created_at": self.created_at,
                "can_undo": len(self.trace.frames) > 1 and not self.thinking,
            }
