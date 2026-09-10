"""Profile A0 and screen an external UCI engine for budget-sensitive decisions.

Research only: changes of move/evaluation are hypotheses, not proven engine mistakes.
Exported engine commands are never executed; the executable is a separate explicit argument.
"""

import argparse
import io
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import chess
import chess.engine
import chess.pgn

from chesslab.experiments.audit_run import verify_game
from chesslab.registry import EngineSpec, file_hash, freeze, write_json

PROFILE_WORKER = """
import cProfile, json, pstats, sys
from dataclasses import asdict
import chess
from a0 import __version__
from a0.search import Search, SearchConfig
r=json.load(sys.stdin)
b=chess.Board(r['case']['start_fen'])
for m in r['case']['moves']: b.push_uci(m)
s=Search(SearchConfig(max_nodes=r['nodes']))
p=cProfile.Profile()
p.enable()
result=s.analyse(b,3600000,3600000)
p.disable()
p.dump_stats(r['profile'])
stats=pstats.Stats(p)
rows=[]
buckets={}
for (filename,line,name),(primitive,calls,own,cumulative,callers) in stats.stats.items():
    path=filename.replace('\\\\','/')
    category='other'
    if path.endswith('a0/evaluation.py'): category='evaluation_and_incremental_updates'
    elif path.endswith('a0/search.py') and name=='_draw': category='draw_detection'
    elif path.endswith('a0/search.py') and name in ('_order','priority'): category='move_ordering'
    elif path.endswith('chess/__init__.py'):
        if name.startswith('generate_') or name in (
            '_generate_evasions','_is_safe','is_into_check','is_legal','_slider_blockers'):
            category='move_generation_and_legality'
        elif name in ('push','pop','restore','_remove_piece_at','_set_piece_at'):
            category='board_updates'
        elif 'insufficient_material' in name: category='draw_detection'
    buckets[category]=buckets.get(category,0)+own
    rows.append({'file':path,'line':line,'function':name,'calls':calls,'self_seconds':own,'cumulative_seconds':cumulative})
result_data=asdict(result)
result_data['move']=result.move.uci()
print(json.dumps({'version':__version__,'result':result_data,'self_time_buckets':buckets,
 'total_profile_seconds':stats.total_tt,
 'hotspots':sorted(rows,key=lambda x:x['self_seconds'],reverse=True)[:25],
 'instrumented':True}))
"""


def collect_cases(directory: Path, opponent: str, plies: list[int]) -> list[dict[str, Any]]:
    run = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    cases = []
    for entry in run["games"]:
        if entry["status"] != "completed" or entry["opponent"] != opponent:
            continue
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", entry["id"]):
            raise ValueError("Invalid game id")
        path = directory / "games" / f"{entry['id']}.json"
        detail = json.loads(path.read_text(encoding="utf-8"))
        verify_game(detail)
        for key in ("id", "white", "black", "opening", "opponent", "seed", "status"):
            if detail[key] != entry[key]:
                raise ValueError(f"Manifest/detail mismatch: {key}")
        game = chess.pgn.read_game(io.StringIO(detail["pgn"]))
        assert game is not None
        moves = list(game.mainline_moves())
        for ply in plies:
            if ply >= len(moves):
                continue
            board = game.board()
            for move in moves[:ply]:
                board.push(move)
            if board.outcome(claim_draw=True) is not None:
                continue
            cases.append(
                {
                    "id": f"{entry['id']}-p{ply:03d}",
                    "game": entry["id"],
                    "ply": ply,
                    "opening": entry["opening"],
                    "start_fen": game.board().fen(),
                    "moves": [move.uci() for move in moves[:ply]],
                    "fen": board.fen(),
                    "side_to_move_engine": entry["white" if board.turn else "black"],
                    "recorded_next_move": moves[ply].uci(),
                    "game_file_sha256": file_hash(path),
                }
            )
    return cases


def board_for(case: dict[str, Any]) -> chess.Board:
    board = chess.Board(case["start_fen"])
    for move in case["moves"]:
        board.push_uci(move)
    if board.fen() != case["fen"]:
        raise ValueError("Case history does not reproduce its FEN")
    return board


def query(
    engine: chess.engine.SimpleEngine, board: chess.Board, nodes: int, move: str | None = None
) -> dict[str, Any]:
    started = time.perf_counter()
    info = engine.analyse(
        board,
        chess.engine.Limit(nodes=nodes),
        game=object(),
        root_moves=[chess.Move.from_uci(move)] if move else None,
    )
    score = info["score"].pov(board.turn)
    pv = info.get("pv", [])
    if not pv or pv[0] not in board.legal_moves or (move and pv[0].uci() != move):
        raise ValueError("Engine did not return a legal root move")
    result: dict[str, Any] = {
        "requested_nodes": nodes,
        "move": pv[0].uci(),
        "score_cp": score.score(),
        "mate": score.mate(),
        "pv": [m.uci() for m in pv],
        "elapsed_ms": (time.perf_counter() - started) * 1000,
        "restricted_root_move": move,
        **{key: info.get(key) for key in ("nodes", "depth", "seldepth", "nps", "tbhits")},
    }
    if "wdl" in info:
        wdl = info["wdl"].pov(board.turn)
        result["model_wdl"] = [wdl.wins, wdl.draws, wdl.losses]
    return result


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.out.exists():
        raise ValueError("Choose a new output directory; evidence is never overwritten")
    executable = args.engine.resolve(strict=True)
    cases = collect_cases(args.run_dir, args.opponent, args.plies)
    if not cases:
        raise ValueError("No nonterminal positions matched the request")
    args.out.mkdir(parents=True)
    build = freeze(
        EngineSpec("a0-profile", "Frozen A0", "Original A0 alpha-beta", path=str(args.a0_build)),
        args.out / "build",
    )
    profile_dir = (args.out / "profiles").resolve()
    profile_dir.mkdir()
    worker_path = args.out / "profile-worker.py"
    worker_path.write_text(PROFILE_WORKER, encoding="utf-8")
    write_json(args.out / "cases.json", cases)
    options: dict[str, chess.engine.ConfigValue] = {
        "Threads": 1,
        "Hash": 64,
        "UCI_ShowWDL": True,
        "UCI_LimitStrength": False,
        "Skill Level": 20,
        "SyzygyPath": "",
    }
    report: dict[str, Any] = {
        "status": "running",
        "source_run": str(args.run_dir.resolve()),
        "source_manifest_sha256": file_hash(args.run_dir / "manifest.json"),
        "probe_source_sha256": file_hash(Path(__file__)),
        "profile_worker_sha256": file_hash(worker_path),
        "engine": str(executable),
        "engine_sha256": file_hash(executable),
        "options": options,
        "budgets": args.budgets,
        "verification_nodes": args.verify_nodes,
        "a0_nodes": args.a0_nodes,
        "a0_build": build,
        "cases": [],
        "protocol": "Single thread, fresh UCI game/hash/history for each query, MultiPV=1, "
        "no pondering or tablebases. Full recorded board history supplied.",
        "limitations": [
            "Development positions from played games; not a random sample or a strength test.",
            "Fixed-node search is not full-clock game time management.",
            "Higher-budget and restricted-root scores are estimates, not ground truth.",
            "Move disagreement alone does not establish a mistake or a forceable exploit.",
            "A0 timings include cProfile overhead; self-time buckets are approximate categories.",
        ],
    }
    write_json(args.out / "report.json", report)
    try:
        with chess.engine.SimpleEngine.popen_uci(str(executable), timeout=60) as engine:
            if "UCI_AnalyseMode" in engine.options:
                options["UCI_AnalyseMode"] = False
            engine.configure(options)
            report["engine_identity"] = engine.id
            report["engine_option_defaults"] = {
                name: option.default for name, option in engine.options.items()
            }
            for case in cases:
                board = board_for(case)
                profiled = subprocess.run(
                    [sys.executable, "-c", PROFILE_WORKER],
                    cwd=build["frozen_path"],
                    input=json.dumps(
                        {
                            "case": case,
                            "nodes": args.a0_nodes,
                            "profile": str(profile_dir / f"{case['id']}.prof"),
                        }
                    ),
                    text=True,
                    capture_output=True,
                    check=True,
                    timeout=120,
                )
                probes = [query(engine, board, budget) for budget in args.budgets]
                verification = query(engine, board, args.verify_nodes)
                candidates = sorted({row["move"] for row in [*probes, verification]})
                restricted = (
                    [query(engine, board, args.verify_nodes, move) for move in candidates]
                    if len(candidates) > 1
                    else []
                )
                row = {
                    "case": case,
                    "a0": json.loads(profiled.stdout),
                    "budgets": probes,
                    "verification": verification,
                    "restricted_verification": restricted,
                    "different_root_moves": len(candidates),
                }
                report["cases"].append(row)
                write_json(args.out / "report.json", report)
                print(f"{case['id']}: {len(candidates)} root move(s)", flush=True)
        if file_hash(executable) != report["engine_sha256"]:
            raise ValueError("External engine changed during measurement")
        for name, expected in build["files"].items():
            if file_hash(Path(build["frozen_path"]) / name) != expected:
                raise ValueError("Frozen A0 build changed during measurement")
        report["status"] = "completed"
    except Exception as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        write_json(args.out / "report.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--opponent", default="stockfish")
    parser.add_argument("--a0-build", type=Path, default=Path(".chesslab/checkpoints/a0-0.1.2"))
    parser.add_argument("--plies", type=int, nargs="+", default=[8, 9, 24, 25])
    parser.add_argument("--budgets", type=int, nargs="+", default=[1000, 10000, 100000])
    parser.add_argument("--verify-nodes", type=int, default=1000000)
    parser.add_argument("--a0-nodes", type=int, default=5000)
    args = parser.parse_args()
    if min(args.budgets) < 1 or args.a0_nodes < 1 or args.verify_nodes <= max(args.budgets):
        parser.error("Positive budgets required; verification must exceed each screening budget")
    if min(args.plies) < 0 or len(set(args.plies)) != len(args.plies):
        parser.error("Choose distinct nonnegative plies")
    if args.budgets != sorted(set(args.budgets)):
        parser.error("Choose strictly increasing budgets")
    run(args)


if __name__ == "__main__":
    main()
