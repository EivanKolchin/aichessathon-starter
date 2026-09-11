# A1 — original compiled-search experiment

Version **0.2.2** additionally defaults to LLVM optimisation level 1 before importing Numba.
The default level 3 caused startup timeouts in the first paired hybrid run. Compile cost and
playing speed are both experiment metrics; an offline `NUMBA_OPT` override enables ablations.

Working version **0.2.1** verifies all six position-identity words before reusing cached scores.
Synthetic null subtrees cannot claim real-history repetition/fifty-move draws, read/write score
bounds, or make further null moves. Null pruning is restricted to non-mate null windows.
It remains heuristic: having a non-pawn piece does not rule out zugzwang.

The search now accepts a compiled evaluator. `neural.py` supplies an original **768→32→1**
positional residual pilot trained from scratch, with a classical-only control. It refreshes
sparse features at leaves; incremental accumulators and quantisation are future experiments.
See [the hybrid study](../docs/research/hybrid-pilot.md) for reproducible commands and measured
results. Frozen 0.1.0 and 0.2.0 checkpoints remain separate from these changes.

Version 0.2.0 adds null-move pruning and late move reductions on top of 0.1.0's compiled
backend and 0.1.x's transposition score bounds. Measured over six positions at a fixed depth
these cut nodes to 0.08× and gain about 3.3 plies in a 2-second budget; four forced mates and
two zugzwang positions are unchanged. Whether that wins games is what the paired experiments
in `chesslab/experiments/a1-0.2.0-*.json` are for.

Version 0.1.0 remains a separately selectable research candidate. The root submission continues
to use A0 0.1.2 until a later promotion decision.

The 0x88 mailbox, attack detection, legal moves, make/unmake, exact packed repetition identities,
search loops and evaluation implementation are original Python compiled in memory by Numba.
Evaluation formulas, iterative deepening, PVS, an eight-ply quiet quiescence limit and clock
allocation retain the A0 starting design. Move generation/order differs, and pawn features are
recalculated instead of using A0's Python pawn cache.

Transposition entries carry a full identity, move hint, packed score, depth and bound. Score
reuse checks exact position identity and never happens at the root. Two context policies exist:
`strict_draw_context=True` adds a probabilistic fingerprint of the visited-position multiset.
Different move orders often visit different intermediate positions, restricting reuse; the default
`False` ties it to the halfmove clock and relies on the repetition, fifty-move and
insufficient-material tests each node runs before probing. That is the usual graph-history-
interaction approximation. The three storage policies agree on the small regression suite;
this is not a general proof of equivalence. `use_tt=False` disables
score reuse entirely for ablation. Measured effect and limits are in
[`docs/research/development-path-to-stockfish.md`](../docs/research/development-path-to-stockfish.md). Perft supplies an identical-tree board traversal control.

Production signatures are warmed during `a1.engine` import. No compilation cache, external
engine, pretrained model, network request or background search is used. The Python boundary uses
python-chess for FEN validation, between-move history recovery and a final legal-move check.
Draw identity distinguishes capturable en passant, castling rights and side to move; checkmate
precedes fifty-move draws. Search stop signals unwind every board update before returning.

Build a separate checkpoint with `python -m chesslab.experiments.build_a1 --out <new-directory>`.
It contains an A1 entrypoint plus the original `a0` support and `a1` sources. Register that
directory in Chess Lab to run games. It does not replace the root `agent.py` or upload anything.

For a hybrid checkpoint, add `--model <model-directory>/model.npz`. An adjacent `training.json`
must declare training from scratch and match the weights' SHA-256. The builder packages only
source, model weights and training provenance; it does not package teacher binaries or datasets.
`--blend 0` is an exact classical control. The neural checkpoint warms its own search signature
before accepting moves. A fresh search instance is required when changing evaluators or weights.
