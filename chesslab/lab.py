"""Sequential match scheduler and auditable on-disk experiment records."""

import copy
import hashlib
import json
import os
import platform
import random
import shutil
import sys
import threading
import time
import uuid
from contextlib import suppress
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import chess

from chesslab.builds import build_spec, extract
from chesslab.locking import WorkspaceLock
from chesslab.openings import Opening, catalog
from chesslab.players import ObservedAgent, Trace, make_player, replay
from chesslab.registry import (
    ROOT,
    EngineSpec,
    custom_engines,
    drop,
    identifier,
    file_hash,
    freeze,
    load_registry,
    parse,
    store,
    write_json,
)
from chesslab.sparring import Sparring
from chesslab.statistics import summarise
from harness.referee import play_match
from harness.rules import INIT_BUDGET_S
from harness.sandbox import AgentFailure, local


def bounded(request: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = request.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{key} must be an integer between {low} and {high}")
    return value


def runtime_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in ("chess", "numpy", "numba", "torch", "onnxruntime"):
        try:
            versions[name] = version(name)
        except PackageNotFoundError:
            versions[name] = None
    return versions


def schedule(
    candidate: str, opponents: list[str], openings: list[Opening], seed: int
) -> list[dict[str, Any]]:
    pairs = [(opponent, opening) for opponent in opponents for opening in openings]
    random.Random(seed).shuffle(pairs)
    games: list[dict[str, Any]] = []
    for index, (opponent, opening) in enumerate(pairs):
        # Stable per pair and shared across the colour swap. Timing is still nondeterministic.
        pair_seed = int.from_bytes(
            hashlib.sha256(f"{seed}:{opponent}:{opening.id}".encode()).digest()[:4]
        )
        for side in (0, 1):
            white, black = (candidate, opponent) if side == 0 else (opponent, candidate)
            games.append(
                {
                    "id": f"g{len(games) + 1:05d}",
                    "pair_id": f"p{index + 1:05d}",
                    "white": white,
                    "black": black,
                    "opponent": opponent,
                    "opening": opening.data(),
                    "seed": pair_seed,
                    "status": "queued",
                    "result": None,
                    "termination": None,
                    "plies": 0,
                }
            )
    return games


class Lab:
    def __init__(self, data_dir: Path, registry_path: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.registry_path = registry_path.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_lock = WorkspaceLock(self.data_dir / ".writer.lock")
        self.lock = threading.RLock()
        self.stop_requested = threading.Event()
        self.closing = threading.Event()
        self.worker: threading.Thread | None = None
        self.active: dict[str, Any] | None = None
        self.live_game: dict[str, Any] | None = None
        self.trace: Trace | None = None
        self.players: list[ObservedAgent] = []
        self.sparring: Sparring | None = None
        self.agents_dir = self.registry_path.parent / "agents"
        # Restart never silently resumes an experiment under different code or conditions.
        for path in self.data_dir.glob("*/manifest.json"):
            manifest = json.loads(path.read_text(encoding="utf-8"))
            if manifest["status"] in {"preparing", "running", "stopping"}:
                manifest["status"] = "interrupted"
                for game in manifest["games"]:
                    if game["status"] == "running":
                        game["status"] = "interrupted"
                write_json(path, manifest)

    def catalog(self) -> dict[str, Any]:
        specs = load_registry(self.registry_path)
        added = {item["id"] for item in custom_engines(self.registry_path)}
        engines = []
        for spec in specs.values():
            available, reason = spec.availability()
            engines.append(
                {
                    **spec.data(),
                    "available": available,
                    "reason": reason,
                    "custom": spec.id in added,
                }
            )
        return {"engines": engines, "openings": [o.data() for o in catalog()]}

    def register(self, request: dict[str, Any]) -> dict[str, Any]:
        """Add or replace one engine from the browser, the way `chesslab register` does."""
        spec = parse(request)
        present, reason = spec.present()
        if not present:
            raise ValueError(reason)
        with self.lock:
            store(self.registry_path, spec)
        return self.catalog()

    def forget(self, engine_id: str) -> dict[str, Any]:
        with self.lock:
            if self.sparring is not None and self.sparring.spec.id == engine_id:
                raise ValueError("That engine is in the middle of a game")
            drop(self.registry_path, engine_id)
        return self.catalog()

    def start(self, request: dict[str, Any]) -> str:
        specs = load_registry(self.registry_path)
        candidate = request.get("candidate", "candidate")
        opponents = request.get("opponents", [])
        opening_ids = request.get("openings", [])
        if not isinstance(opponents, list) or not all(isinstance(x, str) for x in opponents):
            raise ValueError("Opponents must be a list of engine ids")
        if not isinstance(opening_ids, list) or not all(isinstance(x, str) for x in opening_ids):
            raise ValueError("Openings must be a list of opening ids")
        if not isinstance(candidate, str) or candidate not in specs:
            raise ValueError("Choose a registered candidate")
        if not opponents or len(set(opponents)) != len(opponents) or candidate in opponents:
            raise ValueError("Choose distinct opponents other than the candidate")
        selected = [candidate, *opponents]
        if any(key not in specs for key in selected):
            raise ValueError("Unknown engine id")
        for key in selected:
            ready, reason = specs[key].availability()
            if not ready:
                raise ValueError(f"{specs[key].name}: {reason}")
        all_openings = {o.id: o for o in catalog()}
        if not opening_ids or len(set(opening_ids)) != len(opening_ids):
            raise ValueError("Choose distinct opening positions")
        if any(key not in all_openings for key in opening_ids):
            raise ValueError("Unknown opening id")
        openings = [all_openings[key] for key in opening_ids]
        if len({o.split for o in openings}) != 1:
            raise ValueError("Keep development and validation batches separate")
        limits: dict[str, int] = {
            key: bounded(request, key, default, low, high)
            for key, default, low, high in (
                ("base_ms", 10000, 100, 3_600_000),
                ("increment_ms", 100, 0, 60_000),
                ("ply_cap", 600, 20, 600),
                ("seed", 42, 0, 2**31 - 1),
            )
        }
        if any(chess.Board(o.fen).ply() >= limits["ply_cap"] for o in openings):
            raise ValueError("Ply cap must exceed every starting position's ply number")
        if len(opponents) * len(openings) * 2 > 5000:
            raise ValueError("A batch supports up to 5,000 games")
        with self.lock:
            if self.worker is not None and self.worker.is_alive():
                raise ValueError(
                    "A batch is already running; let it finish or stop after this game"
                )
            # One core, one measurement: a sparring game would take time out of every match.
            if self.sparring is not None:
                if not self.sparring.finished():
                    raise ValueError("Finish or leave your game before starting a batch")
                self.sparring.close()
                self.sparring = None
            run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
            self.active = {
                "schema": 1,
                "id": run_id,
                "created_at": time.time(),
                "status": "preparing",
                "candidate": candidate,
                "split": openings[0].split,
                "limits": limits,
                "label": str(request.get("label", "Untitled experiment"))[:100],
                "games": schedule(candidate, opponents, openings, limits["seed"]),
                "engines": {key: specs[key].data() for key in selected},
                "environment": {
                    "python": sys.version,
                    "platform": platform.platform(),
                    "machine": platform.machine(),
                    "processor": platform.processor(),
                    "host": platform.node(),
                    "logical_cpus": os.cpu_count(),
                    "packages": runtime_versions(),
                    "chess": chess.__version__,
                    "parallel_games": 1,
                    "platform_container": False,
                },
                "harness_sha256": {p.name: file_hash(p) for p in (ROOT / "harness").glob("*.py")},
                "lab_sha256": {
                    str(p.relative_to(ROOT)): file_hash(p) for p in (ROOT / "chesslab").glob("*.py")
                },
                "statistics_note": "Exploratory public openings; no auto-promotion or Elo claim",
            }
            self.live_game = None
            self.trace = None
            self.stop_requested.clear()
            self._save()
            self.worker = threading.Thread(target=self._run, name="chesslab-matches", daemon=True)
            self.worker.start()
            return run_id

    def stop(self) -> None:
        with self.lock:
            if self.active and self.active["status"] in {"running", "preparing"}:
                self.stop_requested.set()
                self.active["status"] = "stopping"
                self._save()

    def _save(self) -> None:
        if self.active is not None:
            self.active["summary"] = summarise(self.active["games"], self.active["candidate"])
            write_json(self.data_dir / self.active["id"] / "manifest.json", self.active)

    def _run(self) -> None:
        assert self.active is not None
        run = self.active
        directory = self.data_dir / run["id"]
        try:
            for key, spec in list(run["engines"].items()):
                frozen = freeze(EngineSpec(**spec), directory / "builds" / key)
                with self.lock:
                    run["engines"][key] = frozen
            with self.lock:
                run["status"] = "stopping" if self.stop_requested.is_set() else "running"
                self._save()
            limits = run["limits"]
            for game in run["games"]:
                if self.stop_requested.is_set():
                    break
                trace = Trace(game["opening"]["fen"], limits["base_ms"], limits["increment_ms"])
                white = make_player(
                    run["engines"][game["white"]], game["seed"], trace, sys.executable
                )
                black = make_player(
                    run["engines"][game["black"]], game["seed"], trace, sys.executable
                )
                with self.lock:
                    game["status"] = "running"
                    self.live_game, self.trace, self.players = game, trace, [white, black]
                    self._save()
                outcome = play_match(
                    white,
                    black,
                    limits["base_ms"],
                    limits["increment_ms"],
                    limits["ply_cap"],
                    game["opening"]["fen"],
                )
                if self.closing.is_set():
                    raise RuntimeError("Server shutdown interrupted this game; it is not scored")
                frames = replay(outcome.pgn, limits["base_ms"], limits["increment_ms"])
                with self.lock:
                    game.update(
                        status="completed",
                        result=outcome.result,
                        termination=outcome.termination,
                        plies=len(frames) - 1,
                    )
                    detail = {
                        **game,
                        "frames": frames,
                        "pgn": outcome.pgn,
                        "engine_info": trace.engine_info,
                        "logs": {"white": white.stderr_log, "black": black.stderr_log},
                    }
                    write_json(directory / "games" / f"{game['id']}.json", detail)
                    (directory / "games" / f"{game['id']}.pgn").write_text(
                        outcome.pgn, encoding="utf-8"
                    )
                    self.players = []
                    self._save()
            with self.lock:
                # External binaries/assets cannot be frozen cheaply; invalidate if edited.
                for spec in run["engines"].values():
                    if spec["kind"] == "uci":
                        for name, expected in spec["files"].items():
                            if file_hash(Path(name)) != expected:
                                raise ValueError(f"External engine changed during batch: {name}")
                run["status"] = "stopped" if self.stop_requested.is_set() else "completed"
                run["finished_at"] = time.time()
                self._save()
        except Exception as error:
            with self.lock:
                run["status"] = "interrupted" if self.closing.is_set() else "failed"
                run["error"] = f"{type(error).__name__}: {error}"
                if self.live_game is not None and self.live_game["status"] == "running":
                    self.live_game["status"] = "interrupted"
                self._save()
        finally:
            for player in self.players:
                player.stop()
            self.players = []
            if self.closing.is_set():
                self.workspace_lock.close()

    # ── sparring: one game you play yourself, never scored ─────────────────────────────────

    @staticmethod
    def _position(request: dict[str, Any]) -> dict[str, str]:
        fen = request.get("fen", "")
        if fen:
            if not isinstance(fen, str) or len(fen) > 120:
                raise ValueError("A FEN is one short line of text")
            try:
                board = chess.Board(fen.strip())
            except ValueError as error:
                raise ValueError(f"That FEN is not a position: {error}") from None
            if not board.is_valid():
                raise ValueError("No game can continue from that position")
            if board.outcome() is not None:
                raise ValueError("That position is already finished")
            return {
                "id": "custom",
                "name": "Custom position",
                "family": "Pasted FEN",
                "split": "sparring",
                "fen": board.fen(),
            }
        opening_id = request.get("opening", "start")
        if opening_id == "start":
            return {
                "id": "start",
                "name": "Standard start",
                "family": "Standard",
                "split": "sparring",
                "fen": chess.STARTING_FEN,
            }
        openings = {o.id: o for o in catalog()}
        if not isinstance(opening_id, str) or opening_id not in openings:
            raise ValueError("Unknown opening id")
        return openings[opening_id].data()

    def play_start(self, request: dict[str, Any]) -> dict[str, Any]:
        specs = load_registry(self.registry_path)
        engine_id = request.get("engine")
        if not isinstance(engine_id, str) or engine_id not in specs:
            raise ValueError("Choose a registered engine to play")
        spec = specs[engine_id]
        ready, reason = spec.availability()
        if not ready:
            raise ValueError(f"{spec.name}: {reason}")
        colour = request.get("colour", "white")
        if colour not in {"white", "black", "random"}:
            raise ValueError("Choose white, black or random")
        if colour == "random":
            colour = random.choice(["white", "black"])
        base_ms = bounded(request, "base_ms", 60_000, 100, 3_600_000)
        increment_ms = bounded(request, "increment_ms", 500, 0, 60_000)
        ply_cap = bounded(request, "ply_cap", 600, 20, 600)
        opening = self._position(request)
        if chess.Board(opening["fen"]).ply() >= ply_cap:
            raise ValueError("Ply cap must exceed the starting position's ply number")
        with self.lock:
            if self.worker is not None and self.worker.is_alive():
                raise ValueError("An experiment is running; stop it before playing a game")
            if self.sparring is not None:
                self.sparring.close()
            self.sparring = Sparring(
                spec,
                chess.BLACK if colour == "white" else chess.WHITE,
                base_ms,
                increment_ms,
                opening,
                sys.executable,
                ply_cap,
            )
            self.sparring.start()
            return self.sparring.state()

    def _session(self) -> Sparring:
        with self.lock:
            if self.sparring is None:
                raise ValueError("No game in progress; start one first")
            return self.sparring

    def play_state(self) -> dict[str, Any] | None:
        with self.lock:
            return self.sparring.state() if self.sparring is not None else None

    def play_move(self, uci: str) -> dict[str, Any]:
        session = self._session()
        session.play(uci)
        return session.state()

    def play_undo(self) -> dict[str, Any]:
        session = self._session()
        session.undo()
        return session.state()

    def play_resign(self) -> dict[str, Any]:
        session = self._session()
        session.resign()
        return session.state()

    def play_end(self) -> dict[str, Any]:
        with self.lock:
            session, self.sparring = self.sparring, None
        if session is not None:
            session.close()
        return {"play": None}

    def _manifest(self, run_id: str) -> dict[str, Any]:
        if not all(c.isalnum() or c == "-" for c in run_id) or not run_id:
            raise ValueError("Invalid run id")
        path = self.data_dir / run_id / "manifest.json"
        if not path.exists():
            raise ValueError("Experiment not found")
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data

    def state(self, run_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            runs = []
            for path in sorted(self.data_dir.glob("*/manifest.json"), reverse=True)[:100]:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                runs.append({key: manifest[key] for key in ("id", "label", "status", "created_at")})
            run = self._manifest(run_id) if run_id else self.active
            if run is None and runs:
                run = self._manifest(runs[0]["id"])
            return copy.deepcopy(
                {
                    "runs": runs,
                    "run": run,
                    "busy": bool(self.worker and self.worker.is_alive()),
                    "playing": self.sparring is not None,
                }
            )

    def game(self, run_id: str, game_id: str) -> dict[str, Any]:
        with self.lock:
            run = self._manifest(run_id)
            if game_id not in {game["id"] for game in run["games"]}:
                raise ValueError("Game not found")
            path = self.data_dir / run_id / "games" / f"{game_id}.json"
            if path.exists():
                data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
                return data
            if (
                self.active
                and self.active["id"] == run_id
                and self.live_game
                and self.live_game["id"] == game_id
                and self.trace
            ):
                # Only the worker appends immutable frames; taking a slice is atomic in CPython.
                return {
                    **copy.deepcopy(self.live_game),
                    "frames": self.trace.frames[:],
                    "pgn": None,
                }
            game = next(game for game in run["games"] if game["id"] == game_id)
            trace = Trace(
                game["opening"]["fen"], run["limits"]["base_ms"], run["limits"]["increment_ms"]
            )
            return {**game, "frames": trace.frames, "pgn": None}

    def close(self) -> None:
        self.closing.set()
        self.stop()
        if self.sparring is not None:
            self.sparring.close()
            self.sparring = None
        # Shutdown interrupts the active game; it never contributes a score.
        for player in self.players[:]:
            with suppress(OSError, ValueError):
                player.stop()
        if self.worker is not None:
            self.worker.join(timeout=2)
        if self.worker is None or not self.worker.is_alive():
            self.workspace_lock.close()
