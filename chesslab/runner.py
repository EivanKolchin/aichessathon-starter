"""Plays experiments the site queued, on this machine, and reports what happened.

The runner only ever dials out. There is no inbound port, no certificate on localhost and no
CORS: it asks the Worker for work, plays the games through the unchanged referee, and posts the
positions back as they are produced. The browser talks to the Worker and never to this process.

Games run wherever the runner runs, so what a run measures is this machine. The manifest already
records the platform, the core count and how many games were in flight; the runner adds its own
name to that so a result can always be traced to the thing that produced it.
"""

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from chesslab import ladder
from chesslab.lab import Lab, machine_description
from chesslab.ladder import Endpoint

POLL_S = 2.0
PLAY_POLL_S = 0.35
STREAM_S = 0.4
STATUS_EVERY_S = 2.0
# The browser counts a running clock down on its own between polls; this is how often it is
# told what the board's clock actually says, so the two cannot drift far apart.
CLOCK_REFRESH_S = 1.5
# How often an idle machine goes and gets what has been uploaded since. Without this the
# catalogue was only pulled when a job was claimed, so a freshly uploaded agent could not
# be chosen as an opponent until somebody had already run an experiment without it.
PULL_EVERY_S = 30.0
FRAME_BATCH = 200


class Runner:
    """One machine, offering itself to the catalogue for as long as it is left running."""

    def __init__(self, source: Endpoint, lab: Lab, name: str, uploads: Path) -> None:
        self.source = source
        self.lab = lab
        self.name = name
        self.uploads = uploads
        described = machine_description()
        self.machine = f"{described['platform']} · {os.cpu_count()} logical cpus"
        self.session: str | None = None
        self.reported: tuple[Any, ...] | None = None
        self.reported_at = 0.0
        # A finished game still holds the board — take backs and rematches need it — but it
        # no longer holds the machine, so a queued experiment can have it.
        self.occupied = False
        self.pulled = 0.0

    # ── the loop ───────────────────────────────────────────────────────────────────────────

    def serve(self, once: bool = False) -> None:
        while True:
            # A sparring game and a batch cannot share this machine: the batch is measuring how
            # long an engine takes, and a second game running beside it takes that time away.
            # Whoever is already sitting at the board keeps it, and experiments wait.
            self.attend()
            if not self.occupied:
                self.refresh()
                job = self.look()
                if job is not None:
                    if self.session is not None:
                        self.send(f"/api/play/{self.session}/state", {"status": "ended"})
                        self.leave()
                    self.play(job)
            if once:
                return
            time.sleep(PLAY_POLL_S if self.session else POLL_S)

    def refresh(self) -> None:
        """Keep this machine's list of opponents level with what the catalogue holds."""
        if time.monotonic() - self.pulled < PULL_EVERY_S:
            return
        self.pulled = time.monotonic()
        try:
            ladder.pull(
                self.source, self.uploads, self.lab.registry_path, self.uploads / "ladder.json"
            )
        except (ValueError, OSError) as error:
            print(f"could not pull the catalogue: {error}", flush=True)

    def look(self) -> dict[str, Any] | None:
        """One attempt to take work. A ladder that blinks is a wait, not the end of the run."""
        try:
            return self.claim()
        except ValueError as error:
            print(f"could not ask for work: {error}", flush=True)
            return None

    def claim(self) -> dict[str, Any] | None:
        """Also the heartbeat, and how the site learns what this machine can play."""
        body = json.dumps(
            {"runner": self.name, "machine": self.machine, "catalog": self.lab.catalog()}
        ).encode()
        answer = self.source.json("/api/jobs/claim", "POST", body, "application/json")
        job: dict[str, Any] | None = answer.get("job")
        return job

    # ── one person, one board ──────────────────────────────────────────────────────────────

    def attend(self) -> None:
        """Play the sparring game the site is holding, if there is one, and report the board."""
        try:
            answer = self.source.json(
                "/api/play/claim",
                "POST",
                json.dumps({"runner": self.name}).encode(),
                "application/json",
            )
        except ValueError as error:
            print(f"could not ask for a game: {error}", flush=True)
            return
        session: dict[str, Any] | None = answer.get("play")
        if session is None:
            self.leave()
            return
        play_id = str(session["id"])
        if play_id != self.session:
            self.leave()
            try:
                self.lab.play_start(dict(session["request"]))
            except (ValueError, OSError) as error:
                print(f"could not start {play_id}: {error}", flush=True)
                self.send(f"/api/play/{play_id}/state", {"status": "ended"})
                return
            self.session = play_id
            self.occupied = True
        command = session.get("command") or {}
        try:
            kind = str(command.get("kind", ""))
            if kind == "move":
                self.lab.play_move(str(command.get("uci", "")))
            elif kind == "undo":
                self.lab.play_undo()
            elif kind == "resign":
                self.lab.play_resign()
            elif kind == "end":
                self.leave()
                self.send(f"/api/play/{play_id}/state", {"status": "ended"})
                return
        except ValueError as error:
            # A refused move is the board's answer to it, not a reason to abandon the game.
            print(f"{play_id}: {error}", flush=True)
        self.report_board(play_id)

    def report_board(self, play_id: str) -> None:
        """Post the position, but only when it has moved on or the clocks have gone stale."""
        state = self.lab.play_state()
        if state is None:
            self.session = None
            self.send(f"/api/play/{play_id}/state", {"status": "ended"})
            return
        moment = (
            play_id,
            state["status"],
            len(state["frames"]),
            state["thinking"],
            state["your_turn"],
            state["result"],
        )
        if moment == self.reported and time.monotonic() - self.reported_at < CLOCK_REFRESH_S:
            return
        self.reported, self.reported_at = moment, time.monotonic()
        # "over" is the game being finished, not the board being gone: the session stays here so
        # the person can take the last move back or look at the position.
        finished = state["status"] == "finished"
        self.occupied = not finished
        self.send(
            f"/api/play/{play_id}/state",
            {"state": state, "status": "over" if finished else "live"},
        )

    def leave(self) -> None:
        if self.session is not None:
            print(f"leaving {self.session}", flush=True)
        self.session = None
        self.reported = None
        self.occupied = False
        self.lab.play_end()

    def play(self, job: dict[str, Any]) -> None:
        run_id = str(job["run_id"])
        print(f"claimed {run_id}", flush=True)
        try:
            # An uploaded agent has to exist locally before it can be an opponent.
            ladder.pull(
                self.source, self.uploads, self.lab.registry_path, self.uploads / "ladder.json"
            )
            local_id = self.lab.start(dict(job["request"]))
        except Exception as error:
            self.report(run_id, "failed", f"{type(error).__name__}: {error}")
            return
        manifest = self.lab.state(local_id)["run"]
        self.send(f"/api/runs/{run_id}/manifest", {"manifest": self.stamp(manifest)})
        self.stream(run_id, local_id)

    # ── streaming ──────────────────────────────────────────────────────────────────────────

    def stream(self, run_id: str, local_id: str) -> None:
        sent: dict[str, int] = {}
        reported: set[str] = set()
        asked = 0.0
        while True:
            state = self.lab.state(local_id)
            run = state["run"]
            for game in run["games"]:
                if game["status"] == "queued":
                    continue
                self.push(run_id, local_id, game, sent, reported)
            if time.monotonic() - asked > STATUS_EVERY_S:
                asked = time.monotonic()
                if self.wanted_stopped(run_id):
                    self.lab.stop()
            if not state["busy"]:
                break
            time.sleep(STREAM_S)
        final = self.lab.state(local_id)["run"]
        for game in final["games"]:
            self.push(run_id, local_id, game, sent, reported)
        self.send(f"/api/runs/{run_id}/manifest", {"manifest": self.stamp(final)})
        self.report(run_id, final["status"], final.get("error"))
        print(f"finished {run_id}: {final['status']}", flush=True)

    def push(
        self,
        run_id: str,
        local_id: str,
        game: dict[str, Any],
        sent: dict[str, int],
        reported: set[str],
    ) -> None:
        game_id = str(game["id"])
        if game_id in reported:
            return
        detail = self.lab.game(local_id, game_id)
        frames = detail["frames"]
        already = sent.get(game_id, 0)
        if len(frames) > already:
            batch = [
                {"ply": ply, "frame": frame}
                for ply, frame in enumerate(frames)
                if ply >= already
            ][:FRAME_BATCH]
            self.send(f"/api/runs/{run_id}/games/{game_id}/frames", {"frames": batch})
            sent[game_id] = already + len(batch)
        if game["status"] == "completed":
            self.send(
                f"/api/runs/{run_id}/games/{game_id}",
                {
                    "game": {
                        "status": game["status"],
                        "result": game["result"],
                        "termination": game["termination"],
                        "plies": game["plies"],
                        "pgn": detail.get("pgn"),
                        "detail": {
                            "engine_info": detail.get("engine_info", {}),
                            "logs": detail.get("logs", {}),
                            "seed": game.get("seed"),
                            "pair_id": game.get("pair_id"),
                        },
                    }
                },
            )
            if sent.get(game_id, 0) >= len(frames):
                reported.add(game_id)
        elif game["status"] != "queued":
            self.send(
                f"/api/runs/{run_id}/games/{game_id}",
                {"game": {"status": game["status"], "plies": len(frames) - 1}},
            )

    # ── talking to the site ────────────────────────────────────────────────────────────────

    def stamp(self, manifest: dict[str, Any]) -> dict[str, Any]:
        """The site has to be able to say which machine produced a result."""
        environment = {**manifest.get("environment", {}), "runner": self.name}
        return {**manifest, "environment": environment}

    def send(self, path: str, payload: dict[str, Any]) -> None:
        try:
            self.source.json(path, "POST", json.dumps(payload).encode(), "application/json")
        except ValueError as error:
            # A dropped position is not worth abandoning a game that is still being played.
            print(f"could not post to {path}: {error}", flush=True)

    def wanted_stopped(self, run_id: str) -> bool:
        try:
            answer = self.source.json(f"/api/runs/{run_id}")
        except ValueError:
            return False
        status = str(answer.get("run", {}).get("status", ""))
        return status in {"stopping", "stopped"}

    def report(self, run_id: str, status: str, error: str | None = None) -> None:
        payload = {"status": status, "runner": self.name}
        if error:
            payload["error"] = error
        self.send(f"/api/runs/{run_id}/status", payload)


def run_runner(args: argparse.Namespace) -> None:
    state = args.registry.with_name("ladder.json")
    source = ladder.endpoint(args.url, args.token, state)
    lab = Lab(args.data_dir, args.registry)
    name = args.name or machine_description()["host"] or "runner"
    runner = Runner(source, lab, name, args.uploads)
    print(f"runner {name} offering itself to {source.url}", flush=True)
    try:
        runner.serve(once=args.once)
    except KeyboardInterrupt:
        print("\nstopping runner", flush=True)
    finally:
        lab.close()
