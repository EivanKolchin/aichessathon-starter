"""Adapters around the unchanged event referee. Observations publish off the move clock."""

import io
import threading
import time
from pathlib import Path
from typing import Any

import chess
import chess.engine
import chess.pgn

from harness.sandbox import RUNNER, Agent, AgentFailure


class Trace:
    def __init__(self, fen: str, base_ms: int, increment_ms: int) -> None:
        self.board = chess.Board(fen)
        self.clocks = {chess.WHITE: float(base_ms), chess.BLACK: float(base_ms)}
        self.increment_ms = increment_ms
        self.frames: list[dict[str, Any]] = [self.frame()]
        self.engine_info: dict[str, dict[str, Any]] = {}

    def frame(self, san: str = "", uci: str = "", elapsed_ms: float = 0) -> dict[str, Any]:
        return {
            "fen": self.board.fen(),
            "san": san,
            "uci": uci,
            "white_ms": round(self.clocks[chess.WHITE], 1),
            "black_ms": round(self.clocks[chess.BLACK], 1),
            "elapsed_ms": round(elapsed_ms, 2),
        }

    def accept(self, fen: str, uci: str, remaining: int, elapsed: float) -> None:
        # Referee remains authoritative; malformed moves and flag falls never advance the viewer.
        if elapsed > remaining or self.board.fen() != fen:
            return
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            return
        if move not in self.board.legal_moves:
            return
        mover = self.board.turn
        san = self.board.san(move)
        self.board.push(move)
        self.clocks[mover] = remaining - elapsed + self.increment_ms
        self.frames.append(self.frame(san, uci, elapsed))


def replay(pgn: str, base_ms: int, increment_ms: int) -> list[dict[str, Any]]:
    game = chess.pgn.read_game(io.StringIO(pgn))
    if game is None or game.errors:
        raise ValueError("Referee produced an unreadable PGN")
    trace = Trace(game.board().fen(), base_ms, increment_ms)
    for node in game.mainline():
        mover = trace.board.turn
        san = trace.board.san(node.move)
        before = trace.clocks[mover]
        trace.board.push(node.move)
        clock = node.clock()
        remaining = before if clock is None else clock * 1000
        trace.clocks[mover] = remaining
        trace.frames.append(
            trace.frame(san, node.move.uci(), max(0, before + increment_ms - remaining))
        )
    return trace.frames


class PythonAgent(Agent):
    def __init__(self, command: list[str], name: str, seed: int, env: dict[str, str]) -> None:
        super().__init__(command, name, seed)
        self.extra_env = env

    def _environment(self, scratch: str) -> dict[str, str]:
        return {**super()._environment(scratch), **self.extra_env}


class UCIAgent(Agent):
    """External research opponent only. No UCI engine is imported into agent.py."""

    def __init__(self, spec: dict[str, Any], trace: Trace) -> None:
        super().__init__(spec["command"], spec["name"])
        self.spec = spec
        self.trace = trace
        self.engine: chess.engine.SimpleEngine | None = None

    def start(self, init_budget_s: float) -> None:
        try:
            self.engine = chess.engine.SimpleEngine.popen_uci(self.command, timeout=init_budget_s)
            options: dict[str, Any] = {}
            for key, value in {"Threads": 1, "Hash": 64}.items():
                if key in self.engine.options:
                    options[key] = value
            options.update(self.spec["options"])
            self.engine.configure(options)
            self.trace.engine_info[self.spec["id"]] = {
                "id": dict(self.engine.id),
                "options": options,
                "max_move_ms": self.spec["max_move_ms"],
                "max_nodes": self.spec["max_nodes"],
                "ponder": False,
            }
        except (OSError, TimeoutError, chess.engine.EngineError) as error:
            self.stderr_log = str(error)
            raise AgentFailure("init") from error

    def suspend(self) -> None:
        # play() returns after bestmove; pondering is explicitly disabled.
        pass

    def resume(self) -> None:
        pass

    def move(self, fen: str, time_left_ms: int) -> str:
        if self.engine is None:
            raise AgentFailure("init")
        board = self.trace.board.copy(stack=True)
        if board.fen() != fen:
            raise AgentFailure("crash")
        clocks = dict(self.trace.clocks)
        clocks[board.turn] = time_left_ms
        maximum = self.spec["max_move_ms"]
        limit = chess.engine.Limit(
            white_clock=clocks[chess.WHITE] / 1000,
            black_clock=clocks[chess.BLACK] / 1000,
            white_inc=self.trace.increment_ms / 1000,
            black_inc=self.trace.increment_ms / 1000,
            time=None if maximum is None else min(maximum, max(1, time_left_ms - 20)) / 1000,
            # The clock still applies. A node-capped engine that somehow outruns its wall time
            # is stopped by the watchdog below exactly like any other.
            nodes=self.spec["max_nodes"],
        )
        self.engine.timeout = max(0.001, time_left_ms / 1000) + 0.5
        expired = threading.Event()
        engine = self.engine

        def expire() -> None:
            expired.set()
            engine.close()

        # python-chess does not apply SimpleEngine.timeout to clock-only go limits.
        # A hard wall-time watchdog is therefore needed even with a full UCI clock.
        watchdog = threading.Timer(self.engine.timeout, expire)
        watchdog.daemon = True
        watchdog.start()
        try:
            result = self.engine.play(board, limit, ponder=False)
        except TimeoutError as error:
            self.stderr_log = str(error)
            raise AgentFailure("flag") from error
        except chess.engine.EngineError as error:
            self.stderr_log = str(error)
            raise AgentFailure("flag" if expired.is_set() else "crash") from error
        finally:
            watchdog.cancel()
        if result.move is None:
            raise AgentFailure("illegal")
        return result.move.uci()

    def stop(self) -> None:
        if self.engine is not None:
            self.engine.close()
            self.engine = None


class ObservedAgent(Agent):
    def __init__(self, inner: Agent, trace: Trace, key: str = "engine") -> None:
        super().__init__([], inner.name)
        self.inner = inner
        self.trace = trace
        self.key = key
        self.stop_lock = threading.Lock()
        self.pending: tuple[str, str, int, float] | None = None

    def start(self, init_budget_s: float) -> None:
        started = time.monotonic()
        try:
            self.inner.start(init_budget_s)
        finally:
            self.trace.engine_info.setdefault(self.key, {})["init_ms"] = round(
                (time.monotonic() - started) * 1000, 2
            )

    def move(self, fen: str, time_left_ms: int) -> str:
        started = time.monotonic()
        uci = self.inner.move(fen, time_left_ms)
        self.pending = fen, uci, time_left_ms, (time.monotonic() - started) * 1000
        return uci

    def suspend(self) -> None:
        self.inner.suspend()
        if self.pending is not None:
            self.trace.accept(*self.pending)
            self.pending = None

    def resume(self) -> None:
        self.inner.resume()

    def stop(self) -> None:
        with self.stop_lock:
            self.inner.stop()
            self.stderr_log = self.inner.stderr_log


def make_player(spec: dict[str, Any], seed: int, trace: Trace, python: str) -> ObservedAgent:
    inner: Agent
    if spec["kind"] == "uci":
        inner = UCIAgent(spec, trace)
    else:
        command = [python, str(RUNNER), str(Path(spec["frozen_path"]))]
        inner = PythonAgent(command, spec["name"], seed, spec["env"])
    return ObservedAgent(inner, trace, spec["id"])
