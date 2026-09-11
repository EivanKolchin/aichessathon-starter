"""Train the A1 residual evaluator on a large extracted dataset.

The network learns a *residual* on top of the hand-crafted evaluation, never the teacher score
itself. The label is ``clip((teacher_cp - classical_cp) / RESIDUAL_CP, -1, 1)`` and the network
fits ``tanh(raw)`` to it, which is exactly what ``a1.neural.create_evaluator`` then adds back:
``classical + blend * RESIDUAL_CP * tanh(raw)``. Fitting the teacher score directly would make
the network re-learn material that the classical term already supplies, and double-count it.

Numba is used here only for the sparse forward and backward passes of offline training. Nothing
in this module is imported by ``a1`` or by ``agent.py``.
"""

import argparse
import json
import time
from pathlib import Path
from typing import Any, Final

import numba
import numpy as np
from numpy.typing import NDArray

from a1.neural import INPUTS, RESIDUAL_CP, Weights
from chesslab.experiments.nn_features import MAX_PIECES
from chesslab.registry import file_hash, write_json

type F32 = NDArray[np.float32]
type I16 = NDArray[np.int16]

TRAIN_BUCKETS: Final = 90
VALIDATION_BUCKETS: Final = 95


@numba.njit(cache=True, parallel=True, fastmath=True)
def _forward(idx: I16, w1: F32, b1: F32, hidden: F32) -> None:
    """hidden[b] = clip(sum of the w1 rows this position sets, plus b1, 0, 1)."""
    for row in numba.prange(idx.shape[0]):
        for neuron in range(b1.size):
            hidden[row, neuron] = b1[neuron]
        for slot in range(idx.shape[1]):
            feature = idx[row, slot]
            if feature < 0:
                break
            for neuron in range(b1.size):
                hidden[row, neuron] += w1[feature, neuron]


@numba.njit(cache=True, parallel=True, fastmath=True)
def _scatter(idx: I16, delta: F32, buffers: F32) -> None:
    """Accumulate the w1 gradient into one private buffer per slice, avoiding a write race."""
    slices = buffers.shape[0]
    span = (idx.shape[0] + slices - 1) // slices
    for part in numba.prange(slices):
        for row in range(part * span, min((part + 1) * span, idx.shape[0])):
            for slot in range(idx.shape[1]):
                feature = idx[row, slot]
                if feature < 0:
                    break
                for neuron in range(delta.shape[1]):
                    buffers[part, feature, neuron] += delta[row, neuron]


def _targets(score: I16, base: I16) -> F32:
    """The bounded residual the network must reproduce."""
    residual = (score.astype(np.float32) - base.astype(np.float32)) / RESIDUAL_CP
    return np.clip(residual, -1.0, 1.0)


def load(directory: Path) -> dict[str, Any]:
    """Memory-map the extraction, drop duplicate positions and split by canonical-key bucket."""
    counts = (directory / "key.bin").stat().st_size // 8
    idx = np.memmap(directory / "idx.bin", dtype=np.int16, mode="r").reshape(counts, MAX_PIECES)
    base = np.memmap(directory / "base.bin", dtype=np.int16, mode="r")
    score = np.memmap(directory / "score.bin", dtype=np.int16, mode="r")
    key = np.asarray(np.memmap(directory / "key.bin", dtype=np.uint64, mode="r"))
    _, unique = np.unique(key, return_index=True)
    unique.sort()
    bucket = key[unique] % 100
    splits = {
        "train": unique[bucket < TRAIN_BUCKETS],
        "validation": unique[(bucket >= TRAIN_BUCKETS) & (bucket < VALIDATION_BUCKETS)],
        "test": unique[bucket >= VALIDATION_BUCKETS],
    }
    data = {}
    for name, rows in splits.items():
        data[name] = (
            np.ascontiguousarray(idx[rows]),
            np.ascontiguousarray(base[rows]),
            np.ascontiguousarray(score[rows]),
        )
    return {"splits": data, "extracted": int(counts), "unique": int(unique.size)}


def evaluate_split(
    weights: tuple[F32, F32, F32, F32], split: tuple[I16, I16, I16], batch: int = 1 << 17
) -> dict[str, float]:
    """Holdout loss of the residual, and the centipawn error of the blended score."""
    w1, b1, w2, b2 = weights
    idx, base, score = split
    target = _targets(score, base)
    total = 0.0
    absolute = 0.0
    baseline_absolute = 0.0
    for start in range(0, idx.shape[0], batch):
        chunk = idx[start : start + batch]
        hidden = np.empty((chunk.shape[0], b1.size), dtype=np.float32)
        _forward(chunk, w1, b1, hidden)
        np.clip(hidden, 0.0, 1.0, out=hidden)
        predicted = np.tanh(hidden @ w2 + b2[0])
        window = slice(start, start + chunk.shape[0])
        total += float(np.sum((predicted - target[window]) ** 2))
        blended = base[window].astype(np.float32) + RESIDUAL_CP * predicted
        absolute += float(np.sum(np.abs(blended - score[window])))
        baseline_absolute += float(
            np.sum(np.abs(base[window].astype(np.float32) - score[window]))
        )
    rows = max(1, idx.shape[0])
    return {
        "positions": int(idx.shape[0]),
        "residual_mse": total / rows,
        "baseline_residual_mse": float(np.mean(target.astype(np.float64) ** 2)),
        "neural_mae_cp": absolute / rows,
        "baseline_mae_cp": baseline_absolute / rows,
    }


def fit(
    data: dict[str, tuple[I16, I16, I16]],
    *,
    hidden_size: int,
    epochs: int,
    batch: int,
    learning_rate: float,
    decay: float,
    seed: int,
) -> tuple[Weights, list[dict[str, float]]]:
    if not 1 <= hidden_size <= 128 or epochs < 1 or not 0 < learning_rate <= 0.1:
        raise ValueError("Invalid training settings")
    rng = np.random.default_rng(seed)
    w1 = rng.normal(0, 0.025, (INPUTS, hidden_size)).astype(np.float32)
    b1 = np.full(hidden_size, 0.25, dtype=np.float32)
    # A zero second layer starts the agent at exactly the classical evaluation.
    w2 = np.zeros(hidden_size, dtype=np.float32)
    b2 = np.zeros(1, dtype=np.float32)
    arrays = [w1, b1, w2, b2]
    first = [np.zeros_like(value) for value in arrays]
    second = [np.zeros_like(value) for value in arrays]
    idx, base, score = data["train"]
    target = _targets(score, base)
    slices = numba.get_num_threads()
    buffers = np.zeros((slices, INPUTS, hidden_size), dtype=np.float32)
    best = tuple(value.copy() for value in arrays)
    best_loss = float("inf")
    history: list[dict[str, float]] = []
    step = 0
    for epoch in range(epochs):
        started = time.perf_counter()
        rate = learning_rate * decay**epoch
        order = rng.permutation(idx.shape[0])
        for offset in range(0, order.size - batch + 1, batch):
            rows = np.sort(order[offset : offset + batch])
            x = idx[rows]
            activation = np.empty((batch, hidden_size), dtype=np.float32)
            _forward(x, w1, b1, activation)
            h = np.clip(activation, 0.0, 1.0)
            predicted = np.tanh(h @ w2 + b2[0])
            d_raw = (2.0 / batch) * (predicted - target[rows]) * (1.0 - predicted**2)
            d_hidden = (
                d_raw[:, None] * w2 * ((activation > 0.0) & (activation < 1.0))
            ).astype(np.float32)
            buffers[:] = 0.0
            _scatter(x, d_hidden, buffers)
            gradients = [
                buffers.sum(axis=0) + np.float32(1e-7) * w1,
                d_hidden.sum(axis=0),
                (h.T @ d_raw).astype(np.float32) + np.float32(1e-7) * w2,
                np.array([d_raw.sum()], dtype=np.float32),
            ]
            step += 1
            for value, grad, moment, variance in zip(
                arrays, gradients, first, second, strict=True
            ):
                moment *= 0.9
                moment += 0.1 * grad
                variance *= 0.999
                variance += 0.001 * grad**2
                value -= (
                    rate
                    * (moment / (1 - 0.9**step))
                    / (np.sqrt(variance / (1 - 0.999**step)) + 1e-8)
                ).astype(np.float32)
        metrics = evaluate_split(tuple(arrays), data["validation"])  # type: ignore[arg-type]
        history.append(
            {
                "epoch": float(epoch + 1),
                "learning_rate": rate,
                "validation_residual_mse": metrics["residual_mse"],
                "validation_mae_cp": metrics["neural_mae_cp"],
                "seconds": time.perf_counter() - started,
            }
        )
        print(json.dumps(history[-1]), flush=True)
        if metrics["residual_mse"] < best_loss:
            best_loss = metrics["residual_mse"]
            best = tuple(value.copy() for value in arrays)
    weights = Weights(*(np.ascontiguousarray(value, dtype=np.float64) for value in best))
    weights.validate()
    return weights, history


def train(
    dataset: Path,
    output: Path,
    hidden_size: int,
    epochs: int,
    batch: int,
    learning_rate: float,
    decay: float,
    seed: int,
    limit: int,
) -> dict[str, Any]:
    if output.exists():
        raise ValueError("Choose a new model directory")
    loaded = load(dataset)
    data: dict[str, tuple[I16, I16, I16]] = loaded["splits"]
    if limit and data["train"][0].shape[0] > limit:
        data["train"] = tuple(value[:limit] for value in data["train"])  # type: ignore[assignment]
    weights, history = fit(
        data,
        hidden_size=hidden_size,
        epochs=epochs,
        batch=batch,
        learning_rate=learning_rate,
        decay=decay,
        seed=seed,
    )
    output.mkdir(parents=True)
    weights.save(output / "model.npz")
    packed = tuple(value.astype(np.float32) for value in (weights.w1, weights.b1, weights.w2, weights.b2))
    metrics = {
        name: evaluate_split(packed, split)  # type: ignore[arg-type]
        for name, split in data.items()
    }
    report: dict[str, Any] = {
        "schema": 1,
        "architecture": f"768-{hidden_size}-1 bounded residual on the classical evaluation",
        "trained_from_scratch": True,
        "target": "clip((teacher_cp - classical_cp) / 600, -1, 1), side-to-move relative",
        "seed": seed,
        "epochs": epochs,
        "batch": batch,
        "learning_rate": learning_rate,
        "learning_rate_decay": decay,
        "train_positions": int(data["train"][0].shape[0]),
        "positions_extracted": loaded["extracted"],
        "positions_unique": loaded["unique"],
        "selected_epoch": min(history, key=lambda item: item["validation_residual_mse"])["epoch"],
        "dataset": str(dataset.resolve()),
        "dataset_sha256": file_hash(dataset / "extract.json"),
        "model_sha256": file_hash(output / "model.npz"),
        "metrics": metrics,
        "history": history,
        "split_by": "canonical position-key bucket (90/5/5); identical feature vectors share a"
        " bucket, so no position the network can see is split across train and holdout",
        "loss": "MSE of the bounded residual; not a calibrated game-outcome probability",
        "limitations": "Teacher labels are finite-depth search output. Predictive loss is not"
        " playing strength; only paired equal-time games decide that.",
        "source_sha256": {
            "nn_train.py": file_hash(Path(__file__)),
            "nn_dataset.py": file_hash(Path(__file__).with_name("nn_dataset.py")),
            "nn_features.py": file_hash(Path(__file__).with_name("nn_features.py")),
            "neural.py": file_hash(Path(__file__).parents[2] / "a1" / "neural.py"),
        },
    }
    write_json(output / "training.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--hidden", type=int, default=32)
    parser.add_argument("--epochs", type=int, default=8)
    parser.add_argument("--batch", type=int, default=8192)
    parser.add_argument("--learning-rate", type=float, default=0.004)
    parser.add_argument("--decay", type=float, default=0.7)
    parser.add_argument("--seed", type=int, default=20260911)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    report = train(
        args.dataset,
        args.out,
        args.hidden,
        args.epochs,
        args.batch,
        args.learning_rate,
        args.decay,
        args.seed,
        args.limit,
    )
    print(json.dumps(report["metrics"], indent=2))


if __name__ == "__main__":
    main()
