import io
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

import chess
import chess.pgn

from chesslab.compare import compare
from chesslab.lab import Lab, schedule
from chesslab.openings import catalog
from chesslab.players import Trace, UCIAgent, replay
from chesslab.registry import ROOT, EngineSpec, freeze, write_json
from chesslab.server import Server, export_run
from chesslab.statistics import summarise
from harness.package import members
from harness.sandbox import AgentFailure

FIXTURE = Path(__file__).with_name("uci_fixture.py")


class ExperimentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.registry = self.directory / "engines.json"
        self.lab = Lab(self.directory / "runs", self.registry)

    def tearDown(self) -> None:
        self.lab.close()
        self.temporary.cleanup()

    def run_batch(self, **changes: Any) -> dict[str, Any]:
        config: dict[str, Any] = {
            "candidate": "random",
            "opponents": ["greedy"],
            "openings": ["italian"],
            "base_ms": 1000,
            "increment_ms": 0,
            "ply_cap": 20,
            "seed": 7,
        }
        config.update(changes)
        key = self.lab.start(config)
        assert self.lab.worker is not None
        self.lab.worker.join(timeout=20)
        self.assertFalse(self.lab.worker.is_alive(), "Experiment did not finish")
        return self.lab.state(key)["run"]  # type: ignore[no-any-return]

    def register_fixture(self, source: str, key: str = "fixture") -> None:
        directory = self.directory / key
        directory.mkdir()
        (directory / "agent.py").write_text(source, encoding="utf-8")
        write_json(
            self.registry,
            {"engines": [EngineSpec(key, key, "Fixture", path=str(directory)).data()]},
        )

    def test_real_games_persist_and_replay(self) -> None:
        run = self.run_batch()
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["summary"][0]["pairs"], 1)
        for item in run["games"]:
            game = self.lab.game(run["id"], item["id"])
            pgn = chess.pgn.read_game(io.StringIO(game["pgn"]))
            assert pgn is not None
            self.assertFalse(pgn.errors)
            self.assertEqual(pgn.end().board().fen(), game["frames"][-1]["fen"])
            self.assertEqual(len(game["frames"]), item["plies"] + 1)
        self.assertEqual(len(run["engines"]["random"]["sha256"]), 64)
        with zipfile.ZipFile(io.BytesIO(export_run(self.lab, run["id"]))) as archive:
            self.assertIn("manifest.json", archive.namelist())
            self.assertIn("results.csv", archive.namelist())
            self.assertIn("games.pgn", archive.namelist())

    def test_illegal_move_loses_both_colours(self) -> None:
        self.register_fixture('def get_move(fen, time_left_ms):\n    return "a1a8"\n')
        run = self.run_batch(candidate="fixture")
        self.assertEqual(run["summary"][0]["losses"], 2)
        self.assertEqual(run["summary"][0]["candidate_failures"], 2)
        self.assertTrue(all(g["termination"] == "illegal" for g in run["games"]))

    def test_init_crash_logs_survive(self) -> None:
        self.register_fixture('raise RuntimeError("fixture startup failure")\n')
        run = self.run_batch(candidate="fixture")
        self.assertEqual(run["summary"][0]["losses"], 2)
        first = run["games"][0]
        game = self.lab.game(run["id"], first["id"])
        self.assertIn("fixture startup failure", game["logs"]["white"])

    def test_uci_adapter_against_python(self) -> None:
        spec = EngineSpec(
            "uci-test",
            "UCI fixture",
            "Fixture",
            kind="uci",
            command=[sys.executable, str(FIXTURE)],
            assets=[str(FIXTURE)],
        )
        write_json(self.registry, {"engines": [spec.data()]})
        run = self.run_batch(opponents=["uci-test"])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["summary"][0]["pairs"], 1)
        game = self.lab.game(run["id"], run["games"][0]["id"])
        self.assertEqual(game["engine_info"]["uci-test"]["id"]["name"], "Chess Lab test fixture")
        self.assertEqual(game["engine_info"]["uci-test"]["options"]["Threads"], 1)

    def test_invalid_experiment_rejected_before_run(self) -> None:
        cases: list[dict[str, Any]] = [
            {"opponents": []},
            {"opponents": ["random"]},
            {"opponents": ["greedy", "greedy"]},
            {"openings": ["italian", "caro"]},
            {"base_ms": True},
            {"seed": -1},
            {"openings": ["italian", "italian"]},
            {"opponents": ["absent"]},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.lab.start(
                    {
                        "candidate": "random",
                        "opponents": ["greedy"],
                        "openings": ["italian"],
                        **changes,
                    }
                )

    def test_one_writer_per_results_directory(self) -> None:
        with self.assertRaises(ValueError):
            Lab(self.directory / "runs", self.registry)

    def test_stop_before_games_does_not_score_queued_games(self) -> None:
        # Holding the lock prevents the worker passing preparation before stop is set.
        with self.lab.lock:
            key = self.lab.start(
                {"candidate": "random", "opponents": ["greedy"], "openings": ["italian"]}
            )
            self.lab.stop()
        assert self.lab.worker is not None
        self.lab.worker.join(timeout=10)
        run = self.lab.state(key)["run"]
        self.assertEqual(run["status"], "stopped")
        self.assertEqual(run["summary"][0]["games"], 0)

    def test_path_traversal_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.lab.state("../secret")

    def test_loopback_api_requires_token_for_mutations(self) -> None:
        server = Server(0, self.lab)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(url + "/api/catalog") as response:
                catalog_data = json.load(response)
            with self.assertRaises(urllib.error.HTTPError) as failure:
                urllib.request.urlopen(urllib.request.Request(url + "/api/stop", b"{}"))
            self.assertEqual(failure.exception.code, 403)
            request = urllib.request.Request(
                url + "/api/stop", b"{}", {"X-CSRF-Token": catalog_data["token"]}
            )
            with urllib.request.urlopen(request) as response:
                self.assertTrue(json.load(response)["ok"])
            for route in ("/", "/app.js", "/style.css", "/pieces/K.svg"):
                with urllib.request.urlopen(url + route) as response:
                    self.assertEqual(response.status, 200)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


class IntegrityTests(unittest.TestCase):
    def comparison_run(self) -> dict[str, Any]:
        games = schedule("candidate", ["random"], catalog()[:2], 1)
        for game in games:
            game.update(status="completed", result="draw", termination="threefold_repetition")
        return {
            "id": "test",
            "status": "completed",
            "candidate": "candidate",
            "games": games,
            "split": "dev",
            "engines": {"random": {"sha256": "fixed-opponent-hash"}},
            "limits": {"base_ms": 1000, "increment_ms": 0, "ply_cap": 600},
            "environment": {"machine": "test-machine"},
            "harness_sha256": {"a": "b"},
            "lab_sha256": {"a": "b"},
        }

    def test_comparison_matches_complete_pairs(self) -> None:
        baseline, candidate = self.comparison_run(), self.comparison_run()
        candidate["games"][0]["result"] = "white"
        candidate["games"][1]["result"] = "black"
        result = compare(baseline, candidate)
        self.assertEqual(result["opponents"][0]["matched_pairs"], 2)
        self.assertEqual(result["opponents"][0]["score_delta"], 0.25)

    def test_comparison_rejects_different_budget_or_evaluator(self) -> None:
        for key in ("limits", "environment", "lab_sha256", "harness_sha256"):
            baseline, candidate = self.comparison_run(), self.comparison_run()
            if key == "limits":
                candidate[key]["base_ms"] += 1
            else:
                candidate[key]["changed"] = True
            with self.subTest(key=key), self.assertRaises(ValueError):
                compare(baseline, candidate)

    def test_comparison_rejects_changed_opponent_build(self) -> None:
        baseline, candidate = self.comparison_run(), self.comparison_run()
        candidate["engines"]["random"]["sha256"] = "different"
        with self.assertRaises(ValueError):
            compare(baseline, candidate)

    def test_interrupted_runs_are_not_comparable(self) -> None:
        baseline, candidate = self.comparison_run(), self.comparison_run()
        candidate["status"] = "interrupted"
        with self.assertRaises(ValueError):
            compare(baseline, candidate)

    def test_openings_valid_and_families_do_not_cross_splits(self) -> None:
        openings = catalog()
        self.assertEqual(len(openings), 32)
        self.assertTrue(all(chess.Board(o.fen).is_valid() for o in openings))
        dev = {o.family for o in openings if o.split == "dev"}
        validation = {o.family for o in openings if o.split == "validation"}
        self.assertFalse(dev & validation)

    def test_pairs_swap_colour_share_fen_and_seed(self) -> None:
        games = schedule("candidate", ["random", "greedy"], catalog()[:3], 17)
        self.assertEqual(games, schedule("candidate", ["random", "greedy"], catalog()[:3], 17))
        self.assertEqual(len(games), 12)
        for first, second in zip(games[::2], games[1::2], strict=True):
            self.assertEqual(first["white"], second["black"])
            self.assertEqual(first["black"], second["white"])
            self.assertEqual(first["opening"], second["opening"])
            self.assertEqual(first["seed"], second["seed"])

    def test_frozen_build_survives_source_edit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            root = directory / "source"
            root.mkdir()
            source = root / "agent.py"
            source.write_text('def get_move(fen, time_left_ms): return "e2e4"\n')
            spec = EngineSpec("a", "A", "Fixture", path=str(root))
            first = freeze(spec, directory / "first")
            source.write_text('def get_move(fen, time_left_ms): return "d2d4"\n')
            second = freeze(spec, directory / "second")
            self.assertNotEqual(first["sha256"], second["sha256"])
            self.assertIn("e2e4", (directory / "first" / "agent.py").read_text())

    def test_research_tools_are_not_in_submission(self) -> None:
        names = [name for _, name in members(ROOT, ("weights",))]
        self.assertIn("agent.py", names)
        self.assertFalse(any(name.startswith(("chesslab", ".chesslab")) for name in names))

    def test_partial_and_void_pairs_are_not_scored(self) -> None:
        games = schedule("candidate", ["random"], catalog()[:2], 1)
        games[0].update(status="completed", result="white", termination="checkmate")
        self.assertEqual(summarise(games, "candidate")[0]["pairs"], 0)
        games[1].update(status="completed", result="void", termination="both_failed")
        games[2].update(status="completed", result="draw", termination="ply_cap")
        games[3].update(status="completed", result="draw", termination="ply_cap")
        summary = summarise(games, "candidate")[0]
        self.assertEqual(summary["pairs"], 1)
        self.assertEqual(summary["void"], 1)
        self.assertEqual(summary["score"], 0.5)
        self.assertEqual(summary["interval"], [0, 1])

    def test_replay_promotion_castling_and_en_passant(self) -> None:
        cases = (
            ("7k/P7/8/8/8/8/8/7K w - - 0 1", "a7a8q"),
            ("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1"),
            ("7k/8/8/3pP3/8/8/8/7K w - d6 0 2", "e5d6"),
        )
        for fen, uci in cases:
            with self.subTest(uci=uci):
                board = chess.Board(fen)
                board.push_uci(uci)
                game = chess.pgn.Game.from_board(board)
                game.end().set_clock(0.8)
                frames = replay(str(game), 1000, 100)
                self.assertEqual(frames[-1]["fen"], board.fen())
                self.assertEqual(frames[-1]["white_ms"], 800)
                self.assertEqual(frames[-1]["elapsed_ms"], 300)

    def test_trace_rejects_illegal_and_flagged_moves(self) -> None:
        trace = Trace(chess.STARTING_FEN, 1000, 0)
        trace.accept(chess.STARTING_FEN, "a1a8", 1000, 2)
        trace.accept(chess.STARTING_FEN, "e2e4", 1000, 1001)
        self.assertEqual(len(trace.frames), 1)

    def test_uci_hard_watchdog(self) -> None:
        trace = Trace(chess.STARTING_FEN, 100, 0)
        spec = EngineSpec(
            "hang",
            "Hung fixture",
            "Fixture",
            kind="uci",
            command=[sys.executable, str(FIXTURE), "--hang"],
        )
        agent = UCIAgent(spec.data(), trace)
        started = time.monotonic()
        try:
            agent.start(5)
            with self.assertRaises(AgentFailure) as failure:
                agent.move(chess.STARTING_FEN, 100)
            self.assertEqual(failure.exception.reason, "flag")
            self.assertLess(time.monotonic() - started, 4)
        finally:
            agent.stop()


if __name__ == "__main__":
    unittest.main()
