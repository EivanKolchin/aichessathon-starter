import copy
import unittest
from typing import Any

import chess
import chess.pgn

from chesslab.experiments.audit_run import verify_game
from chesslab.players import replay


class AuditTests(unittest.TestCase):
    def fixture(self) -> dict[str, Any]:
        board = chess.Board("7k/8/5KQ1/8/8/8/8/8 w - - 0 1")
        board.push_uci("g6g7")
        game = chess.pgn.Game.from_board(board)
        game.headers["Termination"] = "checkmate"
        game.end().set_clock(0.9)
        return {
            "opening": {"fen": game.board().fen()},
            "result": "white",
            "termination": "checkmate",
            "plies": 1,
            "pgn": str(game),
            "frames": replay(str(game), 1000, 0),
        }

    def test_replays_legal_checkmate(self) -> None:
        self.assertTrue(verify_game(self.fixture()).is_checkmate())

    def test_rejects_tampered_frame_and_outcome(self) -> None:
        for field, value in (("fen", chess.STARTING_FEN), ("uci", "g6g8"), ("san", "Qg8")):
            detail = self.fixture()
            detail["frames"][-1][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify_game(detail)
        detail = self.fixture()
        detail["result"] = "black"
        with self.assertRaises(ValueError):
            verify_game(detail)

    def test_drawn_flag_uses_unplayed_side_and_retains_legal_history(self) -> None:
        # The supplied run had precisely this bare-king versus two-queens ending.
        board = chess.Board("8/6K1/8/4k3/8/8/pp6/3q1q2 b - - 1 66")
        game = chess.pgn.Game.from_board(board)
        game.headers["Result"] = "1/2-1/2"
        game.headers["Termination"] = "flag"
        detail = copy.deepcopy(self.fixture())
        detail.update(
            opening={"fen": board.fen()},
            result="draw",
            termination="flag",
            plies=0,
            pgn=str(game),
            frames=replay(str(game), 108, 100),
        )
        self.assertEqual(verify_game(detail).turn, chess.BLACK)


if __name__ == "__main__":
    unittest.main()
