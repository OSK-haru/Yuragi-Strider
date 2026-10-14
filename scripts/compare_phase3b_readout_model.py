"""Reanalyze the saved Phase 3B QPU data with model-side readout error.

This is a retrospective diagnostic.  It compares three views of the same
saved job without submitting new hardware work:

1. the latent model probability directly against raw QPU counts;
2. the production readout-error transform applied to the model probability
   against raw QPU counts; and
3. the legacy affine correction applied to QPU counts against the latent
   model probability.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.gates import apply_readout_error


DEFAULT_RAW = ROOT / "validation_hardware" / "raw" / (
    "phase3b_formal_audit_d9nlh5ssfqic73ar6f30.json"
)
DEFAULT_MODEL = ROOT / "validation_results" / "phase3b_native_model_comparison.json"
DEFAULT_OUTPUT = ROOT / "validation_results" / "phase3b_readout_model_reanalysis.json"


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(ROOT))
    except ValueError:
        return str(resolved)


def _observed_p1(record: dict[str, Any]) -> float:
    shots = int(record["shots"])
    if shots <= 0:
        raise ValueError("shots must be positive")
    counts = record["counts"]
    count_one = int(counts.get("1", 0))
    if count_one < 0 or count_one > shots:
        raise ValueError("count of outcome 1 must be between zero and shots")
    if sum(int(value) for value in counts.values()) != shots:
        raise ValueError("counts must sum to shots")
    return count_one / shots


def _raw_kind(model_kind: str) -> str:
    if model_kind == "t1":
        return "t1"
    if model_kind == "t2":
        return "t2_ramsey"
    raise ValueError(f"unexpected model kind: {model_kind}")


def _model_observed_p1(true_p1: float, p10: float, p01: float) -> float:
    observed = apply_readout_error(
        {"0": 1.0 - true_p1, "1": true_p1},
        1,
        [(p10, p01)],
    )
    return float(observed["1"])


def _summary(rows: list[dict[str, Any]], difference_key: str) -> dict[str, float | int]:
    differences = [float(row[difference_key]) for row in rows]
    return {
        "points": len(differences),
        "mean_absolute_difference": float(np.mean(differences)),
        "root_mean_square_difference": float(
            math.sqrt(np.mean(np.square(differences)))
        ),
        "max_absolute_difference": max(differences),
    }


def _bootstrap_point_intervals(
    *,
    rows: list[dict[str, Any]],
    readout_zero: dict[str, Any],
    readout_one: dict[str, Any],
    replicates: int,
    seed: int,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, float | int]]]:
    if replicates < 100:
        raise ValueError("bootstrap replicates must be at least 100")
    rng = np.random.default_rng(seed)

    zero_shots = int(readout_zero["shots"])
    one_shots = int(readout_one["shots"])
    measured_p10 = _observed_p1(readout_zero)
    measured_p11 = _observed_p1(readout_one)
    sampled_p10 = rng.binomial(zero_shots, measured_p10, replicates) / zero_shots
    sampled_p11 = rng.binomial(one_shots, measured_p11, replicates) / one_shots
    sampled_span = sampled_p11 - sampled_p10
    valid = sampled_span > 0.0
    if int(np.count_nonzero(valid)) != replicates:
        sampled_p10 = sampled_p10[valid]
        sampled_p11 = sampled_p11[valid]
        sampled_span = sampled_span[valid]

    interval_rows: list[dict[str, Any]] = []
    for row in rows:
        shots = int(row["shots"])
        observed_p1 = float(row["qpu_raw_p1"])
        true_p1 = float(row["model_true_p1"])
        sampled_observed = rng.binomial(shots, observed_p1, len(sampled_span)) / shots

        baseline_residual = sampled_observed - true_p1
        sampled_model_observed = sampled_p10 + sampled_span * true_p1
        model_side_residual = sampled_observed - sampled_model_observed
        corrected = np.clip(
            (sampled_observed - sampled_p10) / sampled_span,
            0.0,
            1.0,
        )
        corrected_residual = corrected - true_p1

        def interval(values: np.ndarray) -> dict[str, float | bool]:
            lower, upper = np.quantile(values, [0.025, 0.975])
            return {
                "lower_95": float(lower),
                "upper_95": float(upper),
                "covers_zero": bool(lower <= 0.0 <= upper),
            }

        interval_rows.append(
            {
                "kind": row["kind"],
                "delay_us": row["delay_us"],
                "baseline_raw_residual": interval(baseline_residual),
                "model_side_readout_residual": interval(model_side_residual),
                "qpu_side_corrected_residual": interval(corrected_residual),
            }
        )

    coverage: dict[str, dict[str, float | int]] = {}
    for kind in ("t1", "t2"):
        selected = [row for row in interval_rows if row["kind"] == kind]
        coverage[kind] = {"points": len(selected)}
        for method in (
            "baseline_raw_residual",
            "model_side_readout_residual",
            "qpu_side_corrected_residual",
        ):
            covered = sum(bool(row[method]["covers_zero"]) for row in selected)
            coverage[kind][method] = covered / len(selected)
    return interval_rows, coverage


def analyze(
    raw_path: Path,
    model_path: Path,
    *,
    bootstrap_replicates: int,
    seed: int,
) -> dict[str, Any]:
    raw = _load_object(raw_path)
    model = _load_object(model_path)
    if model.get("source_job_id") != raw.get("job_id"):
        raise ValueError("raw QPU data and model comparison must use the same job ID")

    raw_records = raw.get("raw_counts")
    model_rows = model.get("rows")
    if not isinstance(raw_records, list) or not isinstance(model_rows, list):
        raise ValueError("raw_counts and model rows must be lists")

    zero_records = [row for row in raw_records if row.get("kind") == "readout_zero"]
    one_records = [row for row in raw_records if row.get("kind") == "readout_one"]
    if len(zero_records) != 1 or len(one_records) != 1:
        raise ValueError("exactly one readout-zero and one readout-one record are required")
    readout_zero = zero_records[0]
    readout_one = one_records[0]
    p10 = _observed_p1(readout_zero)
    p11 = _observed_p1(readout_one)
    p01 = 1.0 - p11
    span = p11 - p10
    if span <= 0.0:
        raise ValueError("readout calibration span must be positive")

    raw_by_key: dict[tuple[str, float], dict[str, Any]] = {}
    for record in raw_records:
        if record.get("kind") not in {"t1", "t2_ramsey"}:
            continue
        key = (str(record["kind"]), float(record["delay_us"]))
        if key in raw_by_key:
            raise ValueError(f"duplicate raw record: {key}")
        raw_by_key[key] = record

    rows: list[dict[str, Any]] = []
    used_keys: set[tuple[str, float]] = set()
    for model_row in model_rows:
        kind = str(model_row["kind"])
        key = (_raw_kind(kind), float(model_row["delay_us"]))
        if key in used_keys:
            raise ValueError(f"duplicate model row: {key}")
        if key not in raw_by_key:
            raise ValueError(f"model row has no matching raw record: {key}")
        used_keys.add(key)
        raw_record = raw_by_key[key]
        raw_p1 = _observed_p1(raw_record)
        true_p1 = float(model_row["native_model_p1"])
        model_observed_p1 = _model_observed_p1(true_p1, p10, p01)
        corrected_p1 = (raw_p1 - p10) / span
        rows.append(
            {
                "kind": kind,
                "delay_us": float(model_row["delay_us"]),
                "shots": int(raw_record["shots"]),
                "qpu_raw_p1": raw_p1,
                "qpu_readout_corrected_p1": corrected_p1,
                "model_true_p1": true_p1,
                "model_with_readout_p1": model_observed_p1,
                "baseline_raw_absolute_difference": abs(raw_p1 - true_p1),
                "model_side_readout_absolute_difference": abs(
                    raw_p1 - model_observed_p1
                ),
                "qpu_side_corrected_absolute_difference": abs(
                    corrected_p1 - true_p1
                ),
            }
        )
    if used_keys != set(raw_by_key):
        missing = sorted(set(raw_by_key) - used_keys)
        raise ValueError(f"raw records have no matching model rows: {missing}")

    intervals, coverage = _bootstrap_point_intervals(
        rows=rows,
        readout_zero=readout_zero,
        readout_one=readout_one,
        replicates=bootstrap_replicates,
        seed=seed,
    )
    summaries: dict[str, Any] = {}
    for kind in ("t1", "t2"):
        selected = [row for row in rows if row["kind"] == kind]
        baseline_summary = _summary(selected, "baseline_raw_absolute_difference")
        model_side_summary = _summary(
            selected, "model_side_readout_absolute_difference"
        )
        qpu_side_summary = _summary(
            selected, "qpu_side_corrected_absolute_difference"
        )
        baseline_mae = float(baseline_summary["mean_absolute_difference"])
        model_side_mae = float(model_side_summary["mean_absolute_difference"])
        summaries[kind] = {
            "baseline_true_model_vs_raw_qpu": baseline_summary,
            "model_side_readout_vs_raw_qpu": model_side_summary,
            "qpu_side_correction_vs_true_model": qpu_side_summary,
            "model_side_change_vs_baseline": {
                "mean_absolute_difference_delta": model_side_mae - baseline_mae,
                "relative_change": (
                    (model_side_mae - baseline_mae) / baseline_mae
                    if baseline_mae > 0.0
                    else None
                ),
                "points_improved": sum(
                    row["model_side_readout_absolute_difference"]
                    < row["baseline_raw_absolute_difference"]
                    for row in selected
                ),
                "points_worsened": sum(
                    row["model_side_readout_absolute_difference"]
                    > row["baseline_raw_absolute_difference"]
                    for row in selected
                ),
                "points_unchanged": sum(
                    row["model_side_readout_absolute_difference"]
                    == row["baseline_raw_absolute_difference"]
                    for row in selected
                ),
            },
            "pointwise_95_percent_coverage": coverage[kind],
        }

    return {
        "analysis_id": "phase3b_readout_model_reanalysis_v1",
        "analysis_type": "retrospective_diagnostic_not_formal_holdout",
        "source": {
            "job_id": raw["job_id"],
            "backend": raw["backend_properties"],
            "raw_result": _source_path(raw_path),
            "raw_result_sha256": _sha256(raw_path),
            "model_comparison": _source_path(model_path),
            "model_comparison_sha256": _sha256(model_path),
        },
        "data_quality": {
            "readout_calibration_records": 2,
            "t1_records": sum(row["kind"] == "t1" for row in rows),
            "t2_ramsey_records": sum(row["kind"] == "t2" for row in rows),
            "shots_per_record": sorted({int(row["shots"]) for row in rows}),
            "job_ids_match": True,
            "all_raw_and_model_points_matched": True,
        },
        "readout_calibration": {
            "p10_observed_1_given_0": p10,
            "p01_observed_0_given_1": p01,
            "p11_observed_1_given_1": p11,
            "assignment_span": span,
            "source": "same_job_readout_zero_and_readout_one_records",
        },
        "methods": {
            "baseline": "compare latent model P1 directly with raw QPU P1",
            "model_side": (
                "apply the production affine readout-error transform to model P1, "
                "then compare with raw QPU P1"
            ),
            "qpu_side": (
                "apply the legacy affine correction to raw QPU P1, then compare "
                "with latent model P1"
            ),
            "bootstrap": {
                "replicates": bootstrap_replicates,
                "seed": seed,
                "interval": "pointwise percentile 95% residual interval",
                "calibration_uncertainty": "same-job p10 and p11 binomial resampling",
                "measurement_uncertainty": "per-point binomial resampling",
            },
        },
        "summaries": summaries,
        "rows": rows,
        "bootstrap_point_intervals": intervals,
        "limitations": [
            "This reuses previously inspected hardware data and is therefore retrospective, not a new formal holdout.",
            "The model predictions come from the existing native-model comparison and are not refit here.",
            "The Ramsey prediction is the zero-detuning production model; the post-hoc detuning scan is intentionally not inserted.",
            "Model-side readout and QPU-side affine correction are algebraically related; lower error on the observed scale alone is not evidence of a better latent physical model.",
            "The analysis tests whether the saved assignment-error calibration changes agreement; it does not validate a resonator or IQ-discrimination model.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--bootstrap-replicates", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=20260906)
    args = parser.parse_args()

    report = analyze(
        args.raw,
        args.model,
        bootstrap_replicates=args.bootstrap_replicates,
        seed=args.seed,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["summaries"], indent=2, ensure_ascii=True))
    print(f"WROTE: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
