"""Differential oracle checks for the original compiled backend."""

import random
import time
import unittest

import chess
import numpy as np

from a0.evaluation import EvalState, PawnCache
from a1.board import (
    BK,
    EP,
    FULL,
    HALF,
    MAX_MOVES,
    TURN,
    UNDO_SIZE,
    WK,
    attacked,
    decode,
    encode,
    from_board,
    generate,
    identity,
    insufficient,
    make,
    perft,
    unmake,
)
from a1.evaluation import evaluate
from a1.search import (
    NODE_LIMIT,
    ROOT_MOVE,
    STOP,
    Search,
    SearchConfig,
    negamax,
    repeated,
    warmup,
)

SPECIAL_FENS = (
    "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1",
    "r6k/1P6/8/8/8/8/8/7K w - - 0 1",
    "7k/P7/8/8/8/8/8/r6K w - - 0 1",
    "7k/8/8/3pP3/8/8/8/7K w - d6 0 2",
    "4r2k/8/8/3pP3/8/8/8/4K3 w - d6 0 2",
    "8/8/8/r4pPK/8/8/8/7k w - f6 0 1",
    "4k3/8/8/8/8/8/8/r3K2R w K - 0 1",
    "4k3/8/8/8/8/8/8/2r1K2R w K - 0 1",
    "4k3/8/8/8/8/8/8/5rKR w - - 0 1",
    "7k/6Q1/6K1/8/8/8/8/8 b - - 100 1",
    "7k/5Q2/6K1/8/8/8/8/8 b - - 0 1",
)


class CompiledBoardTests(unittest.TestCase):
    def check_position(self, reference: chess.Board) -> None:
        self.assertTrue(reference.is_valid(), reference.fen())
        board, state = from_board(reference)
        before_board, before_state = board.copy(), state.copy()
        moves = np.zeros(MAX_MOVES, dtype=np.int64)
        undo = np.zeros(UNDO_SIZE, dtype=np.int64)
        count = generate(board, state, moves, undo)
        actual = [decode(int(move)) for move in moves[:count]]
        self.assertEqual(set(actual), set(reference.legal_moves), reference.fen())
        self.assertEqual(len(actual), len(set(actual)))
        self.assertTrue(np.array_equal(board, before_board))
        self.assertTrue(np.array_equal(state, before_state))
        expected_eval = EvalState.from_board(reference).evaluate(reference, PawnCache())
        self.assertEqual(evaluate(board, state), expected_eval, reference.fen())
        self.assertEqual(insufficient(board), reference.is_insufficient_material())
        for color in (chess.WHITE, chess.BLACK):
            for square in range(64):
                cell = (square // 8) * 16 + square % 8
                self.assertEqual(
                    attacked(board, cell, 1 if color else -1),
                    reference.is_attacked_by(color, square),
                    (reference.fen(), color, square),
                )
        for move in actual:
            encoded = encode(move)
            self.assertEqual(decode(encoded), move)
            make(board, state, encoded, undo)
            reference.push(move)
            rebuilt_board, rebuilt_state = from_board(reference)
            self.assertTrue(np.array_equal(board, rebuilt_board), move.uci())
            self.assertTrue(np.array_equal(state, rebuilt_state), move.uci())
            self.assertEqual(
                evaluate(board, state),
                EvalState.from_board(reference).evaluate(reference, PawnCache()),
            )
            reference.pop()
            unmake(board, state, encoded, undo)
            self.assertTrue(np.array_equal(board, before_board), move.uci())
            self.assertTrue(np.array_equal(state, before_state), move.uci())

    def test_special_moves_and_colour_mirrors(self) -> None:
        for fen in SPECIAL_FENS:
            board = chess.Board(fen)
            with self.subTest(fen=fen):
                self.check_position(board)
                self.check_position(board.mirror())

    def test_random_play_every_legal_child_and_exact_undo(self) -> None:
        rng = random.Random(904112)
        for _ in range(5):
            board = chess.Board()
            for ply in range(100):
                if ply % 3 == 0:
                    self.check_position(board)
                if board.is_game_over():
                    break
                board.push(rng.choice(list(board.legal_moves)))

    def test_perft_against_independent_reference(self) -> None:
        def reference_perft(board: chess.Board, depth: int) -> int:
            if depth == 0:
                return 1
            result = 0
            for move in board.legal_moves:
                board.push(move)
                result += reference_perft(board, depth - 1)
                board.pop()
            return result

        board, state = from_board(chess.Board())
        moves = np.zeros((5, MAX_MOVES), dtype=np.int64)
        undos = np.zeros((5, UNDO_SIZE), dtype=np.int64)
        self.assertEqual(perft(board, state, 4, moves, undos), 197281)
        for fen in SPECIAL_FENS[:8]:
            reference = chess.Board(fen)
            board, state = from_board(reference)
            self.assertEqual(perft(board, state, 3, moves, undos), reference_perft(reference, 3))

    def test_repetition_identity_ignores_only_unusable_en_passant(self) -> None:
        for fen, equal in (
            ("4r2k/8/8/3pP3/8/8/8/4K3 w - d6 0 2", True),
            ("7k/8/8/3pP3/8/8/8/7K w - d6 0 2", False),
            ("7k/8/8/3p4/8/8/8/7K w - d6 0 2", True),
        ):
            reference = chess.Board(fen)
            board, state = from_board(reference)
            before = board.copy(), state.copy()
            key, other = np.zeros(6, dtype=np.int64), np.zeros(6, dtype=np.int64)
            identity(board, state, key)
            self.assertTrue(np.array_equal(board, before[0]))
            self.assertTrue(np.array_equal(state, before[1]))
            state[EP] = -1
            identity(board, state, other)
            self.assertEqual(np.array_equal(key, other), equal)
            state[HALF], state[FULL] = 99, 120
            identity(board, state, other)
            self.assertEqual(np.array_equal(key, other), equal)
            state[TURN] *= -1
            identity(board, state, other)
            self.assertFalse(np.array_equal(key, other))

    def test_material_draw_rules(self) -> None:
        for fen in (
            "7k/8/8/8/8/8/8/K7 w - - 0 1",
            "7k/8/8/8/8/8/8/KN6 w - - 0 1",
            "6nk/8/8/8/8/8/8/KN6 w - - 0 1",
            "7k/8/8/8/8/8/8/KNN5 w - - 0 1",
            "5b1k/8/8/8/8/8/8/K1B5 w - - 0 1",
            "6bk/8/8/8/8/8/8/K1B5 w - - 0 1",
        ):
            reference = chess.Board(fen)
            board, state = from_board(reference)
            self.assertEqual(insufficient(board), reference.is_insufficient_material())
            self.assertNotEqual(state[WK], state[BK])


class CompiledSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        warmup()

    def test_search_matches_independent_exhaustive_values(self) -> None:
        def exact(board: chess.Board, depth: int) -> int:
            if board.is_checkmate():
                return -30_000
            if (
                board.is_stalemate()
                or board.is_insufficient_material()
                or board.halfmove_clock >= 100
                or board.is_repetition(3)
            ):
                return 0
            if not depth:
                return EvalState.from_board(board).evaluate(board, PawnCache())
            values = []
            for move in board.legal_moves:
                board.push(move)
                values.append(-exact(board, depth - 1))
                board.pop()
            return max(values)

        for fen in (
            "7k/8/6p1/8/3P4/8/8/K7 w - - 0 1",
            "6k1/8/3p4/8/3P4/2N5/8/1K6 w - - 0 1",
            "7k/8/8/3pP3/8/8/8/K7 w - d6 0 2",
        ):
            for reference in (chess.Board(fen), chess.Board(fen).mirror()):
                expected = exact(reference, 3)
                for pvs in (True, False):
                    search = Search(SearchConfig(max_depth=3, quiescence=False, pvs=pvs))
                    result = search.analyse(reference, 10_000, 10_000)
                    self.assertEqual(result.depth, 3)
                    self.assertEqual(result.score, expected, (reference.fen(), pvs))

    def test_transposition_bounds_do_not_change_what_the_search_concludes(self) -> None:
        """A stored bound is an optimisation. Three storage policies, one answer."""
        policies = (
            SearchConfig(use_tt=False),
            SearchConfig(use_tt=True, strict_draw_context=True),
            SearchConfig(use_tt=True, strict_draw_context=False),
        )
        for fen in (
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "r1bqkb1r/pppp1ppp/2n2n2/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4",
            "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1",
            "7k/8/8/3pP3/8/8/8/K7 w - d6 0 2",
        ):
            answers = set()
            for policy in policies:
                config = SearchConfig(
                    max_depth=4,
                    aspiration=False,
                    use_tt=policy.use_tt,
                    strict_draw_context=policy.strict_draw_context,
                )
                result = Search(config).analyse(chess.Board(fen), 30_000, 30_000)
                self.assertEqual(result.depth, 4, fen)
                answers.add((result.move.uci(), result.score))
            self.assertEqual(len(answers), 1, (fen, answers))

    def test_a_repetition_is_still_a_draw_when_bounds_are_reused(self) -> None:
        """The pre-probe repetition test is what makes relaxed bound reuse safe; check it."""
        reference = chess.Board("7k/7r/8/8/8/8/R7/K7 w - - 10 1")
        for move in ("a2b2", "h7g7", "b2a2", "g7h7"):
            reference.push_uci(move)
        config = SearchConfig(max_depth=4, aspiration=False, strict_draw_context=False)
        result = Search(config).analyse(reference, 30_000, 30_000)
        reference.push(result.move)
        self.assertFalse(reference.is_repetition(3), result.move.uci())

    def test_mate_underpromotion_and_fifty_move_priority(self) -> None:
        for reference in (
            chess.Board("7k/5Q2/6K1/8/8/8/8/8 w - - 99 1"),
            chess.Board("7k/5Q2/6K1/8/8/8/8/8 w - - 99 1").mirror(),
        ):
            result = Search(SearchConfig(max_depth=2)).analyse(reference, 1000, 1000)
            reference.push(result.move)
            self.assertTrue(reference.is_checkmate())
            self.assertEqual(result.score, 29999)
        reference = chess.Board("8/k1P5/2K5/8/8/8/8/8 w - - 0 1")
        result = Search(SearchConfig(max_depth=1)).analyse(reference, 1000, 1000)
        self.assertEqual(result.move.uci(), "c7c8r")

    def test_exact_history_and_stop_restore(self) -> None:
        reference = chess.Board()
        for move in ("g1f3", "g8f6", "f3g1", "f6g8"):
            reference.push_uci(move)
        search = Search(SearchConfig(max_nodes=80))
        board, state, positions, index = search.prepare(reference)
        self.assertFalse(repeated(positions, index, int(state[HALF])))
        before_board, before_state = board.copy(), state.copy()
        control = np.zeros(6, dtype=np.int64)
        control[NODE_LIMIT], control[ROOT_MOVE] = 80, encode(chess.Move.from_uci("e2e4"))
        negamax(
            board,
            state,
            8,
            -32000,
            32000,
            0,
            0,
            index,
            search.moves,
            search.scores,
            search.undos,
            positions,
            search.killers,
            search.history,
            search.hints,
            control,
            np.array([time.perf_counter() + 10.0]),
            search.flags,
        )
        self.assertTrue(control[STOP])
        self.assertTrue(np.array_equal(board, before_board))
        self.assertTrue(np.array_equal(state, before_state))
        for move in ("g1f3", "g8f6", "f3g1", "f6g8"):
            reference.push_uci(move)
        board, state, positions, index = search.prepare(reference)
        self.assertTrue(repeated(positions, index, int(state[HALF])))
        result = Search(SearchConfig(max_depth=2)).analyse(reference, 1000, 1000)
        self.assertEqual(result.score, 0)
        reference_without_history = chess.Board(reference.fen())
        _, state, positions, index = search.prepare(reference_without_history)
        self.assertFalse(repeated(positions, index, int(state[HALF])))

    def test_short_and_expired_deadlines_leave_input_unchanged(self) -> None:
        reference = chess.Board()
        for ms in (0.0, 3.0, 15.0, 50.0):
            search = Search()
            started = time.perf_counter()
            result = search.analyse(reference, ms, ms)
            elapsed = (time.perf_counter() - started) * 1000
            self.assertLess(elapsed, ms + 100, result)
            self.assertIn(result.move, reference.legal_moves)
            self.assertTrue(result.stopped)
            self.assertEqual(reference.fen(), chess.STARTING_FEN)
            if not ms:
                self.assertEqual(result.nodes, 0)


class CompiledAgentTests(unittest.TestCase):
    def test_clock_fallback_and_between_move_history(self) -> None:
        from a1.engine import ChessAgent

        agent = ChessAgent(log=False)
        reference = chess.Board()
        move = agent.get_move(reference.fen(), 60)
        self.assertIsNone(agent.last_result)
        reference.push_uci(move)
        reference.push(next(iter(reference.legal_moves)))
        restored = agent.synchronise(reference.fen())
        self.assertEqual(len(restored.move_stack), 2)
        self.assertEqual(restored.fen(), reference.fen())
        next_move = agent.get_move(reference.fen(), 108)
        self.assertIn(chess.Move.from_uci(next_move), reference.legal_moves)
        self.assertIsNotNone(agent.last_result)

    def test_resynchronisation_discards_unrelated_history(self) -> None:
        from a1.engine import ChessAgent

        agent = ChessAgent(log=False)
        agent.get_move(chess.STARTING_FEN, 200)
        reference = chess.Board("7k/8/6p1/8/3P4/8/8/K7 w - - 0 1")
        board = agent.synchronise(reference.fen())
        self.assertEqual(board.fen(), reference.fen())
        self.assertEqual(len(board.move_stack), 0)
        self.assertFalse(agent.search.hints.any())


if __name__ == "__main__":
    unittest.main()
