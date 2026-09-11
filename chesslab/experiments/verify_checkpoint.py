"""Package a local candidate and exercise the extracted copy through the unchanged runner."""

import argparse
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import chess

from chesslab.registry import file_hash, write_json
from harness.package import build
from harness.rules import INIT_BUDGET_S, MAX_UNZIPPED_BYTES
from harness.sandbox import local


def verify(checkpoint: Path, output: Path, positions: Path) -> dict[str, Any]:
    if output.exists():
        raise ValueError("Choose a new verification directory")
    output.mkdir(parents=True)
    upload = output / "agent.zip"
    files = build(checkpoint, upload, ("weights",))
    cases = json.loads(positions.read_text(encoding="utf-8"))["trials"]
    trials: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "checkpoint": str(checkpoint.resolve()),
        "files": files,
        "zip_sha256": file_hash(upload),
        "parent_compiler_overrides": {
            key: os.environ.get(key) for key in ("NUMBA_OPT", "NUMBA_LLVM_REFPRUNE_FLAGS")
        },
        "positions_sha256": file_hash(positions),
        "limits": "Local runner: init limit and reply protocol. Not Linux container isolation."
        " Fresh/resynchronised FENs; no forced increment estimate.",
        "trials": trials,
    }
    with tempfile.TemporaryDirectory(prefix="chesslab-verify-") as scratch:
        root = Path(scratch)
        with ZipFile(upload) as archive:
            size = sum(item.file_size for item in archive.infolist())
            if size > MAX_UNZIPPED_BYTES or "agent.py" not in archive.namelist():
                raise ValueError("Invalid submission size or entrypoint")
            # Archive was just constructed from our controlled local checkpoint.
            archive.extractall(root)
        report["unzipped_bytes"] = size
        agent = local(root)
        try:
            started = time.perf_counter()
            agent.start(INIT_BUDGET_S)
            report["init_seconds"] = time.perf_counter() - started
            for case in cases:
                reference = chess.Board(case["fen"])
                tick = time.perf_counter()
                move = agent.move(reference.fen(), case["remaining_ms"])
                elapsed = (time.perf_counter() - tick) * 1000
                trials.append(
                    {
                        "fen": reference.fen(),
                        "remaining_ms": case["remaining_ms"],
                        "move": move,
                        "elapsed_ms": elapsed,
                        "legal": chess.Move.from_uci(move) in reference.legal_moves,
                        "within_clock": elapsed < case["remaining_ms"],
                    }
                )
            report["passed"] = all(row["legal"] and row["within_clock"] for row in trials)
        except Exception as error:
            report["passed"] = False
            report["error"] = f"{type(error).__name__}: {error}"
        finally:
            agent.stop()
            (output / "agent.log").write_text(agent.stderr_log, encoding="utf-8")
    write_json(output / "verification.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--positions", type=Path, default=Path("docs/research/results/a0-0.1.2-user-low-clock.json")
    )
    args = parser.parse_args()
    report = verify(args.checkpoint, args.out, args.positions)
    print(json.dumps({key: value for key, value in report.items() if key != "trials"}, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
