# A development path toward beating Stockfish 19

Research note · 10 September 2026 · **revised the same day after rung 1 was measured; see
§3 and §4a** · complements
[the research programme](stockfish-research-plan.md) and
[the exploitation roadmap](stockfish-exploitation-roadmap.md)

**Review, 11 September.** The engine now has correctness fixes and a separately measured
[neural/search pilot](hybrid-pilot.md). The six-position search measurements below describe the
older builds; they do not establish Elo gains or a language-based strength ceiling.

**Summary.** Beating full-strength Stockfish is an unproven long-term objective. Ratings drawn
from different engine pools do not measure our gap or prove that it cannot be closed. A useful
development target, and what this note argues for, is a ladder with a scalar to track: the
Stockfish strength at which our engine breaks even. A new measurement taken for this note
locates the bottleneck, and it is not where the previous profile pointed. A1's compiled backend
is 3.7× faster per node than A0 but buys under one ply, because neither engine does any
selective pruning at all. Search selectivity, not throughput, is the dominant term. Rung 1 of the
ladder below has since been implemented and measured; it was worth about a seventh of what this
note first predicted, and the reasoning behind that prediction was wrong. §4a records it.

## 1. Size the target before choosing the work

| Quantity | Value | Source |
|---|---|---|
| Stockfish 18, single thread, CCRL 40/15 | 3653 Elo | [CCRL, Sept 2026](https://talkchess.com/viewtopic.php?t=86715) |
| Best known Python/Numba engines | ~2200–2300 | [Antares](https://github.com/Alex2262/Antares), [Numbfish](https://github.com/dimdano/numbfish) |
| Our gap | Not measured in a common pool | Cross-pool subtraction is not an Elo estimate |

Stockfish 19 in this workspace is registered at full strength — `Threads 1`, `Hash 64`, no
skill limit, no node cap. A0 0.1.2's 6/6 loss record against it is the expected outcome of that
configuration, not a diagnosis.

The roadmap's success criterion — "positive expected score against a specified Stockfish
configuration" — requires recording that configuration and both agents' budgets. Two further
points make the framing matter:

- **Entrants cannot ship Stockfish.** Third-party engines are prohibited in submissions;
  house engines can still play the public ladder. No entrant-strength ceiling follows from
  the implementation language.
- **Transfer must be tested.** Gains against weakened Stockfish may not transfer to other
  opponents. Keep diverse opponent families alongside the Stockfish yardstick.

## 2. Replace the binary with a dial

Stockfish exposes two calibrated ways to be weaker, and they measure different things. Use both.

| Dial | What it does | What break-even means |
|---|---|---|
| `UCI_LimitStrength true` + `UCI_Elo N` (1320–3190) | Full search, then a randomised bias toward slightly worse moves among ≥4 candidates | An Elo number **calibrated at 120 s + 1 s, anchored to CCRL 40/4** — almost exactly the event clock |
| `nodes N` per move | Genuinely smaller search, no deliberate blunders | Our search efficiency against a shrunken but undamaged opponent |

The `UCI_Elo` calibration matching the event time control is the useful accident here: it turns
"how strong are we" into one directly interpretable number, at the clock we actually play.

Its limitation matters and should be recorded with every result. `UCI_Elo` weakens by *choosing
worse moves*, not by *seeing less*. An engine that breaks even against `UCI_Elo 2000` has beaten
a full-strength searcher that occasionally plays a deliberately inferior move, which is not the
same opponent as a genuinely 2000-rated engine. The node dial has the opposite character and no
blunder injection. Reporting both keeps either from being over-read.
[Stockfish UCI options](https://official-stockfish.github.io/docs/stockfish-wiki/UCI-Protocol-and-Stockfish-Commands.html)

**Proposed primary metric.** `E50` — the `UCI_Elo` at which the candidate scores 50% over
colour-swapped pairs from held-out openings at 120 s + 0.5 s, reported with a pair-level
interval. Secondary: `N50`, the same against a node-limited Stockfish. Every milestone below is
justified by its predicted effect on `E50` and refuted by its measured one.

This is cheap to establish and should be done before any further engine work: it converts an
unmeasurable goal into a number that moves.

## 3. What the new measurement says

Four positions, this host, ~0.25 s of search each, fresh caches, one untimed warm-up.
Small and descriptive, not a benchmark.

| Backend | Throughput | Mean depth | Start-position nodes to depth 4 |
|---|---:|---:|---:|
| A0 0.1.2 (python-chess) | 13,462 nps | 3.2 | 2,171 |
| A1 0.1.0 (Numba, 0x88) | 50,127 nps | 4.0 | 13,376 |

A1 is **3.7× faster per node** and gains **about 0.8 ply**.

An earlier draft of this note read the two node columns above as evidence that A1 needed six
times as many nodes as A0 for the same depth. That was wrong, and the correction matters enough
to keep in the record. Those counts are cumulative totals from *timed* searches, so the two
engines were stopped at different points inside an unfinished deeper iteration; they were never
a like-for-like measurement of cost per depth. Repeating it at a fixed depth with aspiration off:

| Position | A0 nodes to depth 4 | A1 nodes to depth 4 | Ratio | Same move |
|---|---:|---:|---:|:--:|
| start | 2,171 | 2,192 | 1.01× | yes |
| italian | 14,974 | 13,628 | 0.91× | yes |
| closed | 27,976 | 25,032 | 0.89× | yes |
| endgame | 1,633 | 1,578 | 0.97× | yes |

Interior nodes match to within 1% in every case. **A1 has no node penalty against A0**, and the
premise of rung 1 was therefore false before it was written.

The real number is still the effective branching factor, just a smaller one than claimed: 2,192
nodes to depth 4 is an EBF near **6.7** (about 5.0 counting interior nodes alone). Between 89%
and 91% of those nodes are quiescence.

At a fixed 100,000 nodes — roughly what 2 s at A1's current speed buys:

| EBF | Depth reached at 100k nodes |
|---:|---:|
| 6.7 | ~6.0 |
| 3 | ~10.5 |
| 2 | ~16.6 |

Getting the EBF from 6.7 to 3 is worth about **4.5 plies at the same node count**. Buying those
same plies through speed alone would take a factor of roughly 6.7^4.5 — about 5,000× — which no
amount of Numba supplies. A 20 s import budget spent on compilation is a good trade; it is not
the trade that matters most.

The pinned Stockfish 19 binary makes the same point directly, measured on this host at
comparable node counts:

| Engine | Nodes | Depth reached | Implied EBF |
|---|---:|---:|---:|
| A1 | 12,363 | 5 | ~6.6 |
| Stockfish 19 | 16,000 | 11 | ~2.4 |
| Stockfish 19 | 256,103 | 19 | ~1.9 |

Stockfish gets **six more plies out of a comparable number of nodes**, and its EBF falls rather
than rises as the budget grows by 16×. That difference is search selectivity, and it is available
through published techniques rather than through anything proprietary. It is the gap worth
attacking first.

Confirmed by inspection: `null_move`, `lmr`, `reduction`, `futility`, `razor`, `see` and `delta`
appear **zero times** in both `a0/search.py` and `a1/search.py`. Both engines are unpruned PVS
with quiescence, killers and history. The previous profile's finding — 58% of self time in
python-chess — was accurate and led to a worthwhile backend, but it could only ever measure cost
per node, never the number of nodes the search should have avoided.

## 4. The ladder, ordered by Elo per unit of work

Each rung states its expected effect and how it is refuted. Published Elo figures are from other
engines and other time controls; they set expectations, they do not transfer.

| # | Work | Expected | Refuted when |
|---|---|---|---|
| 1 | ~~**Restore A1's TT score bounds and pawn cache.**~~ **Done and measured — see §4a.** | ~~Removes the ~6× node penalty; ~1–2 ply~~ | **Refuted.** There was no node penalty. Worth 14% of nodes and +0.17 ply |
| 2 | ~~**Null-move pruning**~~ **Done — see §4b.** | ~~+100–200 Elo~~ | Held: 0.41× nodes, no tactical or zugzwang regression |
| 3 | ~~**Late move reductions**~~ **Done — see §4b.** | ~~~+100 Elo~~ | Held: 0.08× nodes with rung 2, +3.3 ply |
| 4 | **SEE for capture ordering and pruning**, plus delta pruning in quiescence | Cuts quiescence explosion; ordering is what makes 2 and 3 safe | Quiescence node share does not fall, or losing captures are wrongly cut |
| 5 | **Reverse futility / static null move**, futility at frontier nodes | Tens of Elo each; cheap once 2–4 exist | Horizon losses rise |
| 6 | **A small NNUE, trained here.** 768→N→1 incremental, int16, evaluated in Numba. Weights must be ours: published nets are prohibited, including fine-tuned or re-exported ones. | The remaining large term once search is selective; Numbfish reaches ~2300 with NNUE + numpy | Cost per node cancels the eval gain, or training data leaks engine labels the rules forbid |
| 6a | **Cut the Numba warmup.** Compile fewer signatures at import, or narrow the warmed types. Measured 12–20 s idle and 68.9 s under contention against a hard 90 s budget; four games were lost to it. | Removes a whole-game loss mode | Warm-up falls but first-move latency rises enough to cost time on the clock |
| 7 | **Time management and endgames.** Opening book to move 20 and 4-man Syzygy both fit the 50 MB cap; 5-man WDL does not. | Conversion and clock safety, not raw strength | Book lines reach positions the engine misplays |

Rungs 1–5 are ordinary engine work with well-understood mechanisms, they compound, and they are
measurable in fixed-node terms before any game is played. Rung 6 is the largest single remaining
term and also the largest project; it should not start until the search around it is selective,
or the two changes will be inseparable.

## 4a. Rung 1, as measured

Transposition score bounds are implemented in `a1/search.py`: entries now carry a packed score,
a depth and a bound alongside the move hint, with mate scores packed relative to ply. A bound is
never reused at the root, where the caller still needs the move the node chooses.

Reuse has to be conditioned on draw context, because whether a position is drawn depends on the
halfmove clock and on what the path has already visited. Two policies were implemented and both
were measured over six positions at a fixed depth 5:

| Policy | Nodes vs no bounds | Mean depth in 2 s | Same move everywhere |
|---|---:|---:|:--:|
| No score reuse (hints only) | 1.00× | 5.50 | — |
| Strict — bound tied to the whole path, as A0 does | **0.995×** | 5.50 | yes |
| Relaxed — bound tied to the halfmove clock | **0.86×** | 5.67 | yes |

**Strict reuse saved little in this small measurement.** A path sum fingerprints the multiset
of visited positions, not their order; transpositions often have different intermediate boards.
It is also a probabilistic fingerprint rather than an exact history comparison.

Relaxed reuse saves 7–22% of nodes and gains 0.17 ply on average. It is now the default, with
strict retained as an ablation. The residual risk is the ordinary graph-history-interaction
approximation every engine makes: a bound can come from a subtree whose internal repetitions
differed from this path's. What makes it tolerable here is that every node already tests
repetition, the fifty-move counter and insufficient material *before* it probes, so a position
that is itself drawn never reads a stored score.

Two tests now guard this: all three storage policies must return the same move and score at a
fixed depth across four positions, and a search from a twice-repeated position must not walk
into the third repetition. Both pass, along with the eleven existing A1 tests including
agreement with an independent exhaustive reference.

**What this rung teaches about the ladder.** The predicted gain was 1–2 ply and the measured
gain was 0.17. The prediction came from a node comparison that was not like-for-like, and no
amount of care in the writing would have caught that — only running it did. Rungs 2 and 3 are
supported by published results from other engines rather than by anything measured here, so
they deserve the same treatment: implement, measure against the frozen previous build, and
publish the number even when it embarrasses the estimate.

## 4b. Rungs 2 and 3, as measured

Both are implemented in `a1/search.py` and shipped as checkpoint **A1 0.2.0**.

Null-move pruning: depth ≥ 3, not in check, not at the root, beta below mate range, and the side
to move must hold a piece other than pawns and king. Reduction is `2 + depth // 6`. A mate found
behind a pass is reported as the bound rather than as a mate, because a mate that needs the
opponent to pass is not forceable.

**Corrected in 0.2.1:** a null position can create a false repetition. King triangulation can
restore placement after five real plies, then a pass restores side-to-move. Two passes can also
undo each other. The new regression reproduces the triangulation case. Synthetic subtrees now
disable history-based draw claims, nested null moves, and score-cache reads/writes; intrinsic
terminal positions are still recognised. The following node counts remain historical 0.2.0 data.

Late move reductions: quiet moves only, from the fourth move on, depth ≥ 3, not in check and not
giving check. Reduction is `1 + (depth - 3) // 4 + (i - 3) // 8`, and any reduced move that beats
alpha is re-searched at full depth before its score is used.

Six positions, fixed depth 6, aspiration off:

| Position | Baseline nodes | + null-move | + LMR | Ratio |
|---|---:|---:|---:|---:|
| start | 52,326 | 27,367 | 6,496 | 0.12× |
| italian | 399,437 | 165,642 | 18,126 | 0.05× |
| closed | 679,021 | 362,435 | 19,050 | 0.03× |
| kid | 270,078 | 94,150 | 18,811 | 0.07× |
| open | 670,465 | 127,825 | 36,685 | 0.05× |
| endgame | 14,772 | 9,429 | 5,908 | 0.40× |
| **geometric mean** | 1.00× | **0.41×** | **0.08×** | |

What that buys where it counts, in a fixed 2,000 ms budget:

| | Baseline | + null-move | + LMR |
|---|---:|---:|---:|
| Mean depth over the six positions | 5.17 | 5.83 | **8.50** |

**+3.3 plies for the same wall clock.** The implied branching factor falls from about 6.7 to
about 3.6, which is the range §3 identified as the target. Five of six positions choose the same
move as the unpruned search at depth 6; the italian prefers `b1c3` over `f3g5`, which is what an
approximate heuristic is expected to do and is why games decide this, not node counts.

Guards, all passing and all now in `chesslab/tests/test_a1.py`:

- Four forced mates — back rank, smothered, ladder, queen — found with the same move and a mate
  score under all three pruning configurations.
- Two zugzwang positions where passing is exactly the wrong idea return an identical move and
  score with and without null-move pruning.
- A null move restores placement and metadata exactly.
- A reduced search never reports a mate the unpruned search does not also see.

Seventeen A1 tests pass, including agreement with an independent exhaustive reference and perft
against python-chess. A two-game smoke against frozen A0 0.1.2 through the unchanged referee
completed 1 win and 1 draw with no failures.

**Still unmeasured: whether this wins games.** Node counts and depth are not strength. The
paired experiments below are what decide it, and they need a machine to run on.

## 4c. The games, 11 September 2026

140 games, four experiments run concurrently on a 16-thread workstation. Each run records its
own `parallel_games`, so none of these will be compared against a sequential run by mistake.
Read the contention caveat under each table; it is not the same for every experiment.

### A1 0.2.0 beats A0 0.1.2, and now says so at the preregistered bar

| | |
|---|---|
| Result | **37 W · 1 D · 2 L** over 40 games |
| Openings | all 20 development positions, colours swapped, 20 complete pairs |
| Pair score | **0.938** |
| 95% Hoeffding bound | **0.634 – 1.000** |
| Candidate failures | 1 |

The bound excludes 50%. The earlier 20-game run scored 0.90 but its bound reached down to 0.47,
so this is the run that settles it. Two games at once at 10 s + 100 ms: both engines are
wall-clock limited, so contention dilutes them together.

### The Elo dial: `E50` is near 1800, and the sampling says so loudly

| Opponent | W | D | L | Score | 95% bound |
|---|---:|---:|---:|---:|---|
| `sf-elo-1320` | 10 | 0 | 0 | 1.00 | 0.39 – 1.00 |
| `sf-elo-1600` | 9 | 0 | 1 | 0.90 | 0.29 – 1.00 |
| `sf-elo-1800` | 5 | 0 | 5 | **0.50** | 0.00 – 1.00 |
| `sf-elo-2000` | 8 | 0 | 2 | 0.80 | 0.19 – 1.00 |

Zero failures in 40 games at the full event clock.

**The ordering is not monotone, and that is the finding.** Scoring 0.80 against 2000 after 0.50
against 1800 is not a statement about chess; it is five opening pairs per rung against an
opponent whose weakening is *randomised move selection*, which is a high-variance process by
construction. Every bound spans at least 0.61.

### Sequential confirmation at 1800

Twenty games, ten openings, one at a time on an otherwise idle machine, 94 minutes:

| | |
|---|---|
| Result | **13 W · 0 D · 7 L**, 10 complete pairs |
| Pair score | **0.650** |
| 95% Hoeffding bound | 0.221 – 1.000 |
| Candidate failures | **0** |
| Terminations | 20 of 20 by checkmate |

Two things this settles and one it does not.

It settles that the `init` failures were an artefact of contention: **zero failures in twenty
games at the event clock** when the machine was not shared. It also settles that A1 0.2.0 is
above even with `sf-elo-1800` rather than level with it, so **`E50` lies above 1800** — the
combined evidence at that rung is 18 W · 12 L over 30 games.

It does not settle where `E50` actually is. The Hoeffding bound still spans 0.22–1.00, because
ten pairs is ten pairs. Calling it **1800–2000** is as precise as the evidence supports.

Worth recording: the same rung scored 0.50 contended and 0.65 uncontended. A plausible reason
is that contention costs A1 real search while costing `UCI_Elo` Stockfish less, since its
handicap is randomised move choice rather than reduced depth — so a shared machine penalises
the side whose strength is compute. That is a hypothesis from two samples, not a finding, and
it is an argument for measuring `E50` sequentially whatever else is run in parallel.

No draws in twenty games, and no clock trouble at 120 s + 0.5 s.

### Diagnostic regression: no strength regression, one real risk

38 W · 0 D · 4 L across seven families. **All four losses were `init` failures at zero plies** —
the process never started. No flags, no crashes, no illegal moves, so A1's clock handling and
move legality held up under load; only its startup did not.

The cause is A1's Numba warmup. Idle it is 12–20 s. Measured **68.9 s under this load**, against
the platform's hard 90 s `INIT_BUDGET_S`. Four games compiled during peak contention and lost
outright before playing a move.

Rungs 2 and 3 did not cause it. Under identical load, **0.1.0 warmed in 73.3 s and 0.2.0 in
60.7 s**; adding null-move pruning and reductions did not make compilation slower. The
fragility is inherent to compiling the whole search at import.

That still deserves its own rung. An `init` timeout is a whole game, which is a larger expected
loss than any of the search refinements below is likely to gain, and 12–20 s idle is thinner
cover against 90 s than it appears if the platform's machine is ever cold or shared.

### The node dial: discard this one

1 W · 17 L against `sf-nodes-1000`, `4000` and `16000`. It should not be read at all, because
of a mistake in how it was run: **a node-limited engine is immune to CPU contention.** It gets
its thousand nodes whether it holds a full core or a tenth of one, while A1's 120-second wall
clock is diluted by everything else on the machine. Running it beside three other experiments
systematically favoured the node-limited side. `N50` needs a sequential re-run before it means
anything, and until then no conclusion about search efficiency follows from it.

The Elo dial does not share this flaw, which is why it is reported above and this is not.

## 5. What to deprioritise, and why

The exploitation roadmap's M3–M4 — trajectory mining, opponent-response models, selective
verification — are well designed and should not be started yet.

The arithmetic is the argument. Those mechanisms aim to convert an opponent's occasional bad
decision into a game result. Against an opponent 1,350 Elo stronger, almost every position is
already lost by the time any induced error could be reached; the budget probe's own finding was
that the one dramatic low-budget error disappeared at the next budget and did not affect the
game. Against opponents near our own strength — the ones we actually play — the same effort
spent on rungs 2–4 raises the score in every game rather than in rare constructed ones.

The Go adversarial-policy result is real, and it is the strongest evidence for the idea, but it
was produced by training a dedicated adversary with large compute against a fixed frozen target.
Neither the compute nor the fixed target exists here: opponents are other teams' agents, and they
change daily.

Revisit M3–M4 once `E50` stops responding to engine work. That is the honest trigger, and it is
also the point at which exploitation becomes the only remaining move.

## 6. First actions

0. **Done in this note.** The `UCI_Elo` and node dials are registered as thirteen separate
   opponents (`sf-elo-1320` … `sf-elo-2800`, `sf-nodes-1000` … `sf-nodes-256000`), and the lab's
   UCI adapter now honours a `max_nodes` search limit, which it previously could not express.
   Both dials were confirmed against the pinned binary: exact node counts honoured, depth 5/11/19
   at 1k/16k/256k, `UCI_Elo` accepted over its full 1320–3190 range.

1. **Establish `E50` and `N50` for A0 0.1.2 and A1 0.1.0.** A bisection over `UCI_Elo` — start at
   1320, 1800, 2400 — with colour-swapped pairs at 120 s + 0.5 s. This is the baseline every
   later claim is measured against, and nothing else should start before it exists.
2. ~~Close A1 rung 1~~ **— done, measured and written up in §4a. It did not do what this note
   predicted, and the fixed-node comparison it called for is what showed why.**
3. ~~Rungs 2 and 3~~ **— done and measured in §4b, and packaged as checkpoint `a1-020`.**
4. **Run the two paired experiments.** `chesslab/experiments/a1-0.2.0-head-to-head.json` is 20
   games against frozen A0 0.1.2 at 10 s + 100 ms; `a1-0.2.0-ladder.json` is 40 games against
   the Elo dial at the full 120 s + 0.5 s event clock, which is what establishes `E50`.
5. **Then rung 4**, SEE ordering and delta pruning in quiescence. Quiescence is still 89–91% of
   all nodes, so it is where the next large reduction is, and it is what makes the reductions
   already in place safer rather than riskier.

The existing lab already supports every one of these: frozen builds, colour-swapped pairs,
paired statistics, fixed-node comparison and replay verification. With step 0 done, no new
infrastructure is needed to start.

## 7. Limits of this note

The throughput table is four positions on one Windows host at ~0.25 s per search, with no
interval; it establishes an order of magnitude and the direction of the bottleneck, nothing
finer. It is also the table that produced this note's one substantive error, and the reason §3
now carries a fixed-depth comparison beside it. EBF derived from a single search is a crude
estimate and varies by position and phase. The rung 1 table in §4a is six positions on the same
host with no repeats and no interval; it separates three policies confidently enough to choose
between them and is not a strength result. The published Elo figures for null-move pruning, LMR and NNUE come from
other engines at other time controls and are expectations, not predictions for this codebase.
No claim is made here that any rung will produce its stated gain; each is stated so that it can
fail visibly.

The 1,350 Elo gap estimate rests on a CCRL single-thread rating measured at 40/15 and on
third-party Python engine ratings from a different rating pool. Treat it as an order of
magnitude. It would have to be wrong by roughly a factor of three before "beat full-strength
Stockfish 19" became a reasonable objective.

## Sources

- [CCRL rating lists, 5 September 2026](https://talkchess.com/viewtopic.php?t=86715)
- [Stockfish UCI options and strength limiting](https://official-stockfish.github.io/docs/stockfish-wiki/UCI-Protocol-and-Stockfish-Commands.html)
- [Stockfish useful data — node and depth scaling](https://official-stockfish.github.io/docs/stockfish-wiki/Useful-data.html)
- [Null move pruning test results](https://www.chessprogramming.org/Null_Move_Pruning_Test_Results)
- [Late move reductions](https://en.wikipedia.org/wiki/Late_move_reductions)
- [black_numba — Numba bitboard engine, 7.3k to 1.5M nps](https://github.com/Avo-k/black_numba)
- [Antares — Python/Numba engine](https://github.com/Alex2262/Antares)
- [Numbfish — Python NNUE engine](https://github.com/dimdano/numbfish)
- [Fishtest mathematics — paired testing and SPRT](https://official-stockfish.github.io/docs/fishtest-wiki/Fishtest-Mathematics.html)
