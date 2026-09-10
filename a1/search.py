"""Original compiled iterative deepening, PVS and bounded quiescence.

Reuses A0's evaluation and search limits. Transposition storage supplies both move ordering and
score bounds. A stored bound is only reused when the draw context matches, because whether a
position is a draw depends on the halfmove clock and on which positions the path has already
visited; move hints carry no such claim and are shared on position alone. Repetition comparisons
use exact packed identities, not hash equality.
"""

import time
from dataclasses import dataclass

import chess
import numpy as np
from numba import objmode
from numpy.typing import NDArray

from a0.search import INFINITY, MATE, MATE_BOUND, MAX_PLY, SearchResult
from a1.board import (
    EP,
    HALF,
    MAX_MOVES,
    TURN,
    UNDO_SIZE,
    IntArray,
    decode,
    encode,
    from_board,
    generate,
    has_material,
    identity,
    in_check,
    insufficient,
    make,
    make_null,
    unmake,
    unmake_null,
)
from a1.evaluation import evaluate
from a1.jit import compiled

type FloatArray = NDArray[np.float64]
NODES, QNODES, STOP, NODE_LIMIT, CLOCK_MASK, ROOT_MOVE, CONTEXT, TTHITS = range(8)
# Transposition columns. BOUND is 0 when the slot holds a move hint but no usable score.
KEY, MOVE, TAG, SCORE, DEPTH, BOUND = range(6)
LOWER, UPPER, EXACT = 1, 2, 3
HALF_TAG = 0x9E3779B97F4A7C15
VALUES = np.array((0, 100, 315, 330, 505, 950, 0), dtype=np.int64)


@dataclass(frozen=True)
class SearchConfig:
    max_depth: int = 48
    max_nodes: int = 0
    quiescence: bool = True
    pvs: bool = True
    aspiration: bool = True
    use_hints: bool = True
    use_tt: bool = True
    use_null: bool = True
    use_lmr: bool = True
    # Strict conditions a stored bound on the whole path, the way A0 does. Measured over six
    # positions it reused almost nothing - a transposition reached by a different move order
    # never matches a path-summed fingerprint - so it is kept as an ablation rather than the
    # default. Relaxed conditions on the halfmove clock alone and relies on the repetition and
    # fifty-move tests this node already ran before probing. That is the ordinary engine
    # approximation to graph history interaction: a bound can still come from a subtree whose
    # internal repetitions differed from this path's.
    strict_draw_context: bool = False


@compiled
def precise_time() -> float:
    # A rare Python boundary provides the same precise wall clock as the referee
    # wrapper. No background thread, platform DLL or cached compiled file is needed.
    now = 0.0
    with objmode(now="float64"):
        now = time.perf_counter()
    return now


@compiled
def repeated(history: IntArray, index: int, halfmove: int) -> bool:
    count = 1
    for previous in range(index - 2, max(-1, index - halfmove - 1), -2):
        same = True
        for word in range(6):
            if history[index, word] != history[previous, word]:
                same = False
                break
        if same:
            count += 1
            if count >= 3:
                return True
    return False


@compiled
def mix(key: IntArray) -> int:
    """A second, independent fold of the same identity.

    Summed over the path it becomes a draw-context fingerprint. Addition rather than XOR is
    deliberate: XOR cancels a position that appears twice, which is exactly the repetition the
    fingerprint has to notice.
    """
    result = 0
    for index in range(6):
        word = key[index] + HALF_TAG
        word ^= word >> 30
        word *= 0xBF58476D1CE4E5B9
        result += word ^ (word >> 27)
    return int(result)


@compiled
def context_seed(positions: IntArray, count: int) -> int:
    """Sum the inherited reversible history, wrapping at 64 bits the way the search does."""
    total = 0
    for index in range(count):
        total += mix(positions[index])
    return int(total)


@compiled
def pack_mate(score: int, ply: int) -> int:
    if score >= MATE_BOUND:
        return score + ply
    if score <= -MATE_BOUND:
        return score - ply
    return score


@compiled
def unpack_mate(score: int, ply: int) -> int:
    if score >= MATE_BOUND:
        return score - ply
    if score <= -MATE_BOUND:
        return score + ply
    return score


@compiled
def hint_key(key: IntArray) -> int:
    result = key[5]
    for index in range(5):
        result ^= key[index] ^ (key[index] >> (index + 1))
    return int(result ^ (result >> 23) ^ (result >> 41))


@compiled
def ordered(
    board: IntArray,
    state: IntArray,
    moves: IntArray,
    scores: IntArray,
    count: int,
    hint: int,
    killers: IntArray,
    history: IntArray,
    ply: int,
) -> None:
    for i in range(count):
        move = moves[i]
        source, target, promotion = move & 127, (move >> 7) & 127, move >> 14
        victim = abs(board[target])
        if abs(board[source]) == 1 and target == state[EP]:
            victim = 1
        if move == hint:
            value = 2_000_000
        elif victim:
            value = 1_000_000 + 16 * VALUES[victim] - VALUES[abs(board[source])]
        elif promotion:
            value = 900_000 + VALUES[promotion]
        elif move == killers[ply, 0]:
            value = 800_000
        elif move == killers[ply, 1]:
            value = 790_000
        else:
            value = history[0 if state[TURN] == 1 else 1, source, target]
        # Stable insertion sort preserves generator order for equal priorities.
        j = i
        while j > 0 and scores[j - 1] < value:
            scores[j], moves[j] = scores[j - 1], moves[j - 1]
            j -= 1
        scores[j], moves[j] = value, move


@compiled
def negamax(
    board: IntArray,
    state: IntArray,
    depth: int,
    alpha: int,
    beta: int,
    ply: int,
    qply: int,
    root_index: int,
    moves: IntArray,
    scores: IntArray,
    undos: IntArray,
    positions: IntArray,
    killers: IntArray,
    history: IntArray,
    hints: IntArray,
    control: IntArray,
    deadline: FloatArray,
    flags: IntArray,
) -> int:
    control[NODES] += 1
    quiescent = depth <= 0
    control[QNODES] += int(quiescent)
    if (control[NODE_LIMIT] and control[NODES] >= control[NODE_LIMIT]) or (
        control[NODES] & control[CLOCK_MASK] == 0 and precise_time() >= deadline[0]
    ):
        control[STOP] = 1
        return 0
    count = generate(board, state, moves[ply], undos[ply])
    checked = in_check(board, state)
    if count == 0:
        return -MATE + ply if checked else 0
    if (
        state[HALF] >= 100
        or repeated(positions, root_index + ply, state[HALF])
        or insufficient(board)
    ):
        return 0
    if ply >= MAX_PLY - 1:
        return evaluate(board, state)
    if quiescent:
        if not flags[0]:
            return evaluate(board, state)
        if not checked:
            stand = evaluate(board, state)
            if stand >= beta:
                return stand
            alpha = max(alpha, stand)
            if qply >= 8:
                return stand
            tactical = 0
            for i in range(count):
                move = moves[ply, i]
                source, target = move & 127, (move >> 7) & 127
                if board[target] or move >> 14 or (abs(board[source]) == 1 and target == state[EP]):
                    moves[ply, tactical] = move
                    tactical += 1
            count = tactical
    key = hint_key(positions[root_index + ply])
    slot = key & (len(hints) - 1)
    tag = state[HALF] * HALF_TAG
    if flags[4]:
        tag += control[CONTEXT]
    hint = 0
    if not quiescent and flags[2] and hints[slot, KEY] == key:
        hint = hints[slot, MOVE]
        # A bound is only what this position was worth on a path with the same draw prospects,
        # and never at the root, where the caller still needs the move this node chooses.
        if (
            ply > 0
            and flags[3]
            and hints[slot, BOUND]
            and hints[slot, TAG] == tag
            and hints[slot, DEPTH] >= depth
        ):
            value = unpack_mate(hints[slot, SCORE], ply)
            bound = hints[slot, BOUND]
            if (
                bound == EXACT
                or (bound == LOWER and value >= beta)
                or (bound == UPPER and value <= alpha)
            ):
                control[TTHITS] += 1
                return value
    if ply == 0:
        hint = control[ROOT_MOVE]

    # Null-move pruning. If passing the move still leaves the opponent unable to reach beta,
    # the real move almost certainly beats beta too, so the subtree is not worth searching.
    #
    # A null position is written into the repetition history like any other, and cannot create
    # a false repetition: the scan from the child inspects positions an odd number of real
    # plies away, and no odd number of real moves returns to the same placement. It can still
    # miss a genuine repetition inside the null subtree, which errs toward not pruning.
    if (
        flags[5]
        and not quiescent
        and ply > 0
        and depth >= 3
        and not checked
        and beta < MATE_BOUND
        and has_material(board, state[TURN])
    ):
        reduction = 2 + depth // 6
        make_null(state, undos[ply])
        identity(board, state, positions[root_index + ply + 1])
        control[CONTEXT] += mix(positions[root_index + ply + 1])
        score = -negamax(
            board,
            state,
            depth - 1 - reduction,
            -beta,
            -beta + 1,
            ply + 1,
            0,
            root_index,
            moves,
            scores,
            undos,
            positions,
            killers,
            history,
            hints,
            control,
            deadline,
            flags,
        )
        control[CONTEXT] -= mix(positions[root_index + ply + 1])
        unmake_null(state, undos[ply])
        if control[STOP]:
            return 0
        # A mate found behind a pass is not a mate anyone can force; report the bound instead.
        if score >= beta:
            return beta if score >= MATE_BOUND else score
        # The null search overwrote this ply's move list.
        count = generate(board, state, moves[ply], undos[ply])

    ordered(board, state, moves[ply], scores[ply], count, hint, killers, history, ply)
    original_alpha = alpha
    best, best_move = -INFINITY, 0
    for i in range(count):
        move = moves[ply, i]
        source, target = move & 127, (move >> 7) & 127
        quiet = (
            not board[target]
            and not move >> 14
            and not (abs(board[source]) == 1 and target == state[EP])
        )
        make(board, state, move, undos[ply])
        identity(board, state, positions[root_index + ply + 1])
        control[CONTEXT] += mix(positions[root_index + ply + 1])
        child_depth = depth - 1 if depth > 0 else 0
        child_qply = qply + 1 if quiescent else 0

        # Late move reductions. Ordering already put the moves worth searching first, so the
        # ones left over are searched shallower on the assumption they will not beat alpha.
        # When one does, it is searched again at full depth, so this costs accuracy only
        # where the assumption held. Checks, captures, promotions and evasions keep full depth.
        reduction = 0
        if (
            flags[6]
            and not quiescent
            and quiet
            and i >= 3
            and depth >= 3
            and not checked
            and not in_check(board, state)
        ):
            reduction = 1 + (depth - 3) // 4 + (i - 3) // 8
            if reduction > child_depth - 1:
                reduction = child_depth - 1
            if reduction < 0:
                reduction = 0

        if flags[1] and not quiescent and i > 0:
            score = -negamax(
                board,
                state,
                child_depth - reduction,
                -alpha - 1,
                -alpha,
                ply + 1,
                child_qply,
                root_index,
                moves,
                scores,
                undos,
                positions,
                killers,
                history,
                hints,
                control,
                deadline,
                flags,
            )
            # A reduced move that beat alpha was not the kind of move the reduction assumed.
            if not control[STOP] and reduction and score > alpha:
                score = -negamax(
                    board,
                    state,
                    child_depth,
                    -alpha - 1,
                    -alpha,
                    ply + 1,
                    child_qply,
                    root_index,
                    moves,
                    scores,
                    undos,
                    positions,
                    killers,
                    history,
                    hints,
                    control,
                    deadline,
                    flags,
                )
            if not control[STOP] and alpha < score < beta:
                score = -negamax(
                    board,
                    state,
                    child_depth,
                    -beta,
                    -alpha,
                    ply + 1,
                    child_qply,
                    root_index,
                    moves,
                    scores,
                    undos,
                    positions,
                    killers,
                    history,
                    hints,
                    control,
                    deadline,
                    flags,
                )
        else:
            score = -negamax(
                board,
                state,
                child_depth,
                -beta,
                -alpha,
                ply + 1,
                child_qply,
                root_index,
                moves,
                scores,
                undos,
                positions,
                killers,
                history,
                hints,
                control,
                deadline,
                flags,
            )
        unmake(board, state, move, undos[ply])
        control[CONTEXT] -= mix(positions[root_index + ply + 1])
        if control[STOP]:
            return 0
        if score > best:
            best, best_move = score, move
        alpha = max(alpha, score)
        if alpha >= beta:
            if quiet and not quiescent:
                if move != killers[ply, 0]:
                    killers[ply, 1], killers[ply, 0] = killers[ply, 0], move
                color = 0 if state[TURN] == 1 else 1
                history[color, source, target] = min(
                    30_000, history[color, source, target] + depth * depth
                )
            break
    if not quiescent and best_move:
        if flags[2]:
            hints[slot, KEY], hints[slot, MOVE] = key, best_move
            hints[slot, TAG] = tag
            hints[slot, SCORE] = pack_mate(best, ply)
            hints[slot, DEPTH] = depth
            hints[slot, BOUND] = (
                UPPER if best <= original_alpha else LOWER if best >= beta else EXACT
            )
        if ply == 0:
            control[ROOT_MOVE] = best_move
    return alpha if quiescent else best


class Search:
    def __init__(self, config: SearchConfig | None = None) -> None:
        self.config = config or SearchConfig()
        if not 1 <= self.config.max_depth < MAX_PLY or self.config.max_nodes < 0:
            raise ValueError("Invalid compiled search limits")
        self.moves = np.zeros((MAX_PLY, MAX_MOVES), dtype=np.int64)
        self.scores = np.zeros_like(self.moves)
        self.undos = np.zeros((MAX_PLY, UNDO_SIZE), dtype=np.int64)
        self.killers = np.zeros((MAX_PLY, 2), dtype=np.int64)
        self.history = np.zeros((2, 128, 128), dtype=np.int64)
        self.hints = np.zeros((32768, 6), dtype=np.int64)
        self.flags = np.array(
            [
                self.config.quiescence,
                self.config.pvs,
                self.config.use_hints,
                self.config.use_tt,
                self.config.strict_draw_context,
                self.config.use_null,
                self.config.use_lmr,
            ],
            dtype=np.int64,
        )

    def reset(self) -> None:
        self.hints.fill(0)
        self.killers.fill(0)
        self.history.fill(0)

    def prepare(self, reference: chess.Board) -> tuple[IntArray, IntArray, IntArray, int]:
        ancestor = reference.copy(stack=True)
        played: list[chess.Move] = []
        while ancestor.move_stack and len(played) < reference.halfmove_clock:
            played.append(ancestor.pop())
        board, state = from_board(ancestor)
        positions = np.zeros((len(played) + MAX_PLY + 1, 6), dtype=np.int64)
        identity(board, state, positions[0])
        for index, move in enumerate(reversed(played), 1):
            make(board, state, encode(move), self.undos[0])
            identity(board, state, positions[index])
        return board, state, positions, len(played)

    def analyse(self, reference: chess.Board, soft_ms: float, hard_ms: float) -> SearchResult:
        started = time.perf_counter()
        deadline = np.array([started + max(0, hard_ms) / 1000], dtype=np.float64)
        board, state, positions, root_index = self.prepare(reference)
        count = generate(board, state, self.moves[0], self.undos[0])
        if not count:
            raise ValueError("Cannot search a position without legal moves")
        self.history //= 2
        key = hint_key(positions[root_index])
        slot = key & (len(self.hints) - 1)
        hint = int(self.hints[slot, MOVE]) if self.hints[slot, KEY] == key else 0
        ordered(
            board,
            state,
            self.moves[0],
            self.scores[0],
            count,
            hint,
            self.killers,
            self.history,
            0,
        )
        best, score, completed = int(self.moves[0, 0]), evaluate(board, state), 0
        # The reversible history the search inherits is already part of its draw context.
        seed = context_seed(positions, root_index + 1)
        control = np.array(
            [0, 0, 0, self.config.max_nodes, 0 if hard_ms < 20 else 31, best, seed, 0],
            dtype=np.int64,
        )
        for depth in range(1, self.config.max_depth + 1):
            now = time.perf_counter()
            if now >= deadline[0]:
                control[STOP] = 1
                break
            if depth > 1 and (now - started) * 1000 >= soft_ms:
                break
            window = 40 if depth > 2 and self.config.aspiration else INFINITY
            while True:
                low, high = max(-INFINITY, score - window), min(INFINITY, score + window)
                if window >= INFINITY:
                    low, high = -INFINITY, INFINITY
                current = negamax(
                    board,
                    state,
                    depth,
                    low,
                    high,
                    0,
                    0,
                    root_index,
                    self.moves,
                    self.scores,
                    self.undos,
                    positions,
                    self.killers,
                    self.history,
                    self.hints,
                    control,
                    deadline,
                    self.flags,
                )
                if control[STOP] or low < current < high or window >= INFINITY:
                    break
                window = min(INFINITY, window * 4)
            if control[STOP]:
                break
            best, score, completed = int(control[ROOT_MOVE]), current, depth
            if abs(score) >= MATE_BOUND:
                break
        move = decode(best)
        if move not in reference.legal_moves:
            raise RuntimeError("Compiled search returned an illegal root move")
        return SearchResult(
            move,
            int(score),
            completed,
            int(control[NODES]),
            int(control[QNODES]),
            (time.perf_counter() - started) * 1000,
            int(control[TTHITS]),
            bool(control[STOP]),
            (move.uci(),),
        )


def warmup() -> None:
    """Compile production signatures at import, before the playing clock starts."""
    Search(SearchConfig(max_depth=2)).analyse(chess.Board(), 60_000, 60_000)
    precise_time()
