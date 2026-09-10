# A0 — original classical baseline

The working version is 0.1.2. Version 0.1.0 remains a frozen reference in Chess Lab.
The 0.1.1 change restricts quiet-promotion generation to pawns on the promotion rank and empty
destination squares. Captures retain their original order, and checked positions still search
all legal evasions. Version 0.1.2 also uses a precise clock, checks every node under short
deadlines, and reserves more time for the referee/protocol. Evaluation and search policy are unchanged.

A0 supplies the first original engine behind the root `agent.py` contract. It is a correctness
and measurement reference for later evaluator training and search optimisation. It uses the
permitted `python-chess` library for legal moves and board operations. The evaluation, search,
clock allocation and caching below were written for this project; no third-party engine code,
published neural weights or engine-labelled runtime lookup tables are used.

## Structure

| Module | Responsibility |
|---|---|
| `evaluation.py` | Original geometric piece-square formulas, tapered material/placement, pawn structure, rook files, king shelter and bishop pair. Incremental make/unmake deltas and a bounded pawn cache. |
| `search.py` | Iterative deepening negamax/PVS, aspiration windows, quiescence, move ordering, killers/history, bounded transposition storage, draw context and interruption handling. |
| `engine.py` | Stateful `get_move`, reconstruction of the opponent's last move, soft/hard search budgets, clock reserve and JSON search telemetry. |
| root `agent.py` | Imports one persistent `ChessAgent` instance and exposes the competition function. |

The piece-square tables are computed from visible geometric formulas at import; all starting
values are hand-chosen hypotheses. Pawn structure is cached by the two pawn bitboards. Captures,
en passant, promotions and castling update evaluation in place and restore exactly on unmake.
Material/placement interpolates between middle- and endgame scores using remaining piece phase.

Search completes successive depths and keeps the last completed result if interrupted. It
searches captures, promotions and check evasions beyond the nominal horizon. A checkmate takes
precedence over the fifty-move draw. The root board and incremental evaluator are restored even
when a node limit or deadline interrupts a descendant.

Scores in the transposition table are conditioned on piece position, side, castling rights,
capturable en passant, the halfmove clock and a fingerprint of repetition counts. This sacrifices
some transposition reuse to reduce history-dependent draw errors. The fingerprint is a finite
hash and therefore has the usual nonzero collision risk; full board tuples are retained for the
position itself. Mate scores are adjusted when saved and restored at a different root distance.
Move hints can be shared more broadly. Table and pawn-cache capacities are bounded.

The module retains the chosen move and infers the opponent's one legal reply from the next FEN,
preserving actual game history for repetition. A new or unreconcilable position resets that
history. Search needs only the reversible history since the most recent pawn move or capture.

## Clock behaviour

`time_left_ms` is the only clock input. A0 reserves time for Python/protocol overhead, allocates
a fraction of remaining time, and enforces soft/hard search deadlines. It estimates increments
from successive supplied clocks and measured move time rather than assuming every test uses
the competition increment. At 60 ms or less it returns a legal emergency move without a search.
There is no pondering, network access, external process invocation or environment seed input.

Deadline checks use `perf_counter`, between root moves and every 32 visited nodes, or every node
for budgets below 250 ms. A0 reserves 30–100 ms for overhead. This is cooperative timing,
not a real-time operating-system guarantee. The tests exercise low clocks and the event harness
supplies the actual loss-on-flag adjudication.

## Observe and benchmark

Register the working build and open the lab:

```powershell
.venv\Scripts\python.exe -m chesslab register chesslab\experiments\a0-engine.json
.venv\Scripts\python.exe -m chesslab serve
```

The first development protocol is `chesslab/experiments/a0-league.json`: 24 games, four opponents
(greedy, minimax, the lab tactical engine and Stockfish), three development opening families,
both colours, 5 seconds plus 50 ms, with the full 600-ply cap. Its random reference protocol has
the same opponent builds, openings, seed and clock. These are exploratory measurements, not a
rating or a claim of beating Stockfish. Use a different `--data-dir` for headless work if the
browser server already owns the default results directory; avoid concurrent timed experiments.

Each move logs the completed depth, evaluation, visited/quiescence nodes, transposition hits,
elapsed time and a legal continuation recovered from move hints. That continuation is a search
diagnostic, not a proof of the score. A tiny-clock emergency move has no computed evaluation.
Logs are flushed per move; the event harness retains only the beginning and end of long logs.
Aggregate diagnostics from these retained records are a sample, not complete game telemetry.

Run fixed-node measurements separately from timed matches:

```powershell
.venv\Scripts\python.exe -m chesslab.experiments.profile_a0 --nodes 10000
.venv\Scripts\python.exe -m chesslab.experiments.profile_a0 --nodes 10000 --no-pvs --output .chesslab\a0-no-pvs.json
.venv\Scripts\python.exe -m chesslab.experiments.profile_a0 --nodes 10000 --profile .chesslab\a0.pstats --output .chesslab\a0-instrumented.json
```

The profiler saves the search configuration, public development FENs, local environment,
node/depth results and a source snapshot beside each report. Each position starts with fresh
caches. `--no-tt` and `--no-quiescence` are also available for controlled diagnostics. Fixed-node
results compare search behaviour, while full games test whether a change improves play. An
instrumented run includes profiling overhead and must not be compared directly with normal NPS.

## Validation and next changes

Tests include randomized incremental-evaluation round trips, special-move deltas, mirrored
evaluation, mate and draw precedence, underpromotion to avoid stalemate, check evasion,
deadline/node-stop restoration, repetition context, and agreement with an independent exhaustive
search on small positions. End-to-end matches and packaging use the unmodified event harness.

The next step is to examine per-opponent results and loss traces, then profile fixed-node searches.
Keep A0 as the reference while measuring changes separately. Candidates include better quiet-move
ordering, selective search, a compiled original move-generation/search backend, and an independently
trained evaluator. Null-move pruning and late-move reductions are not enabled in this baseline;
their tactical and zugzwang tradeoffs need separate experiments. The current Python search is
not a Stockfish-strength engine.

The first measured results, evidence paths and next experiment are recorded in
[`docs/research/a0-results.md`](../docs/research/a0-results.md).

To compare changes against a frozen original build:

```powershell
.venv\Scripts\python.exe -m chesslab.experiments.bench_a0 --directory .chesslab\benchmarks\my-experiment --nodes 5000 --repeats 4
```

Use a new output directory each time. The command freezes both builds, runs 20 development
positions and nine edge/history fixtures in separate processes, and checks identical moves,
scores, depths, nodes, quiescence nodes, transposition hits and continuations. Each trial warms
the engine before measuring a fresh search with `perf_counter`; build order alternates and
fixture order is shuffled reproducibly. Special fixtures are excluded from the aggregate speed
ratio. Raw trials and hashes are retained. Use `--reference` and `--candidate` to select other
local A0 build directories. Run timing comparisons separately from timed games.
