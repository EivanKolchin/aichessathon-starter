"""Minimal UCI protocol fixture. It is not a benchmark chess engine."""

import sys

import chess


def main() -> None:
    board = chess.Board()
    for line in sys.stdin:
        words = line.strip().split()
        if not words:
            continue
        if words[0] == "uci":
            print("id name Chess Lab test fixture")
            print("option name Threads type spin default 1 min 1 max 8")
            print("option name Hash type spin default 16 min 1 max 256")
            print("uciok", flush=True)
        elif words[0] == "isready":
            print("readyok", flush=True)
        elif words[0] == "position":
            board = chess.Board() if words[1] == "startpos" else chess.Board(" ".join(words[2:8]))
            if "moves" in words:
                for move in words[words.index("moves") + 1 :]:
                    board.push_uci(move)
        elif words[0] == "go" and "--hang" not in sys.argv:
            print("bestmove " + next(iter(board.legal_moves)).uci(), flush=True)
        elif words[0] == "quit":
            return


if __name__ == "__main__":
    main()
