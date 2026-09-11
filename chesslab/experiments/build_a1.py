"""Build a separately selectable A1 checkpoint without replacing the working agent."""

import argparse
import json
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


def build(destination: Path, model: Path | None = None, blend: float = 1.0) -> dict[str, Any]:
    if destination.exists():
        raise ValueError("Choose a new checkpoint directory")
    entrypoint = ENTRYPOINT
    if model is not None:
        from a1.neural import load_weights

        load_weights(model)
        provenance = json.loads(model.with_name("training.json").read_text(encoding="utf-8"))
        if not provenance.get("trained_from_scratch") or provenance["model_sha256"] != file_hash(
            model
        ):
            raise ValueError("Model must match its original-training provenance")
        if not 0 <= blend <= 1:
            raise ValueError("Neural blend must be between 0 and 1")
        entrypoint = '''"""Original hybrid pilot; weights and JIT loaded before the game clock."""
from pathlib import Path
import chess
from a1.runtime import ChessAgent
from a1.neural import create_evaluator, load_weights
from a1.search import Search, SearchConfig

EVALUATOR = create_evaluator(load_weights(Path(__file__).parent / "weights" / "model.npz"), BLEND)
Search(SearchConfig(max_depth=2), EVALUATOR).analyse(chess.Board(), 60000, 60000)
AGENT = ChessAgent(search=Search(evaluator=EVALUATOR))

def get_move(fen: str, time_left_ms: int) -> str:
    return AGENT.get_move(fen, time_left_ms)
'''.replace("BLEND", repr(blend))
    destination.mkdir(parents=True)
    (destination / "agent.py").write_text(entrypoint, encoding="utf-8")
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
    if model is not None:
        (destination / "weights").mkdir()
        for source, name in (
            (model, "model.npz"),
            (model.with_name("training.json"), "training.json"),
        ):
            target = destination / "weights" / name
            shutil.copyfile(source, target)
            files[f"weights/{name}"] = file_hash(target)
    report: dict[str, Any] = {
        "path": str(destination.resolve()),
        "files": files,
        "entrypoint": "A1 experiment",
    }
    if model is not None:
        report.update({"entrypoint": "A1 neural pilot", "neural_blend": blend})
    write_json(destination / "build.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--model", type=Path, help="An original model with adjacent training.json")
    parser.add_argument("--blend", type=float, default=1.0)
    args = parser.parse_args()
    report = build(args.out, args.model, args.blend)
    print(f"Built {len(report['files'])} source files in {report['path']}")


if __name__ == "__main__":
    main()
