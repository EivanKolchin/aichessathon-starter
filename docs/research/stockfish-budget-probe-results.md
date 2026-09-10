# Stockfish budget sensitivity and A0 profiling

Completed 10 September 2026 · exploratory development study

**The probe found budget-sensitive choices, not a demonstrated full-strength Stockfish exploit.**
It also identified enough board-operation overhead in A0 to justify an original compiled backend
as the next engineering experiment. The related [exploitation roadmap](stockfish-exploitation-roadmap.md)
sets out the architectural hypotheses and their rejection criteria.

## Protocol and evidence

The read-only input was the supplied export `20260910-111002-497d4d`. The probe sampled accepted
plies 8, 9, 24 and 25 from each of its six completed Stockfish games: 24 positions from three
opening positions, including 12 turns originally played by A0 and 12 by Stockfish. These are
correlated development samples from games A0 lost, not a representative chess dataset.

Each PGN was legally replayed and checked against its recorded frames. Starting FEN, full played
history and halfmove counter were preserved. The external executable was selected explicitly;
commands in the exported metadata were never executed.

Stockfish 19 was queried at 1,000, 10,000 and 100,000 requested nodes, then 1,000,000 nodes.
When root moves differed, each distinct choice received a separate one-million-node restricted
root search. All queries used one thread, 64 MB hash, maximum skill, no strength limiting,
MultiPV 1, no tablebases and a fresh UCI game state. Actual nodes and elapsed times were recorded;
requested node counts are not exact execution caps. Full board history was sent even though
engine search state was cleared between queries. These are cold-state diagnostics, not the
persistent search state or time allocation of full games.

A fresh frozen A0 0.1.2 search was profiled at 5,000 nodes per case. The profile covers search,
excluding process startup; instrumentation affects timing. The five engine Python files match
the current 0.1.2 source. No new playing strength is claimed for this unchanged engine.

- [Full machine-readable report](results/stockfish-budget-probe-20260910.json): all cases, moves,
  PVs, scores, requested/actual nodes, timings, top profile functions, options and source hashes.
- [Derived summary](results/stockfish-budget-probe-20260910-summary.json): budget disagreements,
  conditional-score comparisons and profile totals.
- Local full profiles and frozen build: `.chesslab/studies/stockfish-budget-probe-20260910-v2/`.
- Tested executable SHA-256: `45bc8e4969147db9c2eb533810637994619bff0eacc81ccfd9854394901bcbd0`.

An initial attempt stopped before processing any cases because this binary does not expose
`UCI_AnalyseMode`. The probe now sets that option only when advertised. The failed attempt is
retained separately; the completed report contains all 24 requested cases.

## What changed with search budget

| Requested screening nodes | Preferred moves differing from the 1M-node reference |
|---|---:|
| 1,000 | 10 / 24 |
| 10,000 | 7 / 24 |
| 100,000 | 4 / 24 |

Across all four budgets, 15 positions had more than one preferred move and nine were stable.
This is **not a 62.5% error rate**. A move can change between near-equivalent continuations,
and a larger search is not an exact oracle. Nine reference evaluations fell within ±150 cp;
that post-analysis filter is useful for prioritisation, not a preregistered result.

The study made 127 UCI queries, executing 57,684,302 reported nodes. Summed query wall time was
192.64 seconds on this Windows workstation, including protocol overhead. That is measurement
cost, not a benchmark for competition hardware or a strength comparison with A0.

| Case | Observation | Interpretation |
|---|---|---|
| `g00047-p025` | At 1k nodes, `Qc7+`; at every larger budget, `Qh5+`. Separate 1M restricted searches gave −10 cp and +1413 cp respectively, from Black's perspective. The recorded game move was `Qh5+`. | A large low-budget miss that disappeared at 10k nodes. It did not trap Stockfish in the actual game. |
| `g00008-p009` | At 1k/10k, `Nc5`; at 100k/1M, `b5`. Conditional scores were −74 and −34 cp. This was originally A0's turn; A0 played `Nc5`. | A near-balanced position useful for testing search allocation. It is not evidence that the actual Stockfish opponent made this mistake. |
| `g00008-p024` | The first three budgets preferred `Bxe7`; 1M preferred `c5`. Restricted scores were +521 and +634 cp. Stockfish played `Bxe7` in the game. | A possible improvement in an already favourable position; no demonstrated outcome change. |
| `g00047-p009` | Choice alternated between `Be7` and `Nbd7`; restricted scores differed by only 1 cp. | Strong move disagreement with negligible estimated value difference. A useful negative control for a miner. |

Conditional scores can disagree with the unrestricted reference's ranking even at the same
budget. They reflect different finite searches; their spread is not exact regret. Mate values
remain separate from centipawn values and are not converted to an arbitrary numeric penalty.
The engine's WDL outputs are labelled model estimates in the data.

## Where A0 spent search time

Aggregating all 24 raw cProfile files gives 29.51 seconds of instrumented self time:

| Module group | Share of self time |
|---|---:|
| python-chess routines | 57.63% |
| Original A0 search routines | 18.29% |
| Original A0 evaluation routines | 11.94% |
| Built-ins and other routines | 12.13% |

These are exclusive self times, not sums of overlapping cumulative times. The report's more
specific function buckets attribute 19.43% to move generation/legality, 9.65% to board updates,
11.94% to evaluation/incremental updates, 3.71% to ordering and 2.64% to draw detection.
Those named buckets are incomplete functional categories: much attack-mask and bit-scanning
work remains under “other”. Their percentages should not be treated as total costs of those
subsystems.

At 5,000 nodes A0 completed depth 3 in 15 cases, depth 2 in six, depth 4 in two and depth 1 in one.
Node counts include substantial quiescence work, but a quiescence-node percentage is not a CPU
time percentage. These profiles do not justify removing defensive search simply to increase
reported depth.

The next experiment should move original board operations and search loops into a compiled
Python backend, preserve the current evaluator initially, and retain python-chess as a correctness
oracle. Measure uninstrumented wall time separately. A faster kernel still needs legality,
history, deadline and full-game confirmation before replacing the reference.

## Verification and remaining limits

Six focused probe/auditor tests passed, covering legal history reconstruction, repetition,
manifest/frame consistency, score orientation, fresh engine game state and invalid root replies.
Ruff passed across the repository and mypy passed for 42 source files. The existing submission
archive remains byte-for-byte consistent with its six source files and contains no lab code or
external engine. The archive was not rebuilt or uploaded for this study.

No opponent-response model, trajectory miner, compiled backend or automatic evolutionary
promotion was trained or implemented by this probe. The next evidence required for an exploit
is a surviving decision error at realistic clocks, followed by a strategy that reaches and
converts it against normal opponent choices on held-out openings. Test additional engine
families, retained search state, higher budgets and tablebase-enabled controls before claiming
transfer.
