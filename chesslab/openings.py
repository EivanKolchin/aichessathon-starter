"""Public development positions, never a claim about the hidden event opening set."""

from dataclasses import asdict, dataclass

import chess


@dataclass(frozen=True)
class Opening:
    id: str
    name: str
    family: str
    split: str
    fen: str

    def data(self) -> dict[str, str]:
        return asdict(self)


# Hold out whole opening families. These are public development/validation splits,
# not a secret final benchmark. All lines are checked for legality at startup.
LINES = (
    ("italian", "Italian game", "Open games", "dev", "e4 e5 Nf3 Nc6 Bc4 Bc5 c3 Nf6"),
    ("scotch", "Scotch game", "Open games", "dev", "e4 e5 Nf3 Nc6 d4 exd4 Nxd4 Nf6"),
    ("petroff", "Petroff defence", "Open games", "dev", "e4 e5 Nf3 Nf6 Nxe5 d6 Nf3 Nxe4"),
    ("ruy", "Ruy Lopez", "Open games", "dev", "e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O Be7"),
    ("najdorf", "Sicilian Najdorf", "Sicilian", "dev", "e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 a6"),
    ("dragon", "Sicilian Dragon", "Sicilian", "dev", "e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 g6"),
    ("alapin", "Sicilian Alapin", "Sicilian", "dev", "e4 c5 c3 Nf6 e5 Nd5 d4 cxd4"),
    ("closed", "Closed Sicilian", "Sicilian", "dev", "e4 c5 Nc3 Nc6 g3 g6 Bg2 Bg7"),
    ("french", "French advance", "French", "dev", "e4 e6 d4 d5 e5 c5 c3 Nc6 Nf3 Qb6"),
    ("winawer", "French Winawer", "French", "dev", "e4 e6 d4 d5 Nc3 Bb4 e5 c5 a3 Bxc3+ bxc3"),
    ("tarrasch", "French Tarrasch", "French", "dev", "e4 e6 d4 d5 Nd2 c5 exd5 exd5 Ngf3"),
    ("french-ex", "French exchange", "French", "dev", "e4 e6 d4 d5 exd5 exd5 Nf3 Nf6 Bd3"),
    ("qgd", "Queen's gambit declined", "Queen's gambit", "dev", "d4 d5 c4 e6 Nc3 Nf6 Bg5 Be7"),
    ("qga", "Queen's gambit accepted", "Queen's gambit", "dev", "d4 d5 c4 dxc4 Nf3 Nf6 e3 e6"),
    ("slav", "Slav defence", "Queen's gambit", "dev", "d4 d5 c4 c6 Nf3 Nf6 Nc3 dxc4 a4 Bf5"),
    ("semi-slav", "Semi-Slav", "Queen's gambit", "dev", "d4 d5 c4 c6 Nf3 Nf6 Nc3 e6 e3 Nbd7"),
    ("english", "English symmetrical", "Flank openings", "dev", "c4 c5 Nc3 Nc6 g3 g6 Bg2 Bg7"),
    ("reti", "Reti opening", "Flank openings", "dev", "Nf3 d5 c4 e6 g3 Nf6 Bg2 Be7"),
    ("bird", "Bird opening", "Flank openings", "dev", "f4 d5 Nf3 Nf6 e3 g6 b3 Bg7"),
    ("larsen", "Larsen opening", "Flank openings", "dev", "b3 e5 Bb2 Nc6 e3 Nf6 Bb5 Bd6"),
    ("caro", "Caro-Kann classical", "Caro-Kann", "validation", "e4 c6 d4 d5 Nc3 dxe4 Nxe4 Bf5"),
    ("caro-adv", "Caro-Kann advance", "Caro-Kann", "validation", "e4 c6 d4 d5 e5 Bf5 Nf3 e6"),
    ("panov", "Panov attack", "Caro-Kann", "validation", "e4 c6 d4 d5 exd5 cxd5 c4 Nf6 Nc3"),
    ("caro-ex", "Caro-Kann exchange", "Caro-Kann", "validation", "e4 c6 d4 d5 exd5 cxd5 Bd3 Nc6"),
    ("kid", "King's Indian", "Indian defences", "validation", "d4 Nf6 c4 g6 Nc3 Bg7 e4 d6 Nf3 O-O"),
    ("nimzo", "Nimzo-Indian", "Indian defences", "validation", "d4 Nf6 c4 e6 Nc3 Bb4 e3 O-O Bd3"),
    (
        "grunfeld",
        "Grunfeld defence",
        "Indian defences",
        "validation",
        "d4 Nf6 c4 g6 Nc3 d5 cxd5 Nxd5 e4",
    ),
    (
        "queens-indian",
        "Queen's Indian",
        "Indian defences",
        "validation",
        "d4 Nf6 c4 e6 Nf3 b6 g3 Bb7 Bg2",
    ),
    ("dutch", "Dutch defence", "Asymmetric defences", "validation", "d4 f5 g3 Nf6 Bg2 g6 Nf3 Bg7"),
    ("pirc", "Pirc defence", "Asymmetric defences", "validation", "e4 d6 d4 Nf6 Nc3 g6 Nf3 Bg7"),
    ("modern", "Modern defence", "Asymmetric defences", "validation", "e4 g6 d4 Bg7 Nc3 d6 f4 a6"),
    (
        "alekhine",
        "Alekhine defence",
        "Asymmetric defences",
        "validation",
        "e4 Nf6 e5 Nd5 d4 d6 Nf3 Bg4",
    ),
)


def catalog() -> list[Opening]:
    result = []
    for key, name, family, split, line in LINES:
        board = chess.Board()
        for san in line.split():
            board.push_san(san)
        result.append(Opening(key, name, family, split, board.fen()))
    return result
