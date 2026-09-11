"""Train an original residual evaluator on a labelled, opening-separated dataset."""

import argparse
import json
from pathlib import Path
from typing import Any

import chess
import numpy as np

from a1.board import from_board
from a1.evaluation import evaluate
from a1.neural import INPUTS, RESIDUAL_CP, SCORE_SCALE, FloatArray, Weights, features, predict
from chesslab.registry import file_hash, write_json


def fit(
    inputs: FloatArray,
    baseline: FloatArray,
    targets: FloatArray,
    validation: tuple[FloatArray, FloatArray, FloatArray],
    *,
    hidden: int = 32,
    epochs: int = 100,
    seed: int = 20260911,
    learning_rate: float = 0.003,
) -> tuple[Weights, list[dict[str, float]]]:
    """Adam from a seeded random initialisation; select epochs using validation only."""
    if not 1 <= hidden <= 128 or epochs < 1 or not 0 < learning_rate <= 0.1:
        raise ValueError("Invalid training settings")
    rng = np.random.default_rng(seed)
    weights = Weights(
        rng.normal(0, 0.025, (INPUTS, hidden)), np.full(hidden, 0.25), np.zeros(hidden), np.zeros(1)
    )
    arrays = (weights.w1, weights.b1, weights.w2, weights.b2)
    first = [np.zeros_like(value) for value in arrays]
    second = [np.zeros_like(value) for value in arrays]
    target = np.tanh(targets / SCORE_SCALE)
    best = Weights(*(array.copy() for array in arrays))
    best_loss = float("inf")
    history: list[dict[str, float]] = []
    step = 0
    for epoch in range(epochs):
        order = rng.permutation(len(inputs))
        for offset in range(0, len(order), 128):
            batch = order[offset : offset + 128]
            x = inputs[batch]
            activation = x @ weights.w1 + weights.b1
            h = np.clip(activation, 0.0, 1.0)
            residual = np.tanh(h @ weights.w2 + weights.b2[0])
            output = np.tanh((baseline[batch] + RESIDUAL_CP * residual) / SCORE_SCALE)
            d_raw = (
                2
                * (output - target[batch])
                / len(batch)
                * (1 - output**2)
                * (RESIDUAL_CP / SCORE_SCALE)
                * (1 - residual**2)
            )
            d_hidden = d_raw[:, None] * weights.w2 * ((activation > 0) & (activation < 1))
            gradients = (
                x.T @ d_hidden + 0.0001 * weights.w1,
                d_hidden.sum(axis=0),
                h.T @ d_raw + 0.0001 * weights.w2,
                np.array([d_raw.sum()]),
            )
            step += 1
            for value, grad, moment, variance in zip(arrays, gradients, first, second, strict=True):
                moment *= 0.9
                moment += 0.1 * grad
                variance *= 0.999
                variance += 0.001 * grad**2
                value -= (
                    learning_rate
                    * (moment / (1 - 0.9**step))
                    / (np.sqrt(variance / (1 - 0.999**step)) + 1e-8)
                )
        val_x, val_base, val_target = validation
        loss = float(
            np.mean(
                (
                    np.tanh(predict(weights, val_x, val_base) / SCORE_SCALE)
                    - np.tanh(val_target / SCORE_SCALE)
                )
                ** 2
            )
        )
        train_loss = float(
            np.mean((np.tanh(predict(weights, inputs, baseline) / SCORE_SCALE) - target) ** 2)
        )
        history.append({"epoch": float(epoch + 1), "train_mse": train_loss, "validation_mse": loss})
        if loss < best_loss:
            best_loss = loss
            best = Weights(*(array.copy() for array in arrays))
    best.validate()
    return best, history


def train(dataset: Path, output: Path, hidden: int, epochs: int, seed: int) -> dict[str, Any]:
    if output.exists():
        raise ValueError("Choose a new model directory")
    data = json.loads(dataset.read_text(encoding="utf-8"))
    rows = data["positions"]
    arrays = []
    group_sets = []
    position_sets = []
    for split in ("train", "validation", "test"):
        subset = [
            row for row in rows if row["split"] == split and row["teacher"]["score_cp"] is not None
        ]
        if not subset:
            raise ValueError(f"No centipawn-labelled positions in {split}")
        boards = [chess.Board(row["fen"]) for row in subset]
        if any(not board.is_valid() or board.is_game_over() for board in boards):
            raise ValueError("Dataset contains invalid or terminal positions")
        group_sets.append({row["opening_group"] for row in subset})
        position_sets.append({features(board).tobytes() for board in boards})
        arrays.append(
            (
                np.stack([features(board) for board in boards]),
                np.array([evaluate(*from_board(board)) for board in boards], dtype=np.float64),
                np.array([row["teacher"]["score_cp"] for row in subset], dtype=np.float64),
            )
        )
    for a, b in ((0, 1), (0, 2), (1, 2)):
        if group_sets[a] & group_sets[b] or position_sets[a] & position_sets[b]:
            raise ValueError(
                "Dataset leaks opening families or equivalent neural inputs across splits"
            )
    weights, history = fit(*arrays[0], arrays[1], hidden=hidden, epochs=epochs, seed=seed)
    output.mkdir(parents=True)
    weights.save(output / "model.npz")
    metrics = {}
    for name, (x, base, target) in zip(("train", "validation", "test"), arrays, strict=True):
        prediction = predict(weights, x, base)
        metrics[name] = {
            "positions": len(x),
            "baseline_mse": float(
                np.mean((np.tanh(base / SCORE_SCALE) - np.tanh(target / SCORE_SCALE)) ** 2)
            ),
            "neural_mse": float(
                np.mean((np.tanh(prediction / SCORE_SCALE) - np.tanh(target / SCORE_SCALE)) ** 2)
            ),
            "baseline_mae_cp": float(np.mean(np.abs(base - target))),
            "neural_mae_cp": float(np.mean(np.abs(prediction - target))),
        }
    report = {
        "schema": 1,
        "architecture": f"768-{hidden}-1 positional residual",
        "trained_from_scratch": True,
        "seed": seed,
        "epochs": epochs,
        "selected_epoch": min(history, key=lambda item: item["validation_mse"])["epoch"],
        "dataset_sha256": file_hash(dataset),
        "dataset": str(dataset.resolve()),
        "model_sha256": file_hash(output / "model.npz"),
        "metrics": metrics,
        "history": history,
        "split_opening_groups": dict(
            zip(
                ("train", "validation", "test"),
                [sorted(groups) for groups in group_sets],
                strict=True,
            )
        ),
        "loss": "MSE of tanh(cp/400); not a calibrated game-outcome probability",
        "limitations": (
            "Pilot imitation of finite-search labels; predictive loss is not playing strength."
        ),
        "source_sha256": {
            str(Path(__file__).name): file_hash(Path(__file__)),
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
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260911)
    args = parser.parse_args()
    report = train(args.dataset, args.out, args.hidden, args.epochs, args.seed)
    print(json.dumps(report["metrics"], indent=2))


if __name__ == "__main__":
    main()
