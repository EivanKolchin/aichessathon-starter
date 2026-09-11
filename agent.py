"""The submission entrypoint. The platform imports this file and calls get_move."""

from a1.engine import ChessAgent

# Original A1 compiled search; the permitted chess library supplies legal moves and FEN parsing.
# Numba compiles every production signature during this import, inside the platform's init budget.
# This instance preserves board history and transposition storage within one game.
AGENT = ChessAgent()


def get_move(fen: str, time_left_ms: int) -> str:
    """Return a legal move in UCI notation.

    fen           the position to move in, and your colour is the side to move
    time_left_ms  your clock before this move, in milliseconds
    returns       "e2e4", or "e7e8q" for a promotion

    The process stays alive, but suspended between your moves, so state you keep on a module or in a
    closure survives to the next call. It does not survive to the next game

    print() is safe. Your stdout is redirected away from the protocol stream and kept in a
    log only your team can read, after validation and after every rated game.
    """
    return AGENT.get_move(fen, time_left_ms)
