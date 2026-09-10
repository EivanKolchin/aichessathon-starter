# A0 0.1.0: first measured reference

10 September 2026. A0 is an original classical engine and a working reference for subsequent
search and evaluator experiments. In the final paired development study it won all 18 games
against the three simpler opponents and lost all six against Stockfish 19. All games completed
without agent failures. The random reference lost all 24 games on the same schedule.

## Measured games

| Opponent | A0 wins / draws / losses | Random reference | A0 points |
|---|---:|---:|---:|
| Material greedy | 6 / 0 / 0 | 0 / 0 / 6 | 6 / 6 |
| Minimax | 6 / 0 / 0 | 0 / 0 / 6 | 6 / 6 |
| Lab tactical search | 6 / 0 / 0 | 0 / 0 / 6 | 6 / 6 |
| Stockfish 19 | 0 / 0 / 6 | 0 / 0 / 6 | 0 / 6 |

Each opponent played both colours from Italian, French advance and Queen's gambit declined
positions: three public development opening pairs, at 5 seconds plus 50 ms per move, with a
600-ply cap. Stockfish used its full clock, one thread, a 64 MB hash and no pondering. The
official executable's identity and hash are recorded in the manifests. Python opponents and
A0 were copied before play. Games ran sequentially on this Windows host.

The strict comparison matched all 12 pairs with identical opponent fingerprints, FENs, seeds,
clock limits, recorded environment and evaluator fingerprints. Its conservative per-opponent
score-difference bounds all include zero. Three selected opening pairs are far too few for a
general strength conclusion; these are exploratory bounds, not an Elo estimate or promotion test.
The Windows host does not reproduce the event container, CPU affinity or hardware.

## Evidence and replay

- Final A0 run: `20260910-105551-0eebea`.
- Matched random reference: `20260910-105442-76145a`.
- [Structured results](results/a0-0.1.0.json): scores, matched differences, source hashes,
  retained search diagnostics, fixed-node measurements and Stockfish loss identifiers.
- Both complete runs are in `.chesslab/runs/` and can be replayed or exported in Chess Lab.
- The independent evaluator workspace is `.chesslab/studies/a0-20260910/`. It contains frozen
  harness, lab, agent and baseline sources, source checksums, run records and `comparison.json`.
  Checksums were verified before each run and after the study. Stockfish remains an external,
  fingerprinted research opponent.
- `A0 0.1.0 reference` is registered from `.chesslab/checkpoints/a0-0.1.0/`. Its five Python
  source files match the measured engine. Keep this checkpoint unchanged when developing A0.

Open the [A0 replay](http://127.0.0.1:8765/?run=20260910-105551-0eebea) while Chess Lab is running.
Recompute the matched report with:

```powershell
.venv\Scripts\python.exe -m chesslab compare .chesslab\runs\20260910-105442-76145a\manifest.json .chesslab\runs\20260910-105551-0eebea\manifest.json
```

Earlier runs are retained. The initial unflushed-log A0 run scored 18 wins and six losses;
the first run with flushed diagnostics scored 17 wins, one draw and six losses. A partial
random-reference run stopped when the disk filled and is excluded. Another completed random
run could not be paired by the strict comparer after concurrent lab command-line changes.
The final study froze the complete evaluator and reran both players. These repeated schedules
are useful reliability checks, but they do not supply independent strength samples.

## What profiling tells us

A0 uses iterative deepening PVS, quiescence, bounded transposition storage, incremental tapered
evaluation, pawn caching, repetition tracking and conservative clock allocation. Legal moves
come from python-chess. Its search and evaluation were written for this project.

The final A0 run contains 627 moves by A0. We recovered 598 complete search records from logs
whose middle can be truncated by the harness. Of those records, 341 completed depth two and
157 completed depth three; one used a depth-zero fallback. Quiescence contributed about 93.9%
of visited nodes in this retained sample. These are sampled diagnostics, not complete telemetry
or a direct measure of tactical quality.

The reproducible profiler searched three development positions with 10,000 visited nodes each,
fresh caches and the default search settings. All completed depth three; elapsed times on the
local host were approximately 0.84, 1.19 and 1.95 seconds. Host load affects these times.
The instrumented repeat produced the same moves and depths. Its cumulative call times included:

| Function | Cumulative seconds |
|---|---:|
| All three searches | 6.06 |
| Quiescence search | 4.50 |
| Legal-move generation | 2.62 |
| Incremental push wrapper | 1.01 |
| Static evaluation | 0.66 |

These rows overlap because callers include their callees; they must not be added. Instrumented
timing includes profiling overhead. The raw records are `.chesslab/a0-profile.json`,
`.chesslab/a0-instrumented.json` and `.chesslab/a0.pstats`. See `a0/README.md` for commands.

## Next experiment

1. Reduce redundant move generation in quiescence. In particular, generate quiet promotion
   candidates only from the promotion rank, and examine repeated legal-move checks. First require
   identical fixed-node moves, scores and node counts on a broader development fixture set,
   alongside special-move and interruption tests. This should preserve search behaviour.
2. Measure speed with fresh caches, alternating the order of old and new builds across repeated
   runs on an otherwise idle host. Then compare full paired games against the frozen A0 reference
   and a wider opponent/opening pool. A faster microbenchmark alone does not justify promotion.
3. Test selective pruning or a compiled original backend as separate candidates. Preserve the
   Python implementation as a correctness reference. Verify a new move generator against it
   before using speed results to choose an architecture.
4. After that reference is stable, build A1's original training and incremental-evaluation
   pipeline. Track teacher identity, data splits, export size, incremental-update correctness,
   inference cost and same-clock game results. Keep training loss separate from playing strength.

Offline research agents can propose these experiments and analyse losses. The current runtime
remains a deterministic search program. Broader evolutionary architecture search, neural
training and automatic promotion are still future modules; the longer-term programme is in
`stockfish-research-plan.md`.
