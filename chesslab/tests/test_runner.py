"""The runner's side of a hosted experiment, and of a hosted game.

Nothing here talks to a Worker or starts an engine. What is worth pinning down is the part the
runner decides on its own: that one machine plays one thing at a time, that an instruction from
the site is applied exactly once, and that a board is torn down when the session behind it is
gone. The engine and the referee are covered by the lab's own tests.
"""

import http.client
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from typing import Any

from chesslab import ladder
from chesslab.ladder import Endpoint
from chesslab.runner import Runner


class FakeSource:
    """Answers the runner the way the Worker would, and remembers what it was asked."""

    url = "https://ladder.example"
    token = ""

    def __init__(self) -> None:
        self.answers: dict[str, list[Any]] = {"/api/catalog": [{"agents": []}]}
        self.calls: list[tuple[str, Any]] = []

    def reply(self, path: str, *answers: Any) -> None:
        self.answers.setdefault(path, []).extend(answers)

    def json(
        self, path: str, method: str = "GET", body: bytes | None = None, mime: str = ""
    ) -> Any:
        self.calls.append((path, body))
        queued = self.answers.get(path)
        if not queued:
            return {}
        return queued.pop(0) if len(queued) > 1 else queued[0]

    def posted(self, path: str) -> list[Any]:
        return [body for called, body in self.calls if called == path]


class FakeLab:
    """A board that records what was done to it and reports whatever it is told to."""

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.state_now = state
        self.did: list[Any] = []
        self.registry_path = scratch() / "engines.json"

    def catalog(self) -> dict[str, Any]:
        return {"engines": [], "openings": []}

    def play_start(self, request: dict[str, Any]) -> dict[str, Any]:
        self.did.append(("start", request))
        self.state_now = {
            "status": "playing", "thinking": False, "your_turn": True,
            "result": None, "frames": [{}],
        }
        return self.state_now

    def play_move(self, uci: str) -> None:
        self.did.append(("move", uci))
        assert self.state_now is not None
        self.state_now["frames"] = [*self.state_now["frames"], {}]

    def play_undo(self) -> None:
        self.did.append(("undo", None))

    def play_resign(self) -> None:
        self.did.append(("resign", None))

    def play_end(self) -> dict[str, Any]:
        self.did.append(("end", None))
        self.state_now = None
        return {"play": None}

    def play_state(self) -> dict[str, Any] | None:
        return self.state_now


# Whatever a test pulls lands here, never in the repository.
SCRATCH: tempfile.TemporaryDirectory[str] | None = None


def setUpModule() -> None:
    global SCRATCH
    SCRATCH = tempfile.TemporaryDirectory(prefix="chesslab-runner-tests-")


def tearDownModule() -> None:
    assert SCRATCH is not None
    SCRATCH.cleanup()


def scratch() -> Path:
    assert SCRATCH is not None
    return Path(SCRATCH.name)


def make_runner(source: FakeSource, lab: FakeLab) -> Runner:
    return Runner(source, lab, "devbox", scratch() / "uploads")  # type: ignore[arg-type]


class SparringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = FakeSource()
        self.lab = FakeLab()
        self.runner = make_runner(self.source, self.lab)

    def offer(self, **session: Any) -> None:
        self.source.reply("/api/play/claim", {"play": {"id": "20260101000000-abcdef", **session}})

    def test_a_claimed_session_starts_the_game_here_and_reports_the_board(self) -> None:
        self.offer(request={"engine": "random", "colour": "white"}, command=None)
        self.runner.attend()
        wanted = {"engine": "random", "colour": "white"}
        self.assertEqual(self.lab.did, [("end", None), ("start", wanted)])
        posted = self.source.posted("/api/play/20260101000000-abcdef/state")
        self.assertEqual(len(posted), 1)
        self.assertIn(b'"status": "live"', posted[0])

    def test_an_instruction_is_applied_once_and_only_once(self) -> None:
        self.offer(request={"engine": "random"}, command={"kind": "move", "uci": "e2e4"})
        self.runner.attend()
        # The Worker hands the instruction over and clears it, so the next look carries none.
        self.source.answers["/api/play/claim"] = [
            {"play": {"id": "20260101000000-abcdef", "request": {"engine": "random"},
                      "command": None}}
        ]
        self.runner.attend()
        self.assertEqual([done for done, _ in self.lab.did].count("move"), 1)

    def test_a_refused_move_leaves_the_game_standing(self) -> None:
        self.offer(request={"engine": "random"}, command=None)
        self.runner.attend()

        def refuse(uci: str) -> None:
            raise ValueError("Not a legal move")

        self.lab.play_move = refuse  # type: ignore[method-assign]
        self.source.answers["/api/play/claim"] = [
            {"play": {"id": "20260101000000-abcdef", "request": {"engine": "random"},
                      "command": {"kind": "move", "uci": "a1a8"}}}
        ]
        self.runner.attend()
        self.assertEqual(self.runner.session, "20260101000000-abcdef")
        self.assertIsNotNone(self.lab.play_state())

    def test_a_session_that_is_gone_takes_the_board_down_with_it(self) -> None:
        self.offer(request={"engine": "random"}, command=None)
        self.runner.attend()
        self.source.answers["/api/play/claim"] = [{"play": None}]
        self.runner.attend()
        self.assertIsNone(self.runner.session)
        self.assertIsNone(self.lab.play_state())
        self.assertFalse(self.runner.occupied)

    def test_an_unfinished_game_keeps_the_machine_to_itself(self) -> None:
        self.offer(request={"engine": "random"}, command=None)
        self.source.reply("/api/jobs/claim", {"job": {"run_id": "r1", "request": {}}})
        taken: list[Any] = []
        self.runner.play = lambda job: taken.append(job)  # type: ignore[method-assign]
        self.runner.serve(once=True)
        self.assertEqual(taken, [], "an experiment took the machine out from under a live game")

    def test_a_finished_game_hands_the_machine_back(self) -> None:
        self.offer(request={"engine": "random"}, command=None)
        self.source.reply("/api/jobs/claim", {"job": {"run_id": "r1", "request": {}}})
        self.runner.attend()
        # The person is still looking at the final position; the machine is free either way.
        assert self.lab.state_now is not None
        self.lab.state_now.update(status="finished", your_turn=False, result="white")
        self.runner.reported_at = 0.0
        self.runner.attend()
        self.assertFalse(self.runner.occupied)
        taken: list[Any] = []
        self.runner.play = lambda job: taken.append(job)  # type: ignore[method-assign]
        self.runner.serve(once=True)
        self.assertEqual([job["run_id"] for job in taken], ["r1"])
        self.assertIsNone(self.runner.session, "the game was left before the batch started")

    def test_the_last_word_on_a_finished_game_says_it_is_over(self) -> None:
        self.offer(request={"engine": "random"}, command=None)
        self.runner.attend()
        assert self.lab.state_now is not None
        self.lab.state_now.update(status="finished", result="black")
        self.runner.reported_at = 0.0
        self.runner.attend()
        posted = self.source.posted("/api/play/20260101000000-abcdef/state")
        self.assertIn(b'"status": "over"', posted[-1])


class CatalogueTests(unittest.TestCase):
    def test_a_catalogue_that_answers_with_nonsense_is_a_message_not_a_crash(self) -> None:
        source = FakeSource()
        source.answers["/api/catalog"] = [{"unexpected": True}]
        with self.assertRaises(ValueError) as caught:
            ladder.pull(
                source,  # type: ignore[arg-type]
                scratch() / "uploads",
                scratch() / "engines.json",
                scratch() / "ladder.json",
            )
        self.assertIn("did not answer with a catalogue", str(caught.exception))

    def test_an_idle_machine_goes_and_gets_what_was_uploaded_since(self) -> None:
        source, lab = FakeSource(), FakeLab()
        runner = make_runner(source, lab)
        pulls: list[Any] = []
        with unittest.mock.patch("chesslab.runner.ladder.pull", lambda *a: pulls.append(a)):
            runner.serve(once=True)
            self.assertEqual(len(pulls), 1)
            runner.serve(once=True)          # too soon; the catalogue has not moved on
            self.assertEqual(len(pulls), 1)
            runner.pulled = 0.0
            runner.serve(once=True)
        self.assertEqual(len(pulls), 2)

    def test_an_unreachable_catalogue_does_not_stop_the_runner(self) -> None:
        source, lab = FakeSource(), FakeLab()
        runner = make_runner(source, lab)

        def refuse(*args: Any) -> None:
            raise ValueError("Could not reach https://ladder.example")

        with unittest.mock.patch("chesslab.runner.ladder.pull", refuse):
            runner.serve(once=True)
        self.assertIsNone(runner.session)


class WeatherTests(unittest.TestCase):
    """A runner is left up for hours; the network under it is not that reliable."""

    def test_a_dropped_connection_is_a_failed_request_not_a_crash(self) -> None:
        dropped = http.client.RemoteDisconnected("Remote end closed connection without response")
        with (
            unittest.mock.patch("urllib.request.urlopen", side_effect=dropped),
            self.assertRaises(ValueError) as caught,
        ):
            Endpoint("https://ladder.example", "t").json("/api/runs")
        self.assertIn("Could not reach", str(caught.exception))

    def test_a_ladder_that_blinks_is_a_wait_not_the_end_of_the_run(self) -> None:
        source, lab = FakeSource(), FakeLab()
        runner = make_runner(source, lab)

        def refuse(*args: Any, **kwargs: Any) -> Any:
            raise ValueError("Could not reach https://ladder.example: [Errno 104] reset")

        source.json = refuse  # type: ignore[method-assign]
        runner.serve(once=True)          # would have taken the process down with it
        self.assertIsNone(runner.session)


if __name__ == "__main__":
    unittest.main()
