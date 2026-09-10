"""Build a separately selectable A1 checkpoint without replacing the working agent."""

import argparse
import shutil
from pathlib import Path
from typing import Any

from chesslab.registry import ROOT, file_hash, write_json

ENTRYPOINT = '''"""Experimental original A1 engine; compiled during module import."""

from a1.engine import ChessAgent

AGENT = ChessAgent()


def get_move(fen: str, time_left_ms: int) -> str:
    return AGENT.get_move(fen, time_left_ms)
'''


def build(destination: Path) -> dict[str, Any]:
    if destination.exists():
        raise ValueError("Choose a new checkpoint directory")
    destination.mkdir(parents=True)
    (destination / "agent.py").write_text(ENTRYPOINT, encoding="utf-8")
    files = {"agent.py": file_hash(destination / "agent.py")}
    for package in ("a0", "a1"):
        for source in sorted((ROOT / package).rglob("*")):
            if not source.is_file() or "__pycache__" in source.parts:
                continue
            if source.suffix not in (".py", ".md"):
                raise ValueError(f"Unexpected experiment asset: {source.name}")
            relative = source.relative_to(ROOT)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            files[relative.as_posix()] = file_hash(target)
    report = {"path": str(destination.resolve()), "files": files, "entrypoint": "A1 experiment"}
    write_json(destination / "build.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = build(args.out)
    print(f"Built {len(report['files'])} source files in {report['path']}")


if __name__ == "__main__":
    main()
