import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import chess
import chess.engine
import chess.pgn

from chesslab.experiments.position_probe import board_for, collect_cases, query
from chesslab.players import replay


class PositionProbeTests(unittest.TestCase):
    def test_sampling_keeps_reversible_history_and_ignores_engine_commands(self) -> None:
        board = chess.Board()
        for move in ("g1f3", "g8f6", "f3g1", "f6g8", "e2e4", "e7e5"):
            board.push_uci(move)
        game = chess.pgn.Game.from_board(board)
        game.headers["Result"] = "1/2-1/2"
        game.headers["Termination"] = "ply_cap"
        entry = {
            "id": "g00001",
            "status": "completed",
            "opponent": "stockfish",
            "white": "candidate",
            "black": "stockfish",
            "seed": 1,
            "opening": {"fen": chess.STARTING_FEN},
            "result": "draw",
            "termination": "ply_cap",
            "plies": 6,
        }
        detail = {**entry, "pgn": str(game), "frames": replay(str(game), 1000, 0)}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "games").mkdir()
            (directory / "manifest.json").write_text(
                json.dumps(
                    {
                        "games": [entry],
                        "engines": {"stockfish": {"command": ["never execute this"]}},
                    }
                )
            )
            (directory / "games/g00001.json").write_text(json.dumps(detail))
            cases = collect_cases(directory, "stockfish", [4, 5, 20])
        self.assertEqual(len(cases), 2)
        restored = board_for(cases[0])
        self.assertEqual(len(restored.move_stack), 4)
        self.assertTrue(restored.is_repetition(2))
        self.assertEqual(cases[0]["side_to_move_engine"], "candidate")
        self.assertEqual(cases[1]["side_to_move_engine"], "stockfish")
        cases[0]["fen"] = chess.STARTING_FEN
        with self.assertRaises(ValueError):
            board_for(cases[0])

    def test_scores_are_from_root_side_and_queries_reset_game(self) -> None:
        board = chess.Board()
        board.push_uci("e2e4")
        engine = Mock()
        engine.analyse.return_value = {
            "score": chess.engine.PovScore(chess.engine.Cp(80), chess.WHITE),
            "pv": [chess.Move.from_uci("e7e5")],
            "nodes": 1024,
            "depth": 3,
            "wdl": chess.engine.PovWdl(chess.engine.Wdl(200, 700, 100), chess.WHITE),
        }
        first = query(engine, board, 1000)
        second = query(engine, board, 2000, "e7e5")
        self.assertEqual(first["score_cp"], -80)
        self.assertEqual(first["model_wdl"], [100, 700, 200])
        self.assertEqual(first["requested_nodes"], 1000)
        self.assertEqual(first["nodes"], 1024)
        self.assertEqual(second["restricted_root_move"], "e7e5")
        self.assertIsNot(
            engine.analyse.call_args_list[0].kwargs["game"],
            engine.analyse.call_args_list[1].kwargs["game"],
        )

    def test_rejects_illegal_or_wrong_restricted_root(self) -> None:
        engine = Mock()
        engine.analyse.return_value = {
            "score": chess.engine.PovScore(chess.engine.Mate(3), chess.WHITE),
            "pv": [chess.Move.from_uci("e2e4")],
        }
        with self.assertRaises(ValueError):
            query(engine, chess.Board(), 1000, "d2d4")
        engine.analyse.return_value["pv"] = [chess.Move.from_uci("e2e5")]
        with self.assertRaises(ValueError):
            query(engine, chess.Board(), 1000)


if __name__ == "__main__":
    unittest.main()
