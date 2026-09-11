"""Classical A1 entrypoint: compile the selected search before the playing clock."""

from a1.runtime import ChessAgent as ChessAgent
from a1.search import warmup

# No disk compilation cache. A hybrid entrypoint instead warms its own evaluator once.
warmup()
