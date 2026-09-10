"""Client for the shared upload catalogue. Uploads become ordinary registered opponents.

The site stores builds; it never plays them. Everything here runs on the machine that will
play the games, so an uploaded zip is validated a second time locally before it is unpacked,
on the assumption that the catalogue could serve something the Worker did not reject.
"""

import io
import json
import os
import secrets
import shutil
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from chesslab.builds import build_spec, extract, verify
from chesslab.registry import EngineSpec, write_json

TIMEOUT_S = 60.0
USER_AGENT = "chesslab-ladder/0.1 (+https://aichessathon.com)"


@dataclass(frozen=True)
class Endpoint:
    url: str
    token: str

    def open(
        self, path: str, method: str = "GET", body: bytes | None = None, mime: str = ""
    ) -> Any:
        request = urllib.request.Request(f"{self.url.rstrip('/')}{path}", data=body, method=method)
        # Cloudflare's edge answers 403 (error 1010) to the default Python-urllib signature, so
        # the client says what it actually is. This never comes up against `wrangler dev`.
        request.add_header("User-Agent", USER_AGENT)
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        if mime:
            request.add_header("Content-Type", mime)
        try:
            return urllib.request.urlopen(request, timeout=TIMEOUT_S)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            try:
                payload = json.loads(detail)
                message = payload.get("error", detail)
                for problem in payload.get("problems", []):
                    message += f"\n  - {problem}"
            except json.JSONDecodeError:
                message = detail or error.reason
            raise ValueError(f"{self.url} said {error.code}: {message}") from error
        except urllib.error.URLError as error:
            raise ValueError(f"Could not reach {self.url}: {error.reason}") from error
        except OSError as error:
            # A keep-alive connection dropped by the edge arrives here rather than as a
            # URLError, and a runner left up for hours will meet one. Same kind of failure as
            # the two above: the request did not happen, so say so and let the caller retry.
            raise ValueError(f"Could not reach {self.url}: {error}") from error

    def json(
        self, path: str, method: str = "GET", body: bytes | None = None, mime: str = ""
    ) -> Any:
        with self.open(path, method, body, mime) as response:
            return json.loads(response.read())


def endpoint(url: str | None, token: str | None, state: Path) -> Endpoint:
    """Flags win, then the environment, then whatever the last successful pull saved."""
    saved: dict[str, Any] = {}
    if state.exists():
        saved = json.loads(state.read_text(encoding="utf-8"))
    resolved = url or os.environ.get("CHESSLAB_LADDER_URL") or saved.get("url", "")
    secret = token or os.environ.get("CHESSLAB_LADDER_TOKEN") or saved.get("token", "")
    if not resolved:
        raise ValueError(
            "No ladder address. Pass --url https://your-worker.example.com once, "
            "or set CHESSLAB_LADDER_URL"
        )
    return Endpoint(resolved, secret)


def spec_for(agent: dict[str, Any], directory: Path) -> EngineSpec:
    owner = str(agent["owner"])
    notes = f" - {agent['notes']}" if agent["notes"] else ""
    return build_spec(
        directory,
        str(agent["id"]),
        str(agent["name"]),
        str(agent["family"]),
        f"Uploaded by {owner}{notes}",
    )


def merge_registry(registry: Path, managed: dict[str, EngineSpec], previous: set[str]) -> None:
    """Rewrite only the entries this client owns, leaving hand-registered engines alone."""
    existing: list[dict[str, Any]] = []
    if registry.exists():
        existing = json.loads(registry.read_text(encoding="utf-8"))["engines"]
    kept = [item for item in existing if item["id"] not in previous and item["id"] not in managed]
    write_json(registry, {"engines": kept + [spec.data() for spec in managed.values()]})


def pull(source: Endpoint, uploads: Path, registry: Path, state: Path) -> dict[str, list[str]]:
    """Bring the local registry in line with the catalogue. Returns what changed."""
    catalogue = source.json("/api/catalog")
    listed = catalogue.get("agents") if isinstance(catalogue, dict) else None
    if not isinstance(listed, list):
        # A runner pulls this on a loop, so a catalogue that answers with something unexpected
        # has to be a message the caller can retry past, not a KeyError out of the loop.
        raise ValueError(f"{source.url} did not answer with a catalogue")
    agents = {str(agent["id"]): agent for agent in listed}
    saved: dict[str, Any] = {}
    if state.exists():
        saved = json.loads(state.read_text(encoding="utf-8"))
    known: dict[str, Any] = saved.get("agents", {})

    added: list[str] = []
    updated: list[str] = []
    removed: list[str] = []
    failed: list[str] = []
    specs: dict[str, EngineSpec] = {}
    for agent_id, agent in agents.items():
        directory = uploads / agent_id
        fresh = known.get(agent_id, {}).get("sha256") != agent["sha256"] or not directory.exists()
        try:
            if fresh:
                with source.open(f"/api/agents/{agent_id}/zip") as response:
                    payload = response.read()
                digest = sha256(payload).hexdigest()
                if digest != agent["sha256"]:
                    raise ValueError(f"Download does not match its recorded hash ({digest[:12]})")
                extract(payload, directory)
                (added if agent_id not in known else updated).append(agent_id)
            specs[agent_id] = spec_for(agent, directory)
        except (ValueError, OSError, zipfile.BadZipFile) as error:
            failed.append(f"{agent_id}: {error}")

    for agent_id in known:
        if agent_id not in specs:
            shutil.rmtree(uploads / agent_id, ignore_errors=True)
            removed.append(agent_id)

    merge_registry(registry, specs, set(known))
    write_json(
        state,
        {
            "url": source.url,
            "token": source.token,
            "agents": {key: {"sha256": agents[key]["sha256"]} for key in specs},
        },
    )
    return {"added": added, "updated": updated, "removed": removed, "failed": failed}


def multipart(fields: dict[str, str], payload: bytes) -> tuple[bytes, str]:
    boundary = f"----chesslab{secrets.token_hex(16)}"
    body = bytearray()
    for key, value in fields.items():
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        body += value.encode() + b"\r\n"
    body += f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '.encode()
    body += b'filename="submission.zip"\r\nContent-Type: application/zip\r\n\r\n'
    body += payload + f"\r\n--{boundary}--\r\n".encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def push(target: Endpoint, archive: Path, name: str, family: str, notes: str) -> dict[str, Any]:
    payload = archive.read_bytes()
    with zipfile.ZipFile(io.BytesIO(payload)) as opened:
        problems = verify(opened)
    if problems:
        raise ValueError("This zip would be refused:\n  - " + "\n  - ".join(problems))
    fields = {"name": name or archive.stem, "family": family, "notes": notes}
    body, mime = multipart(fields, payload)
    result: dict[str, Any] = target.json("/api/upload", "POST", body, mime)
    return result
