# A1 — original compiled-search experiment

Version 0.1.0 is a separately selectable research candidate. The root submission continues
to use A0 0.1.2 until a later promotion decision.

The 0x88 mailbox, attack detection, legal moves, make/unmake, exact packed repetition identities,
search loops and evaluation implementation are original Python compiled in memory by Numba.
Evaluation formulas, iterative deepening, PVS, an eight-ply quiet quiescence limit and clock
allocation retain the A0 starting design. Move generation/order differs. The first experiment
uses transposition move hints only, with no score-bound reuse across histories, and recalculates
pawn features instead of using A0's Python pawn cache. These are material differences when
interpreting search-throughput results. Perft supplies an identical-tree board traversal control.

Production signatures are warmed during `a1.engine` import. No compilation cache, external
engine, pretrained model, network request or background search is used. The Python boundary uses
python-chess for FEN validation, between-move history recovery and a final legal-move check.
Draw identity distinguishes capturable en passant, castling rights and side to move; checkmate
precedes fifty-move draws. Search stop signals unwind every board update before returning.

Build a separate checkpoint with `python -m chesslab.experiments.build_a1 --out <new-directory>`.
It contains an A1 entrypoint plus the original `a0` support and `a1` sources. Register that
directory in Chess Lab to run games. It does not replace the root `agent.py` or upload anything.
