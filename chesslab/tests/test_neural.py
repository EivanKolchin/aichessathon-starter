"""Independent dense inference, colour symmetry and training/search integration checks."""

import json
import random
import tempfile
import unittest
from pathlib import Path

import chess
import numpy as np

from a1.board import from_board
from a1.evaluation import evaluate
from a1.neural import INPUTS, Weights, create_evaluator, features, load_weights, predict
from a1.search import Search, SearchConfig
from chesslab.experiments.train_neural import fit, train
from chesslab.tests.test_a1 import SPECIAL_FENS


class NeuralTests(unittest.TestCase):
    def test_training_rejects_cross_split_position_leakage(self) -> None:
        rows = [
            {
                "split": split,
                "opening_group": split,
                "fen": chess.STARTING_FEN,
                "teacher": {"score_cp": 0},
            }
            for split in ("train", "validation", "test")
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "dataset.json").write_text(json.dumps({"positions": rows}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "leaks"):
                train(path / "dataset.json", path / "out", 4, 1, 1)
            self.assertFalse((path / "out").exists())

    def test_sparse_compiled_inference_matches_dense_and_mirrors(self) -> None:
        rng = np.random.default_rng(15)
        weights = Weights(
            rng.normal(0, 0.1, (INPUTS, 8)),
            rng.normal(0, 0.1, 8),
            rng.normal(0, 0.2, 8),
            np.array([0.1]),
        )
        evaluator = create_evaluator(weights)
        boards = [chess.Board(fen) for fen in SPECIAL_FENS]
        reference = chess.Board()
        choices = random.Random(77)
        for _ in range(80):
            if reference.is_game_over():
                break
            boards.append(reference.copy())
            reference.push(choices.choice(list(reference.legal_moves)))
        for reference in boards:
            board, state = from_board(reference)
            base = evaluate(board, state)
            expected = round(float(predict(weights, features(reference), np.array(base))))
            self.assertEqual(evaluator(board, state), expected, reference.fen())
            mirrored = reference.mirror()
            np.testing.assert_array_equal(features(reference), features(mirrored))
            self.assertEqual(evaluator(*from_board(mirrored)), expected, reference.fen())

    def test_zero_residual_preserves_search_and_input(self) -> None:
        weights = Weights(np.zeros((INPUTS, 4)), np.zeros(4), np.zeros(4), np.zeros(1))
        config = SearchConfig(max_depth=3, aspiration=False)
        board = chess.Board()
        plain = Search(config).analyse(board, 60000, 60000)
        hybrid = Search(config, create_evaluator(weights)).analyse(board, 60000, 60000)
        self.assertEqual(
            (plain.move, plain.score, plain.nodes), (hybrid.move, hybrid.score, hybrid.nodes)
        )
        self.assertEqual(board.fen(), chess.STARTING_FEN)
        self.assertIs(create_evaluator(weights, 0), evaluate)

    def test_model_roundtrip_validation_and_immutable_evaluator(self) -> None:
        weights = Weights(np.zeros((INPUTS, 4)), np.ones(4), np.zeros(4), np.array([0.1]))
        evaluator = create_evaluator(weights)
        board, state = from_board(chess.Board())
        before = evaluator(board, state)
        weights.b2[0] = -0.1
        self.assertEqual(evaluator(board, state), before)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.npz"
            weights.save(path)
            reloaded = load_weights(path)
            np.testing.assert_array_equal(weights.b2, reloaded.b2)
            with self.assertRaises(FileExistsError):
                weights.save(path)
            with self.assertRaises(ValueError):
                create_evaluator(weights, float("nan"))
            weights.w1[0, 0] = float("nan")
            with self.assertRaises(ValueError):
                create_evaluator(weights)

    def test_training_learns_synthetic_signal_reproducibly(self) -> None:
        rng = np.random.default_rng(62)
        x = np.zeros((192, INPUTS))
        x[:, :8] = rng.integers(0, 2, (192, 8))
        base = np.zeros(192)
        target = 70 * (x[:, 0] - x[:, 1]) + 40 * x[:, 2] * x[:, 3]
        validation = x[128:], base[128:], target[128:]
        arguments = (x[:128], base[:128], target[:128], validation)
        first, _ = fit(*arguments, hidden=8, epochs=80, seed=2)
        second, _ = fit(*arguments, hidden=8, epochs=80, seed=2)
        np.testing.assert_array_equal(first.w1, second.w1)
        prediction = predict(first, validation[0], validation[1])
        self.assertLess(
            float(np.mean((prediction - validation[2]) ** 2)),
            float(np.mean(validation[2] ** 2)) / 5,
        )


if __name__ == "__main__":
    unittest.main()
