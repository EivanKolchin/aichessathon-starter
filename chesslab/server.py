"""Loopback-only web interface; no web dependencies or external assets."""

import csv
import io
import json
import mimetypes
import secrets
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import chess
import chess.svg

from chesslab.lab import Lab
from harness.rules import MAX_UNZIPPED_BYTES

STATIC = Path(__file__).with_name("static")


def export_run(lab: Lab, run_id: str) -> bytes:
    state = lab.state(run_id)
    run = state["run"]
    if run["status"] in {"preparing", "running", "stopping"}:
        raise ValueError("Wait for the batch to finish or stop before exporting")
    output = io.BytesIO()
    table = io.StringIO(newline="")
    writer = csv.writer(table)
    writer.writerow(
        [
            "game",
            "pair",
            "white",
            "black",
            "opening",
            "family",
            "status",
            "result",
            "termination",
            "seed",
        ]
    )
    pgns = []
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(run, indent=2))
        for game in run["games"]:
            writer.writerow(
                [
                    game["id"],
                    game["pair_id"],
                    game["white"],
                    game["black"],
                    game["opening"]["name"],
                    game["opening"]["family"],
                    game["status"],
                    game["result"],
                    game["termination"],
                    game["seed"],
                ]
            )
            if game["status"] == "completed":
                detail = lab.game(run_id, game["id"])
                archive.writestr(f"games/{game['id']}.json", json.dumps(detail, indent=2))
                pgns.append(detail["pgn"])
        archive.writestr("games.pgn", "\n\n".join(pgns))
        archive.writestr("results.csv", table.getvalue())
    return output.getvalue()


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, lab: Lab) -> None:
        self.lab = lab
        self.token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), Handler)


class Handler(BaseHTTPRequestHandler):
    server: Server

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send(self, body: bytes, mime: str, status: int = 200, filename: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self'; "
            "script-src 'self'; style-src 'self'; frame-ancestors 'none'",
        )
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def _read(self, length: int) -> bytes:
        """Read the whole body; a large upload does not arrive in one go."""
        chunks: list[bytes] = []
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                raise ValueError("The upload ended early")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _json(self, data: Any, status: int = 200) -> None:
        self._send(json.dumps(data, allow_nan=False).encode(), "application/json", status)

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in {
            f"127.0.0.1:{self.server.server_port}",
            f"localhost:{self.server.server_port}",
        }

    def do_GET(self) -> None:
        if not self._host_ok():
            self._json({"error": "Use the local Chess Lab address"}, 403)
            return
        try:
            self._get()
        except (ValueError, KeyError, FileNotFoundError) as error:
            self._json({"error": str(error)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _get(self) -> None:
        route = urlsplit(self.path)
        query = parse_qs(route.query)
        if route.path == "/api/catalog":
            self._json({**self.server.lab.catalog(), "token": self.server.token})
        elif route.path == "/api/state":
            self._json(self.server.lab.state(query.get("run", [None])[0]))
        elif route.path == "/api/play":
            self._json({"play": self.server.lab.play_state()})
        elif route.path == "/api/game":
            self._json(self.server.lab.game(query["run"][0], query["game"][0]))
        elif route.path == "/api/export":
            run_id = query["run"][0]
            self._send(
                export_run(self.server.lab, run_id), "application/zip", filename=f"{run_id}.zip"
            )
        elif route.path.startswith("/pieces/"):
            symbol = route.path.removeprefix("/pieces/").removesuffix(".svg")
            if symbol not in "PNBRQKpnbrqk" or len(symbol) != 1:
                raise ValueError("Unknown piece")
            self._send(chess.svg.piece(chess.Piece.from_symbol(symbol)).encode(), "image/svg+xml")
        else:
            name = "index.html" if route.path == "/" else route.path.lstrip("/")
            if name not in {"index.html", "app.js", "style.css"}:
                self._json({"error": "Not found"}, 404)
                return
            self._send((STATIC / name).read_bytes(), mimetypes.guess_type(name)[0] or "text/plain")

    def do_POST(self) -> None:
        origin = self.headers.get("Origin")
        if (
            not self._host_ok()
            or self.headers.get("X-CSRF-Token") != self.server.token
            or (origin is not None and origin != f"http://{self.headers.get('Host')}")
        ):
            self._json({"error": "Reload Chess Lab before making changes"}, 403)
            return
        route = urlsplit(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            # A dropped agent arrives as the zip itself, so it gets the event's unzipped cap.
            if route.path == "/api/agents":
                if not 0 < length <= MAX_UNZIPPED_BYTES:
                    raise ValueError(f"An agent upload must be 1 to {MAX_UNZIPPED_BYTES:,} bytes")
                fields = {key: value[0] for key, value in parse_qs(route.query).items()}
                self._json(self.server.lab.adopt(self._read(length), fields), 201)
                return
            if not 0 < length <= 65536:
                raise ValueError("Request body must be between 1 and 65,536 bytes")
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict):
                raise ValueError("Request must be a JSON object")
            if self.path == "/api/run":
                self._json({"id": self.server.lab.start(request)}, 201)
            elif self.path == "/api/resume":
                self._json({"id": self.server.lab.resume(str(request.get("run", "")))}, 201)
            elif self.path == "/api/stop":
                self.server.lab.stop()
                self._json({"ok": True})
            elif self.path == "/api/engines":
                self._json({"catalog": self.server.lab.register(request)}, 201)
            elif self.path == "/api/engines/remove":
                self._json({"catalog": self.server.lab.forget(str(request.get("id", "")))})
            elif self.path == "/api/play":
                self._json({"play": self.server.lab.play_start(request)}, 201)
            elif self.path == "/api/play/move":
                self._json({"play": self.server.lab.play_move(str(request.get("uci", "")))})
            elif self.path == "/api/play/undo":
                self._json({"play": self.server.lab.play_undo()})
            elif self.path == "/api/play/resign":
                self._json({"play": self.server.lab.play_resign()})
            elif self.path == "/api/play/end":
                self._json(self.server.lab.play_end())
            else:
                self._json({"error": "Not found"}, 404)
        except (ValueError, KeyError, TypeError) as error:
            self._json({"error": str(error)}, 400)
