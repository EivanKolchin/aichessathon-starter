"""Validating and unpacking an agent build, whether it came from the catalogue or a drop.

python-chess and the platform stack aside, what ships has to be readable source at the root of
the archive. These are the checks the ladder client already made on a downloaded build; the
browser drop goes through exactly the same ones rather than a friendlier second set.
"""

import ast
import io
import shutil
import zipfile
from pathlib import Path

from chesslab.registry import ROOT, EngineSpec
from harness.rules import MAX_UNZIPPED_BYTES

# Mirrors ladder/src/unzip.js. Compiled Python is refused as well: the event wants readable
# source, and a .pyc in the zip is a build artefact nobody meant to ship.
NATIVE_SUFFIXES = (".so", ".pyd", ".dll", ".dylib", ".exe", ".o", ".a", ".lib", ".pyc")
NATIVE_MAGIC = (
    b"\x7fELF",
    b"MZ",
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
)
PLATFORM_PACKAGES = frozenset({"numpy", "numba", "torch", "onnxruntime", "chess"})
SYMLINK_MODE = 0o120000
CHUNK = 1 << 16

def unsafe(name: str) -> str | None:
    """Reject anything that could write outside the agent directory when it is unpacked."""
    if not name or len(name) > 240:
        return "has a missing or overlong path"
    if name.startswith("/") or "\\" in name:
        return "is an absolute or Windows path"
    if len(name) > 1 and name[1] == ":":
        return "carries a drive letter"
    if any(part in {"..", "."} for part in name.split("/")):
        return "escapes the agent directory"
    if any(character < " " for character in name):
        return "contains a control character"
    return None


def verify(archive: zipfile.ZipFile) -> list[str]:
    problems: list[str] = []
    total = 0
    names: list[str] = []
    for info in archive.infolist():
        reason = unsafe(info.filename)
        if reason:
            problems.append(f'"{info.filename}" {reason}')
            continue
        if info.is_dir():
            continue
        if (info.external_attr >> 16) & 0o170000 == SYMLINK_MODE:
            problems.append(f'"{info.filename}" is a symlink; ship the file itself')
            continue
        if info.filename.lower().endswith(NATIVE_SUFFIXES):
            problems.append(f'"{info.filename}" is a binary; the event takes Python source')
            continue
        total += info.file_size
        names.append(info.filename)
    if "agent.py" not in names:
        problems.append("No agent.py at the root of the zip; the platform does `import agent`")
    if total > MAX_UNZIPPED_BYTES:
        limit = MAX_UNZIPPED_BYTES // 1_000_000
        problems.append(f"{total:,} bytes unzipped is over the {limit} MB limit")
    if problems:
        return problems
    for name in names:
        with archive.open(name) as stream:
            head = stream.read(8)
        if head.startswith(NATIVE_MAGIC):
            problems.append(f'"{name}" is a native binary despite its name; ship Python source')
    return problems


def extract(payload: bytes, destination: Path) -> None:
    """Unpack a verified zip, enforcing the size cap against real bytes, not the header."""
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        problems = verify(archive)
        if problems:
            joined = "\n  - ".join(problems)
            raise ValueError(f"This build does not pass local validation:\n  - {joined}")
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        written = 0
        for info in archive.infolist():
            if info.is_dir():
                continue
            target = destination / info.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as sink:
                while chunk := source.read(CHUNK):
                    written += len(chunk)
                    if written > MAX_UNZIPPED_BYTES:
                        raise ValueError("The archive expands past the size limit; refusing it")
                    sink.write(chunk)


def imports(directory: Path) -> list[str]:
    """Third-party imports the lab should check for before offering this agent."""
    found: set[str] = set()
    for path in sorted(directory.rglob("*.py")):
        try:
            tree = ast.parse(path.read_bytes())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    local = {path.stem for path in directory.glob("*.py")} | {
        path.name for path in directory.iterdir() if path.is_dir()
    }
    return sorted((found & PLATFORM_PACKAGES) - local)

def spec_path(directory: Path) -> str:
    """Inside the repository, engines are recorded relatively so the manifest stays portable."""
    resolved = directory.resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def build_spec(
    directory: Path, engine_id: str, name: str, family: str, description: str
) -> EngineSpec:
    """Everything not a root module is declared, so the frozen build keeps its data files."""
    includes = sorted(
        entry.name
        for entry in directory.iterdir()
        if not (entry.is_file() and entry.suffix == ".py")
    )
    return EngineSpec(
        id=engine_id,
        name=name,
        family=family,
        path=spec_path(directory),
        description=description,
        requires=imports(directory),
        includes=includes,
    )
