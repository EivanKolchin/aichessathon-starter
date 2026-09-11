"""python -m chesslab: board viewer, headless experiments, and opponent registration."""

import argparse
import json
import re
from pathlib import Path

from chesslab import ladder
from chesslab.builds import install
from chesslab.compare import compare
from chesslab.lab import Lab
from chesslab.registry import ROOT, identifier, load_registry, parse, store
from chesslab.runner import run_runner
from chesslab.server import Server


def run_ladder(args: argparse.Namespace) -> None:
    """Catalogue commands. A bad address or a refused build is a message, not a traceback."""
    state = args.registry.with_name("ladder.json")
    source = ladder.endpoint(args.url, args.token, state)
    if args.ladder_action == "list":
        for agent in source.json("/api/catalog")["agents"]:
            print(f"{agent['id']:52} {agent['owner']:24} {agent['unzipped_bytes']:>10,} B")
        return
    if args.ladder_action == "push":
        result = ladder.push(source, args.zip, args.name, args.family, args.notes)
        settled = "already in the catalogue" if result.get("unchanged") else "uploaded"
        print(f"{result['agent']['id']} {settled}")
        return
    changed = ladder.pull(source, args.uploads, args.registry, state)
    for label in ("added", "updated", "removed"):
        for agent_id in changed[label]:
            print(f"{label:8} {agent_id}")
    for problem in changed["failed"]:
        print(f"failed   {problem}")
    total = sum(len(changed[label]) for label in ("added", "updated", "removed"))
    plural = "" if total == 1 else "s"
    print(f"{total} change{plural}; run `python -m chesslab serve` to play them")
    if changed["failed"]:
        raise SystemExit(1)


def title_from(stem: str) -> str:
    """Mirror the drop page's name suggestion, so a zip registers under the name it looks like."""
    words = re.sub(r"[-_]+", " ", stem).strip()
    return words[:1].upper() + words[1:]


def run_register(args: argparse.Namespace) -> None:
    """A JSON spec names an engine already on disk; a zip is checked and unpacked like a drop."""
    if args.spec.suffix.lower() != ".zip":
        spec = parse(json.loads(args.spec.read_text(encoding="utf-8")))
        store(args.registry, spec)
        print(f"Registered {spec.name} ({spec.family}): {spec.availability()[1]}")
        return
    name = args.name.strip() or title_from(args.spec.stem)
    engine_id = identifier(name)
    # The same validation, unpacking and one-move probe the browser drop runs, so a zip that
    # would not have played is refused here rather than registered and discovered mid-experiment.
    spec, report = install(
        args.spec.read_bytes(),
        args.registry.parent / "agents" / engine_id,
        engine_id,
        name,
        args.family.strip() or name,
        args.notes.strip() or f"Registered from {args.spec.name}",
        Lab.probe,
    )
    store(args.registry, spec)
    print(f"Registered {spec.name} ({spec.family}) as {spec.id}: {report}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / ".chesslab" / "runs")
    parser.add_argument("--registry", type=Path, default=ROOT / ".chesslab" / "engines.json")
    sub = parser.add_subparsers(dest="action", required=True)
    serve = sub.add_parser("serve", help="Open the local board and experiment dashboard")
    serve.add_argument("--port", type=int, default=8765)
    run = sub.add_parser("run", help="Execute a JSON experiment request without a browser")
    run.add_argument("config", type=Path)
    register = sub.add_parser("register", help="Register a JSON engine spec or a submission zip")
    register.add_argument("spec", type=Path, help="A JSON spec, or a .zip holding agent.py")
    register.add_argument("--name", default="", help="Name for a zip; defaults to its filename")
    register.add_argument("--family", default="", help="Family for a zip; defaults to its name")
    register.add_argument("--notes", default="", help="Description for a zip")
    sub.add_parser("catalog", help="List opponent availability")
    comparison = sub.add_parser("compare", help="Compare matched pairs in two finished manifests")
    comparison.add_argument("baseline", type=Path)
    comparison.add_argument("candidate", type=Path)
    share = sub.add_parser("ladder", help="Share agents through the upload catalogue")
    offer = sub.add_parser("runner", help="Play experiments the site queued, on this machine")
    # Not platform.node() as a default: argparse evaluates defaults while the parser is being
    # built, and on Windows that name comes from WMI, which can block. It is resolved later.
    offer.add_argument("--name", default="", help="How the site names this machine")
    offer.add_argument("--once", action="store_true", help="Take at most one job, then exit")
    # Carried by every subcommand, so the address goes where people expect: `ladder pull --url`.
    options = argparse.ArgumentParser(add_help=False)
    options.add_argument("--url", help="Address of the ladder Worker; remembered after first use")
    options.add_argument("--token", help="Ladder token, or set CHESSLAB_LADDER_TOKEN")
    options.add_argument("--uploads", type=Path, default=ROOT / ".chesslab" / "uploads")
    for parser_needing_address in (offer,):
        parser_needing_address.add_argument("--url", help="Address of the ladder Worker")
        parser_needing_address.add_argument(
            "--token", help="Ladder token, or CHESSLAB_LADDER_TOKEN"
        )
        parser_needing_address.add_argument(
            "--uploads", type=Path, default=ROOT / ".chesslab" / "uploads"
        )
    shared = share.add_subparsers(dest="ladder_action", required=True)
    shared.add_parser("list", parents=[options], help="Show what the catalogue holds")
    shared.add_parser("pull", parents=[options], help="Register catalogue agents as opponents")
    upload = shared.add_parser("push", parents=[options], help="Upload a zip to the catalogue")
    upload.add_argument("zip", nargs="?", type=Path, default=ROOT / "submission.zip")
    upload.add_argument("--name", default="")
    upload.add_argument("--family", default="")
    upload.add_argument("--notes", default="")
    args = parser.parse_args()
    if args.action == "compare":
        result = compare(
            json.loads(args.baseline.read_text(encoding="utf-8")),
            json.loads(args.candidate.read_text(encoding="utf-8")),
        )
        print(json.dumps(result, indent=2))
        return
    if args.action == "runner":
        args.data_dir = args.data_dir.with_name("runner")
        try:
            run_runner(args)
        except (ValueError, OSError) as error:
            raise SystemExit(str(error)) from error
        return
    if args.action == "ladder":
        try:
            run_ladder(args)
        except (ValueError, OSError) as error:
            raise SystemExit(str(error)) from error
        return
    if args.action == "register":
        try:
            run_register(args)
        except (ValueError, OSError) as error:
            raise SystemExit(str(error)) from error
        return
    if args.action == "catalog":
        for spec in load_registry(args.registry).values():
            print(f"{spec.id:16} {spec.family:26} {spec.availability()[1]}")
        return
    lab = Lab(args.data_dir, args.registry)
    try:
        if args.action == "run":
            run_id = lab.start(json.loads(args.config.read_text(encoding="utf-8")))
            print(f"Running {run_id}", flush=True)
            assert lab.worker is not None
            lab.worker.join()
            result = lab.state(run_id)["run"]
            print(
                json.dumps(
                    {
                        "status": result["status"],
                        "summary": result["summary"],
                        "error": result.get("error"),
                    },
                    indent=2,
                )
            )
            if result["status"] != "completed":
                raise SystemExit(1)
        else:
            with Server(args.port, lab) as server:
                print(f"Chess Lab: http://127.0.0.1:{server.server_port}", flush=True)
                server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\nStopping Chess Lab")
    finally:
        lab.close()


if __name__ == "__main__":
    main()
