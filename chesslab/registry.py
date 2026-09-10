"""Explicit engine identities, availability checks, and immutable Python build copies."""

import dataclasses
import hashlib
import importlib.util
import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from harness.package import members

ROOT = Path(__file__).resolve().parent.parent
IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")


@dataclass(frozen=True)
class EngineSpec:
    id: str
    name: str
    family: str
    kind: str = "python"
    path: str = "."
    description: str = ""
    command: list[str] = field(default_factory=list)
    options: dict[str, str | int | bool | None] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    requires: list[str] = field(default_factory=list)
    includes: list[str] = field(default_factory=lambda: ["weights"])
    assets: list[str] = field(default_factory=list)
    max_move_ms: int | None = None

    def __post_init__(self) -> None:
        if not IDENTIFIER.fullmatch(self.id):
            raise ValueError("Engine id must contain lowercase letters, digits, - or _")
        if self.kind not in {"python", "uci"} or not self.name or not self.family:
            raise ValueError("An engine needs a name, family, and kind python or uci")
        if self.kind == "uci" and (not self.command or not all(self.command)):
            raise ValueError("UCI engines need a command array; shell commands are not accepted")
        if self.max_move_ms is not None and self.max_move_ms < 1:
            raise ValueError("max_move_ms must be positive, or null for the full clock")
        for name in self.includes:
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise ValueError("Included build files must be relative to the engine directory")

    def data(self) -> dict[str, Any]:
        return asdict(self)

    def directory(self) -> Path:
        return (ROOT / self.path).resolve()

    def present(self) -> tuple[bool, str]:
        """Whether the engine's own files are where the spec says, packages aside."""
        if self.kind == "python" and not (self.directory() / "agent.py").is_file():
            return False, "agent.py not found"
        if self.kind == "uci" and shutil.which(self.command[0]) is None:
            return False, "Executable not found; register an installed UCI engine"
        missing_assets = [name for name in self.assets if not (ROOT / name).is_file()]
        if missing_assets:
            return False, "Missing engine assets: " + ", ".join(missing_assets)
        return True, "Ready"

    def availability(self) -> tuple[bool, str]:
        present, reason = self.present()
        if not present:
            return False, reason
        if self.kind == "python":
            missing = [name for name in self.requires if importlib.util.find_spec(name) is None]
            if missing:
                return False, "Missing packages: " + ", ".join(missing)
        return True, "Ready"


def find_stockfish() -> tuple[list[str], str, dict[str, str | int | bool | None]]:
    """Locate a Stockfish executable on PATH or in the repository's engines directory."""
    if shutil.which("stockfish") is not None:
        return ["stockfish"], "Stockfish 19", {"Threads": 1, "Hash": 64}

    known_paths = [
        ROOT
        / ".chesslab"
        / "engines"
        / "stockfish19"
        / "extracted"
        / "stockfish"
        / "stockfish-windows-x86-64-universal.exe",
        ROOT
        / ".chesslab"
        / "engines"
        / "stockfish19"
        / "extracted"
        / "stockfish"
        / "stockfish-ubuntu-x86-64-universal",
    ]
    for candidate in known_paths:
        if candidate.is_file():
            return [str(candidate.resolve())], "Stockfish 19", {"Threads": 1, "Hash": 64}

    engines_dir = ROOT / ".chesslab" / "engines"
    if engines_dir.is_dir():
        for candidate in sorted(engines_dir.glob("**/stockfish*.exe")):
            if candidate.is_file():
                return [str(candidate.resolve())], "Stockfish 19", {"Threads": 1, "Hash": 64}
        for candidate in sorted(engines_dir.glob("**/stockfish*")):
            if candidate.is_file() and not candidate.name.endswith(
                (".zip", ".tar", ".gz", ".h", ".cpp", ".txt", ".md", ".json")
            ):
                return [str(candidate.resolve())], "Stockfish 19", {"Threads": 1, "Hash": 64}

    return ["stockfish"], "Stockfish", {}


def defaults() -> list[EngineSpec]:
    specs = [EngineSpec("candidate", "Working agent", "Candidate", description="Root agent.py")]
    for key, name, family, description in (
        ("random", "Random", "Random policy", "Uniform legal moves; protocol sanity check"),
        ("greedy", "Material greedy", "Material policy", "One-ply material evaluation"),
        ("minimax", "Minimax", "Shallow minimax", "Two-ply material and mobility search"),
        ("numba", "Minimax · Numba", "Shallow minimax", "Same search, compiled evaluation"),
    ):
        specs.append(
            EngineSpec(
                key,
                name,
                family,
                path=f"baselines/{key}",
                description=description,
                requires=["numpy", "numba"] if key == "numba" else [],
            )
        )
    for key, name, family, description in (
        (
            "positional",
            "Positional policy",
            "Static positional policy",
            "Development and centre bias",
        ),
        (
            "tactical",
            "Tactical search",
            "Lab alpha-beta",
            "Iterative deepening with capture search",
        ),
        ("solid", "Solid search", "Lab alpha-beta", "Pawn structure and cautious material values"),
        ("active", "Active search", "Lab alpha-beta", "Activity and king pressure bias"),
        (
            "rollout",
            "Monte Carlo",
            "Flat Monte Carlo",
            "Short sampled continuations; weak diagnostic",
        ),
    ):
        specs.append(
            EngineSpec(
                key,
                name,
                family,
                path="chesslab/bots",
                description=description,
                env={"CHESSLAB_STYLE": key},
            )
        )
    stockfish_cmd, stockfish_name, stockfish_options = find_stockfish()
    specs.append(
        EngineSpec(
            "stockfish",
            stockfish_name,
            "Stockfish",
            kind="uci",
            command=stockfish_cmd,
            options=stockfish_options,
            description="Official Stockfish 19 reference · one thread, full clock",
        )
    )
    return specs


def identifier(text: str) -> str:
    """A registry id from a human name: lowercase letters, digits, - and _ only."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:64]
    if not IDENTIFIER.fullmatch(slug):
        raise ValueError("That name has no letters or digits to build an id from")
    return slug


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def custom_engines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    engines: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))["engines"]
    return engines


def parse(data: dict[str, Any]) -> EngineSpec:
    """Build a spec from browser input. Unknown keys are a mistake worth naming."""
    unknown = set(data) - {field.name for field in dataclasses.fields(EngineSpec)}
    if unknown:
        raise ValueError("Unknown engine fields: " + ", ".join(sorted(unknown)))
    try:
        return EngineSpec(**data)
    except TypeError as error:
        raise ValueError(f"That engine spec is not usable: {error}") from None


def store(path: Path, spec: EngineSpec) -> None:
    """Add or replace one engine in the custom registry, by id."""
    engines = [item for item in custom_engines(path) if item["id"] != spec.id]
    write_json(path, {"engines": [*engines, spec.data()]})


def drop(path: Path, engine_id: str) -> None:
    engines = custom_engines(path)
    kept = [item for item in engines if item["id"] != engine_id]
    if len(kept) == len(engines):
        raise ValueError("Only engines you added here can be removed")
    write_json(path, {"engines": kept})


def load_registry(path: Path) -> dict[str, EngineSpec]:
    specs = {spec.id: spec for spec in defaults()}
    for item in custom_engines(path):
        spec = EngineSpec(**item)
        specs[spec.id] = spec
    return specs


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def freeze(spec: EngineSpec, destination: Path) -> dict[str, Any]:
    available, reason = spec.availability()
    if not available:
        raise ValueError(f"{spec.name}: {reason}")
    result = spec.data()
    files: dict[str, str] = {}
    if spec.kind == "python":
        root = spec.directory()
        for source, name in members(root, tuple(spec.includes)):
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            files[name] = file_hash(target)
        result["frozen_path"] = str(destination.resolve())
    else:
        executable = shutil.which(spec.command[0])
        if executable is None:
            raise ValueError("UCI executable disappeared")
        result["command"] = [str(Path(executable).resolve()), *spec.command[1:]]
        files[executable] = file_hash(Path(executable))
        for asset in spec.assets:
            files[str((ROOT / asset).resolve())] = file_hash(ROOT / asset)
    result["files"] = files
    digest = json.dumps({"spec": spec.data(), "files": files}, sort_keys=True).encode()
    result["sha256"] = hashlib.sha256(digest).hexdigest()
    return result
