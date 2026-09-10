# Research system implementation — 10 September 2026

The research infrastructure is implemented in `chesslab/`, and the root competition agent
now runs the original A0 engine in `a0/`. The event `harness/` remains unchanged.

## Delivered

- Local browser arena with a redesigned board, live move list and recorded clocks.
- Separate experiment setup panel, recent-run navigation and engine registry panel.
- Replay controls, board flipping, FEN copy, PGN download and complete experiment export.
- Eight runnable Python diagnostic opponents in six families. Stockfish 19 is installed in
  this workspace as a ninth opponent and seventh family; Numba remains an optional dependency.
- Frozen A0 0.1.0 and 0.1.2 references are registered for subsequent candidates.
- Registration of original Python checkpoints and external UCI engines, including engines
  using separately declared networks or other assets.
- 32 legal public opening positions, with development and validation families separated.
- Sequential colour-swapped matches through the unchanged event referee.
- Frozen Python build copies; engine configurations, source/asset hashes, clocks, PGNs and logs.
- Headless experiment requests and comparisons that match opponent build, FEN, seed and budget.
- Process failure handling, UCI wall-clock watchdog, single-writer storage and restart tracking.
- Original A0 engine: incremental tapered evaluation, iterative deepening PVS, quiescence,
  move ordering, bounded transposition storage, repetition context and clock allocation.
- Fixed development league and random-reference protocols, plus a reproducible fixed-node
  profiling command with search ablation switches and source snapshots.
- A complete evaluator snapshot for the first matched study, protecting its source from
  concurrent application development.
- A0 0.1.2: cheaper quiet-promotion generation, precise deadlines and a larger clock reserve.
- A repeated fixed-node comparison tool with frozen builds, raw timings and exact search checks.
- A read-only export auditor for legal replay, source hashes, failure positions and corrected
  reliability counts, including clock failures scored as draws.

The lab is a working evaluator. Its small diagnostic opponents are not trained neural engines.
A0's initial league is documented in `a0-results.md`; the latest speed and reliability study,
including the supplied 70-game export, is in `a0-0.1.2-results.md`. These establish development
references, not competitive strength against Stockfish. Submission packaging excludes the lab
and external engines.

## Verified

- Ruff passed across the repository.
- Mypy strict checking passed for 39 source files covering the agent, A0, harness and lab.
- 41 A0/core-lab unit and integration tests passed, including actual Python/UCI process matches, illegal output,
  startup crashes, timeout enforcement, exact PGN replay, special moves, build freezing,
  paired statistics, comparison compatibility and local HTTP controls.
- A0 tests cover incremental evaluation and undo, special moves, mirror symmetry, mate and draw
  handling, underpromotion, deadline restoration, history reconstruction and agreement between
  PVS/transposition caching and an independent exhaustive reference on small positions.
- The latest full workspace test run passed all 102 tests, including export auditing, failure
  accounting and the additional ladder, runner and sparring tests developed alongside A0.
- Playwright checks passed in Edge: board square colours, replay, flip, real batch launch,
  live updates, PGN/ZIP download, engine registry, mobile layout and mobile setup panel.
- A 16-game smoke batch and subsequent UI batches completed. The real Stockfish 19 adapter
  completed four full-cap games, all ending in checkmate, without protocol failures.
- A0 won both games in the existing two-game arena gate against random, without agent failures.
- The frozen matched study completed 48 games: A0 scored 18 wins and six losses, and the random
  reference lost all 24. All six A0 losses were against Stockfish; no agent failures occurred.
- The 0.1.2 fixed-node study matched all 116 search-result comparisons and measured approximately
  11.8% higher throughput over the development positions. This is descriptive local timing.
- The 0.1.2 studies completed 52 games with no candidate failures: 14/3/3 against 0.1.0,
  18 wins against weak diagnostics, six losses against Stockfish and eight wins in the
  supplied-game clock regression. All head-to-head wins and regression wins were checkmates.
- The supplied export's 70 completed games and 3,667 accepted plies replayed consistently.
  Its four timeouts against Solid search included two draws omitted by the old failure count.
  Twenty direct checks of the exact failure positions passed after the clock fix.
- The final study loads correctly in Chess Lab on desktop and mobile: 64 board squares, 24 game
  records, working replay, no horizontal mobile overflow and no JavaScript errors.
- The 0.1.2 regression study also passed browser checks for replay, board flipping and mobile
  layout; the supplied run displays four candidate failures after the summary correction.
- Submission packaging passed both extracted-archive smoke games. The zip contains `agent.py`,
  the four `a0` Python modules and their README: 35,070 bytes uncompressed. All packaged files
  were verified byte-for-byte identical to the current source.

Local results and build copies live in `.chesslab/`, which is excluded from version control.
See `chesslab/README.md` for commands, limitations and how to register additional engine families.

## Next milestone

Keep 0.1.2 as the reference. Profile the quiescence work in the supplied Stockfish loss positions,
then compare individual overhead or search-policy changes against the frozen build. Measure
tactical correctness, search effort, time management and full paired games separately. The
current Python engine usually reached only depth 2–3 in the retained Stockfish logs. An original
compiled search/move-generation backend is the next larger architectural experiment, followed
by A1's independently trained evaluator. Mating conversion deserves a separate development suite.

The architecture-search and self-improvement stages will consume the lab's headless JSON
requests and matched run records. Training, genetic/evolutionary proposal generation and
automatic promotion are not implemented yet. Final promotion needs a separate preregistered
test and an unseen opening set, as described in `stockfish-research-plan.md`.
