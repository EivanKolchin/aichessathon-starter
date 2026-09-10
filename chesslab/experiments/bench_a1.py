"""Measure exact board traversal and descriptive search throughput on a frozen A1 build."""

import argparse
import json
import math
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

from chesslab.experiments.bench_a0 import fixtures
from chesslab.registry import EngineSpec, file_hash, freeze, write_json

WORKER = r"""
import json, random, sys, time
from dataclasses import asdict
request=json.load(sys.stdin)
def progress(stage, **details):
    with open(request['progress'], 'a', encoding='utf-8') as stream:
        stream.write(json.dumps({'stage':stage,'time':time.time(),**details})+'\n')
progress('imports')
import chess, numpy as np, numba
from a0.search import Search as A0, SearchConfig as C0
from a1.search import Search as A1, SearchConfig as C1, warmup
from a1.board import from_board, perft, MAX_MOVES, UNDO_SIZE

def reference_perft(board, depth):
    if depth==0: return 1
    if depth==1: return board.legal_moves.count()
    total=0
    for move in board.legal_moves:
        board.push(move)
        total+=reference_perft(board,depth-1)
        board.pop()
    return total

started=time.perf_counter()
progress('warmup')
warmup()
board,state=from_board(chess.Board())
move_buffer=np.zeros((5,MAX_MOVES),dtype=np.int64)
undo_buffer=np.zeros((5,UNDO_SIZE),dtype=np.int64)
perft(board,state,2,move_buffer,undo_buffer)
A0(C0(max_nodes=1000)).analyse(chess.Board(),3600000,3600000)
warmup_seconds=time.perf_counter()-started
progress('ready', warmup_seconds=warmup_seconds)
rows=[]
for repeat in range(request['repeats']):
    cases=list(request['cases'])
    random.Random(20260910+repeat).shuffle(cases)
    for index,case in enumerate(cases):
        reference=chess.Board(case['fen'])
        for move in case['moves']: reference.push_uci(move)
        order=['a0','a1'] if (index+repeat)%2==0 else ['a1','a0']
        row={'case':case['id'],'repeat':repeat,'timing':case['timing'],'order':order}
        for engine in order:
            progress('search', case=case['id'], engine=engine, repeat=repeat)
            search=(A0(C0(max_nodes=request['nodes'])) if engine=='a0'
                    else A1(C1(max_nodes=request['nodes'])))
            started=time.perf_counter()
            result=search.analyse(reference,3600000,3600000)
            elapsed=(time.perf_counter()-started)*1000
            assert result.move in reference.legal_moves
            data=asdict(result); data['move']=result.move.uci(); data['elapsed_ms']=elapsed
            data['nps']=result.nodes*1000/max(elapsed,0.001)
            row[engine]=data
            # Conversion and buffer allocation are outside board traversal timing.
            board,state=from_board(reference)
            progress('perft', case=case['id'], engine=engine, repeat=repeat)
            started=time.perf_counter()
            nodes=(reference_perft(reference,3) if engine=='a0'
                   else perft(board,state,3,move_buffer,undo_buffer))
            row[engine]['perft_nodes']=nodes
            row[engine]['perft_ms']=(time.perf_counter()-started)*1000
        assert row['a0']['perft_nodes']==row['a1']['perft_nodes'],case['id']
        rows.append(row)
        progress('result', result=row)
print(json.dumps({'rows':rows,'warmup_seconds':warmup_seconds,
 'python':sys.version,'numpy':np.__version__,'numba':numba.__version__,'chess':chess.__version__}))
"""


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cases = []
    for case in sorted({row["case"] for row in rows if row["timing"]}):
        samples = [row for row in rows if row["case"] == case]
        record: dict[str, Any] = {"case": case}
        for engine in ("a0", "a1"):
            record[engine] = {
                key: statistics.median(row[engine][key] for row in samples)
                for key in ("elapsed_ms", "nps", "depth", "perft_ms")
            }
        record["perft_speedup"] = record["a0"]["perft_ms"] / record["a1"]["perft_ms"]
        record["nps_ratio"] = record["a1"]["nps"] / record["a0"]["nps"]
        cases.append(record)
    return {
        "comparisons": len(rows),
        "perft_identical": all(r["a0"]["perft_nodes"] == r["a1"]["perft_nodes"] for r in rows),
        "perft_geomean_speedup": math.exp(
            statistics.mean(math.log(c["perft_speedup"]) for c in cases)
        ),
        "search_nps_geomean_ratio": math.exp(
            statistics.mean(math.log(c["nps_ratio"]) for c in cases)
        ),
        "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--nodes", type=int, default=5000)
    args = parser.parse_args()
    if args.repeats < 1 or args.nodes < 1 or args.out.exists():
        parser.error("Positive budgets and a new output directory are required")
    args.out.mkdir(parents=True)
    frozen = freeze(
        EngineSpec("a1-bench", "A1 benchmark", "Original A1", path=str(args.build.resolve())),
        args.out / "build",
    )
    cases = fixtures()
    report: dict[str, Any] = {
        "status": "running",
        "build": frozen,
        "cases": cases,
        "nodes": args.nodes,
        "repeats": args.repeats,
        "source_sha256": file_hash(Path(__file__)),
        "method": "One trusted frozen process, untimed warmup, fresh Search per trial, "
        "shuffled cases and alternating engine order. Perft depth 3 traverses identical legal "
        "trees; setup excluded. Search throughput is descriptive: move ordering, root counting, "
        "pawn caching and transposition-bound policies differ. No Elo or pure-backend search "
        "speedup claim. Only development openings contribute to aggregate timing.",
    }
    worker = args.out / "worker.py"
    worker.write_text(WORKER, encoding="utf-8")
    report["worker_sha256"] = file_hash(worker)
    write_json(args.out / "report.json", report)
    try:
        completed = subprocess.run(
            [sys.executable, "-c", WORKER],
            cwd=frozen["frozen_path"],
            input=json.dumps({
                "cases": cases, "nodes": args.nodes, "repeats": args.repeats,
                "progress": str((args.out / "progress.jsonl").resolve()),
            }),
            text=True,
            capture_output=True,
            timeout=600,
            check=True,
        )
        report.update(json.loads(completed.stdout))
        for name, sha in frozen["files"].items():
            if file_hash(Path(frozen["frozen_path"]) / name) != sha:
                raise ValueError(f"Frozen source changed: {name}")
        report["summary"] = summarise(report["rows"])
        report["status"] = "completed"
    except Exception as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
        if isinstance(error, subprocess.CalledProcessError):
            report["error"] = f"Worker exited with status {error.returncode}"
            report["stderr"] = error.stderr
        raise
    finally:
        write_json(args.out / "report.json", report)
    print(json.dumps({k: v for k, v in report["summary"].items() if k != "cases"}, indent=2))


if __name__ == "__main__":
    main()
