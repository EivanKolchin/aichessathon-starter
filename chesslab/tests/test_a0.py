"""Correctness checks for our search, evaluation deltas and clock/state integration."""

import random
import time
import unittest

import chess

from a0.engine import ChessAgent, TimeControl
from a0.evaluation import TEMPO, EvalState, PawnCache
from a0.search import (
    MATE,
    Search,
    SearchConfig,
    pack_mate,
    position_key,
    quiescence_moves,
    unpack_mate,
)


class EvaluationTests(unittest.TestCase):
    def test_initial_position_is_balanced(self) -> None:
        board = chess.Board()
        self.assertEqual(EvalState.from_board(board).evaluate(board, PawnCache()), TEMPO)

    def test_incremental_state_matches_rebuild_and_undo(self) -> None:
        rng = random.Random(812)
        for _ in range(4):
            board = chess.Board()
            state = EvalState.from_board(board)
            records = []
            for _ in range(90):
                if board.is_game_over():
                    break
                move = rng.choice(list(board.legal_moves))
                records.append(state.push(board, move))
                board.push(move)
                self.assertEqual(state, EvalState.from_board(board))
            for record in reversed(records):
                board.pop()
                state.restore(record)
                self.assertEqual(state, EvalState.from_board(board))
            self.assertEqual(board.fen(), chess.STARTING_FEN)

    def test_special_move_deltas(self) -> None:
        cases = (
            ("7k/P7/8/8/8/8/8/7K w - - 0 1", "a7a8q"),
            ("r6k/1P6/8/8/8/8/8/7K w - - 0 1", "b7a8n"),
            ("7k/8/8/8/8/8/p7/7K b - - 0 1", "a2a1r"),
            ("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1"),
            ("r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1", "e8c8"),
            ("7k/8/8/3pP3/8/8/8/7K w - d6 0 2", "e5d6"),
        )
        for fen, uci in cases:
            with self.subTest(uci=uci):
                board = chess.Board(fen)
                state = EvalState.from_board(board)
                before = EvalState.from_board(board)
                undo = state.push(board, chess.Move.from_uci(uci))
                board.push_uci(uci)
                self.assertEqual(state, EvalState.from_board(board))
                state.restore(undo)
                self.assertEqual(state, before)

    def test_color_mirror_has_same_side_to_move_score(self) -> None:
        board = chess.Board()
        rng = random.Random(919)
        cache = PawnCache()
        for _ in range(30):
            board.push(rng.choice(list(board.legal_moves)))
            mirror = board.mirror()
            self.assertEqual(
                EvalState.from_board(board).evaluate(board, cache),
                EvalState.from_board(mirror).evaluate(mirror, cache),
            )


class SearchTests(unittest.TestCase):
    def test_quiescence_move_set_and_order_against_full_legal_moves(self) -> None:
        # Independent oracle: filter the complete legal move stream, including en passant,
        # pins and all promotion choices. Keep captures before quiet promotions.
        def check(board: chess.Board) -> None:
            legal = list(board.legal_moves)
            expected = [m for m in legal if board.is_capture(m)] + [
                m for m in legal if m.promotion and not board.is_capture(m)
            ]
            self.assertEqual(quiescence_moves(board), expected, board.fen())

        for fen in (
            "7k/P7/8/8/8/8/8/7K w - - 0 1",
            "r6k/1P6/8/8/8/8/8/7K w - - 0 1",
            "7k/8/8/8/8/8/p7/7K b - - 0 1",
            "7k/P7/8/8/8/8/8/r6K w - - 0 1",
            "7k/8/8/3pP3/8/8/8/7K w - d6 0 2",
            "4r2k/8/8/3pP3/8/8/8/4K3 w - d6 0 2",
        ):
            board = chess.Board(fen)
            self.assertTrue(board.is_valid())
            check(board)
            check(board.mirror())
        rng = random.Random(7301)
        for _ in range(6):
            board = chess.Board()
            for _ in range(100):
                check(board)
                if board.is_game_over():
                    break
                board.push(rng.choice(list(board.legal_moves)))

    def search(self, board: chess.Board, depth: int = 3) -> Search:
        search = Search(SearchConfig(max_depth=depth))
        search.analyse(board, 3000, 5000)
        return search

    def test_mate_in_one_both_colors(self) -> None:
        board = chess.Board("7k/5Q2/6K1/8/8/8/8/8 w - - 0 1")
        for position in (board, board.mirror()):
            result = Search().analyse(position, 100, 500)
            position.push(result.move)
            self.assertTrue(position.is_checkmate())
            self.assertEqual(result.score, MATE - 1)

    def test_no_move_requested_from_checkmate(self) -> None:
        board = chess.Board("7k/6Q1/5K2/8/8/8/8/8 b - - 0 1")
        # This position is mate, and the contract would not call an agent here.
        self.assertTrue(board.is_checkmate())
        with self.assertRaises(ValueError):
            Search().analyse(board, 100, 200)

    def test_only_check_evasion(self) -> None:
        board = chess.Board("7k/7R/6K1/8/8/8/8/8 b - - 0 1")
        self.assertEqual(len(list(board.legal_moves)), 1)
        result = Search().analyse(board, 100, 200)
        self.assertEqual(result.move.uci(), "h8g8")

    def test_underpromotion_avoids_stalemate(self) -> None:
        board = chess.Board("8/k1P5/2K5/8/8/8/8/8 w - - 0 1")
        queen = board.copy()
        queen.push_uci("c7c8q")
        self.assertTrue(queen.is_stalemate())
        result = Search(SearchConfig(max_depth=1)).analyse(board, 500, 1000)
        self.assertEqual(result.move.uci(), "c7c8r")

    def test_search_restores_all_mutable_state_on_node_stop(self) -> None:
        board = chess.Board()
        for san in ("Nf3", "Nf6", "Ng1", "Ng8"):
            board.push_san(san)
        before = board.fen()
        search = Search(SearchConfig(max_nodes=250))
        result = search.analyse(board, 1000, 2000)
        self.assertTrue(result.stopped)
        self.assertIn(result.move, board.legal_moves)
        self.assertEqual(board.fen(), before)
        self.assertEqual(search.board.fen(), before)
        self.assertEqual(search.evaluation, EvalState.from_board(board))
        counts, context = search.counts.copy(), search.context
        search._seed_history(board)
        self.assertEqual(search.counts, counts)
        self.assertEqual(search.context, context)

    def test_tiny_budget_returns_legal_fallback(self) -> None:
        board = chess.Board()
        started = time.monotonic()
        result = Search().analyse(board, 0, 0)
        self.assertIn(result.move, board.legal_moves)
        self.assertEqual(result.depth, 0)
        self.assertLess(time.monotonic() - started, 0.1)

    def test_repetition_context_and_halfmove_clock(self) -> None:
        repeated = chess.Board()
        for san in ("Nf3", "Nf6", "Ng1", "Ng8") * 2:
            repeated.push_san(san)
        fresh = chess.Board(repeated.fen())
        search = Search()
        search.analyse(repeated, 0, 0)
        context = search.context
        self.assertEqual(search._draw(position_key(repeated), 0), 0)
        search.analyse(fresh, 0, 0)
        self.assertNotEqual(search.context, context)
        self.assertIsNone(search._draw(position_key(fresh), 0))
        fresh.halfmove_clock = 100
        search.analyse(fresh, 0, 0)
        self.assertEqual(search._draw(position_key(fresh), 0), 0)

    def test_mate_precedes_fifty_move_draw(self) -> None:
        board = chess.Board("7k/6Q1/6K1/8/8/8/8/8 b - - 100 1")
        search = Search()
        search.board = board
        search._seed_history(board)
        self.assertEqual(search._draw(position_key(board), 5), -MATE + 5)

    def test_mate_scores_change_root_distance_correctly(self) -> None:
        self.assertEqual(unpack_mate(pack_mate(MATE - 8, 5), 2), MATE - 5)
        self.assertEqual(unpack_mate(pack_mate(-MATE + 8, 5), 2), -MATE + 5)
        self.assertEqual(unpack_mate(pack_mate(123, 5), 2), 123)

    def test_quiescence_sees_hanging_queen(self) -> None:
        board = chess.Board("7k/8/8/4q3/3P4/8/8/K7 w - - 0 1")
        result = Search(SearchConfig(max_depth=1)).analyse(board, 100, 500)
        self.assertEqual(result.move.uci(), "d4e5")

    def test_position_key_ignores_non_capturable_ep_but_keeps_legal_ep(self) -> None:
        board = chess.Board()
        board.push_uci("e2e4")
        other = board.copy()
        other.ep_square = None
        self.assertEqual(position_key(board), position_key(other))
        board = chess.Board("7k/8/8/3pP3/8/8/8/K7 w - d6 0 2")
        other = board.copy()
        other.ep_square = None
        self.assertNotEqual(position_key(board), position_key(other))

    def test_pvs_and_tt_match_exhaustive_reference(self) -> None:
        # The independent reference has no alpha-beta bounds, TT, move ordering or PVS.
        def reference(board: chess.Board, depth: int, ply: int) -> int:
            legal = list(board.legal_moves)
            if not legal:
                return -MATE + ply if board.is_check() else 0
            if (
                board.halfmove_clock >= 100
                or board.is_insufficient_material()
                or board.is_repetition(3)
            ):
                return 0
            if depth == 0:
                return EvalState.from_board(board).evaluate(board, PawnCache())
            scores = []
            for move in legal:
                board.push(move)
                scores.append(-reference(board, depth - 1, ply + 1))
                board.pop()
            return max(scores)

        for fen in ("7k/8/6p1/8/3P4/8/8/K7 w - - 0 1", "6k1/8/3p4/8/3P4/2N5/8/1K6 w - - 0 1"):
            board = chess.Board(fen)
            expected = reference(board, 3, 0)
            for pvs, tt in ((False, False), (True, True)):
                result = Search(
                    SearchConfig(max_depth=3, quiescence=False, pvs=pvs, use_tt=tt)
                ).analyse(board, 5000, 5000)
                self.assertEqual(result.depth, 3)
                self.assertEqual(result.score, expected)


class AgentTests(unittest.TestCase):
    def test_history_survives_opponent_moves(self) -> None:
        agent = ChessAgent(log=False)
        agent.search = Search(SearchConfig(max_depth=1))
        board = chess.Board()
        for _ in range(3):
            move = agent.get_move(board.fen(), 2000)
            board.push_uci(move)
            board.push(next(iter(board.legal_moves)))
        agent.synchronise(board.fen())
        assert agent.board is not None
        self.assertEqual(agent.board.fen(), board.fen())
        self.assertEqual(len(agent.board.move_stack), 6)

    def test_critical_clock_returns_promptly(self) -> None:
        agent = ChessAgent(log=False)
        before = time.monotonic()
        move = agent.get_move(chess.STARTING_FEN, 5)
        self.assertIn(chess.Move.from_uci(move), chess.Board().legal_moves)
        self.assertLess(time.monotonic() - before, 0.1)
        self.assertIsNone(agent.last_result)

    def test_budgets_fit_remaining_clock(self) -> None:
        clock = TimeControl()
        for remaining in (1, 10, 100, 1000, 120000):
            budget = clock.allocate(remaining, chess.Board())
            self.assertGreaterEqual(budget.soft_ms, 0)
            self.assertLessEqual(budget.soft_ms, budget.hard_ms)
            self.assertLessEqual(budget.hard_ms, remaining)

    def test_increment_is_learned_from_clock_observations(self) -> None:
        clock = TimeControl()
        clock.finish(10000, 100)
        clock.observe(10400)
        self.assertGreater(clock.increment_estimate, 200)
        self.assertLess(clock.increment_estimate, 500)


if __name__ == "__main__":
    unittest.main()
