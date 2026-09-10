import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

import chess

from chesslab.lab import Lab
from chesslab.registry import EngineSpec, write_json
from chesslab.sparring import adjudicate
from harness.referee import FAILED_TERMINATIONS

MATE_IN_ONE = "6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1"
STALEMATE_IN_ONE = "7k/8/8/8/8/8/5Q2/K7 w - - 0 1"
NEWCOMER = '''import chess


def get_move(fen: str, time_left_ms: int) -> str:
    return next(iter(chess.Board(fen).legal_moves)).uci()
'''


class AdjudicationTests(unittest.TestCase):
    def test_matches_the_referee_questions(self) -> None:
        board = chess.Board(MATE_IN_ONE)
        self.assertIsNone(adjudicate(board, 600))
        board.push_uci("a1a8")
        self.assertEqual(adjudicate(board, 600), ("white", "checkmate"))
        stalemate = chess.Board(STALEMATE_IN_ONE)
        stalemate.push_uci("f2f7")
        self.assertEqual(adjudicate(stalemate, 600), ("draw", "stalemate"))
        self.assertEqual(adjudicate(chess.Board(), 0), ("draw", "ply_cap"))
        fifty = chess.Board("8/8/8/4k3/8/4K3/8/7R w - - 99 80")
        fifty.push_uci("h1h2")
        self.assertEqual(adjudicate(fifty, 600), ("draw", "fifty_moves"))


class SparringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.registry = self.directory / "engines.json"
        self.lab = Lab(self.directory / "runs", self.registry)

    def tearDown(self) -> None:
        self.lab.close()
        self.temporary.cleanup()

    def settled(self, timeout: float = 25.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.lab.play_state()
            assert state is not None
            if state["status"] != "starting" and not state["thinking"]:
                return state
            time.sleep(0.02)
        raise AssertionError("The sparring game never settled")

    def register_fixture(self, source: str, key: str = "fixture") -> None:
        directory = self.directory / key
        directory.mkdir()
        (directory / "agent.py").write_text(source, encoding="utf-8")
        write_json(
            self.registry,
            {"engines": [EngineSpec(key, key, "Fixture", path=str(directory)).data()]},
        )

    def start(self, **changes: Any) -> dict[str, Any]:
        request: dict[str, Any] = {
            "engine": "greedy",
            "colour": "white",
            "base_ms": 4000,
            "increment_ms": 100,
        }
        request.update(changes)
        return self.lab.play_start(request)

    def test_a_game_you_play_moves_and_takes_back(self) -> None:
        opened = self.start()
        self.assertEqual(opened["white"], "you")
        self.assertEqual(opened["black"], "greedy")
        ready = self.settled()
        self.assertEqual(ready["status"], "playing")
        self.assertTrue(ready["your_turn"])
        self.assertEqual(len(ready["legal"]), 20)

        self.lab.play_move("e2e4")
        replied = self.settled()
        self.assertEqual(len(replied["frames"]) - 1, 2)
        self.assertEqual(replied["frames"][1]["san"], "e4")
        self.assertTrue(replied["your_turn"])
        # Both sides are on a real clock and both are reported as time remaining.
        self.assertEqual(replied["running"], "white")
        self.assertLessEqual(replied["clocks"]["white"], 4100)
        self.assertLessEqual(replied["clocks"]["black"], 4100)
        self.assertGreater(replied["clocks"]["white"], 3000)
        # Your recorded clock is the base, less what the move cost, plus the increment.
        spent = replied["frames"][1]["elapsed_ms"]
        self.assertGreaterEqual(spent, 0)
        self.assertAlmostEqual(replied["frames"][1]["white_ms"], 4100 - spent, places=1)

        taken_back = self.lab.play_undo()
        self.assertEqual(len(taken_back["frames"]) - 1, 0)
        self.assertTrue(taken_back["your_turn"])
        self.assertEqual(chess.Board(taken_back["frames"][-1]["fen"]).fen(), chess.STARTING_FEN)

    def test_a_clock_that_runs_out_loses_the_game(self) -> None:
        self.start(base_ms=150, increment_ms=0)
        self.settled()
        time.sleep(0.3)
        flagged = self.lab.play_state()
        assert flagged is not None
        self.assertEqual(flagged["status"], "finished")
        self.assertEqual(flagged["result"], "black")
        self.assertEqual(flagged["termination"], "flag")
        self.assertEqual(flagged["clocks"]["white"], 0)
        with self.assertRaises(ValueError):
            self.lab.play_move("e2e4")

    def test_your_flag_is_a_draw_when_the_engine_cannot_mate(self) -> None:
        # You have a queen, the engine has a bare king: your flag cannot lose to a side that
        # could never mate you, so the referee's rule makes it a draw.
        self.start(base_ms=150, increment_ms=0, fen="4k3/8/8/8/8/8/8/3QK3 w - - 0 1")
        self.settled()
        time.sleep(0.3)
        flagged = self.lab.play_state()
        assert flagged is not None
        self.assertEqual(flagged["termination"], "flag")
        self.assertEqual(flagged["result"], "draw")

    def test_the_clock_is_only_spent_while_it_is_your_move(self) -> None:
        self.start(base_ms=20_000, increment_ms=0)
        self.settled()
        self.lab.play_move("e2e4")
        self.settled()
        after_one = self.lab.play_state()
        assert after_one is not None
        time.sleep(0.4)
        later = self.lab.play_state()
        assert later is not None
        # Yours keeps ticking because it is your move; the engine's is parked between moves.
        self.assertLess(later["clocks"]["white"], after_one["clocks"]["white"])
        self.assertEqual(later["clocks"]["black"], after_one["clocks"]["black"])
        self.assertEqual(later["running"], "white")

    def test_engine_moves_first_when_you_take_black(self) -> None:
        self.start(colour="black")
        ready = self.settled()
        self.assertEqual(ready["your_colour"], "black")
        self.assertEqual(ready["white"], "greedy")
        self.assertEqual(len(ready["frames"]) - 1, 1)
        self.assertTrue(ready["your_turn"])

    def test_resignation_and_illegal_moves_are_refused(self) -> None:
        self.start()
        self.settled()
        for uci in ("e2e5", "not-a-move", ""):
            with self.subTest(uci=uci), self.assertRaises(ValueError):
                self.lab.play_move(uci)
        resigned = self.lab.play_resign()
        self.assertEqual(resigned["status"], "finished")
        self.assertEqual(resigned["result"], "black")
        self.assertEqual(resigned["termination"], "resignation")
        self.assertIn('[Result "0-1"]', resigned["pgn"])
        with self.assertRaises(ValueError):
            self.lab.play_move("d2d4")

    def test_a_checkmate_you_deliver_ends_the_game(self) -> None:
        self.start(fen=MATE_IN_ONE)
        self.settled()
        finished = self.lab.play_move("a1a8")
        self.assertEqual(finished["status"], "finished")
        self.assertEqual(finished["result"], "white")
        self.assertEqual(finished["termination"], "checkmate")
        self.assertEqual(finished["legal"], [])

    def test_an_engine_that_will_not_start_loses_the_game(self) -> None:
        self.register_fixture('raise RuntimeError("fixture startup failure")\n')
        self.start(engine="fixture")
        failed = self.settled()
        self.assertEqual(failed["status"], "finished")
        self.assertEqual(failed["result"], "white")
        self.assertIn(failed["termination"], FAILED_TERMINATIONS)
        self.assertIn("fixture startup failure", failed["log"])
        with self.assertRaises(ValueError):
            self.lab.play_undo()

    def test_an_illegal_engine_reply_ends_the_game(self) -> None:
        self.register_fixture('def get_move(fen, time_left_ms):\n    return "a1a8"\n')
        self.start(engine="fixture")
        self.settled()
        self.lab.play_move("e2e4")
        failed = self.settled()
        self.assertEqual(failed["status"], "finished")
        self.assertEqual(failed["result"], "white")
        self.assertEqual(failed["termination"], "illegal")

    def test_openings_and_pasted_positions(self) -> None:
        self.start(opening="italian")
        italian = self.settled()
        self.assertEqual(italian["opening"]["name"], "Italian game")
        self.lab.play_end()
        self.start(fen=MATE_IN_ONE)
        pasted = self.settled()
        self.assertEqual(pasted["opening"]["id"], "custom")
        self.assertEqual(pasted["frames"][0]["fen"], MATE_IN_ONE)

    def test_invalid_requests_are_refused_before_the_engine_starts(self) -> None:
        cases: list[dict[str, Any]] = [
            {"engine": "absent"},
            {"engine": 4},
            {"colour": "green"},
            {"base_ms": 0},
            {"increment_ms": True},
            {"fen": "not a fen"},
            {"fen": "8/8/8/8/8/8/8/8 w - - 0 1"},
            {"fen": "7k/5Q2/5K2/8/8/8/8/8 b - - 0 1"},
            {"opening": "no-such-opening"},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.start(**changes)
        self.assertIsNone(self.lab.play_state())

    def test_a_batch_and_a_game_never_share_the_core(self) -> None:
        self.start()
        self.settled()
        batch = {
            "candidate": "random",
            "opponents": ["greedy"],
            "openings": ["italian"],
            "base_ms": 1000,
            "ply_cap": 20,
        }
        with self.assertRaises(ValueError):
            self.lab.start(batch)
        self.lab.play_resign()
        # A finished game is cleared out of the way instead of blocking the batch.
        run_id = self.lab.start(batch)
        assert self.lab.worker is not None
        with self.assertRaises(ValueError):
            self.start()
        self.lab.worker.join(timeout=30)
        self.assertEqual(self.lab.state(run_id)["run"]["status"], "completed")
        self.assertFalse(self.lab.state(run_id)["playing"])


class RegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.registry = self.directory / "engines.json"
        self.lab = Lab(self.directory / "runs", self.registry)
        self.agent = self.directory / "newcomer"
        self.agent.mkdir()
        (self.agent / "agent.py").write_text(NEWCOMER, encoding="utf-8")

    def tearDown(self) -> None:
        self.lab.close()
        self.temporary.cleanup()

    def spec(self, **changes: Any) -> dict[str, Any]:
        request: dict[str, Any] = {
            "id": "newcomer",
            "name": "Newcomer",
            "family": "Bench",
            "kind": "python",
            "path": str(self.agent),
        }
        request.update(changes)
        return request

    def test_an_agent_added_here_becomes_an_opponent(self) -> None:
        catalog = self.lab.register(self.spec(description="Added from the browser"))
        added = next(e for e in catalog["engines"] if e["id"] == "newcomer")
        self.assertTrue(added["available"])
        self.assertTrue(added["custom"])
        self.assertEqual(added["description"], "Added from the browser")
        self.assertFalse(next(e for e in catalog["engines"] if e["id"] == "greedy")["custom"])
        # It is in the registry file, not just this process, so a restart still sees it.
        reopened = Lab(self.directory / "restarted", self.registry)
        try:
            self.assertIn("newcomer", {e["id"] for e in reopened.catalog()["engines"]})
        finally:
            reopened.close()

    def test_editing_an_entry_replaces_it_instead_of_duplicating(self) -> None:
        self.lab.register(self.spec())
        catalog = self.lab.register(self.spec(name="Renamed"))
        matches = [e for e in catalog["engines"] if e["id"] == "newcomer"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["name"], "Renamed")

    def test_removing_only_works_on_what_you_added(self) -> None:
        self.lab.register(self.spec())
        catalog = self.lab.forget("newcomer")
        self.assertNotIn("newcomer", {e["id"] for e in catalog["engines"]})
        for engine_id in ("greedy", "absent"):
            with self.subTest(engine_id=engine_id), self.assertRaises(ValueError):
                self.lab.forget(engine_id)

    def test_a_spec_that_would_not_run_is_refused(self) -> None:
        cases: list[dict[str, Any]] = [
            {"path": str(self.directory / "nowhere")},
            {"id": "Not An Id"},
            {"name": ""},
            {"kind": "quantum"},
            {"kind": "uci", "command": []},
            {"kind": "uci", "command": ["definitely-not-an-engine-binary"]},
            {"includes": ["../escape"]},
            {"max_move_ms": 0},
            {"colour": "white"},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.lab.register(self.spec(**changes))
        self.assertEqual([e for e in self.lab.catalog()["engines"] if e["custom"]], [])

    def test_an_engine_in_a_game_cannot_be_removed(self) -> None:
        self.lab.register(self.spec())
        self.lab.play_start({"engine": "newcomer", "colour": "white", "base_ms": 2000})
        with self.assertRaises(ValueError):
            self.lab.forget("newcomer")
        self.lab.play_end()
        self.assertNotIn("newcomer", {e["id"] for e in self.lab.forget("newcomer")["engines"]})


if __name__ == "__main__":
    unittest.main()
