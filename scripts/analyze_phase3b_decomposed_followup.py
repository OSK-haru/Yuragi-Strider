"""Analyze readout, T1, and Ramsey/T2 components from one follow-up job."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RAW = ROOT / "validation_hardware" / "raw" / (
    "phase3b_decomposed_followup_d9nki08qs0bc73e3ht8g.json"
)
DEFAULT_OUTPUT = ROOT / "validation_results" / "phase3b_decomposed_comparison.json"


def _fit_decay(points: list[tuple[float, float]], transform: str) -> dict[str, Any]:
    usable = [(t, value) for t, value in points if value > 1e-9]
    if len(usable) < 2:
        return {"status": "insufficient_points", "points": len(usable)}
    mean_t = sum(t for t, _ in usable) / len(usable)
    mean_y = sum(math.log(value) for _, value in usable) / len(usable)
    denominator = sum((t - mean_t) ** 2 for t, _ in usable)
    slope = sum(
        (t - mean_t) * (math.log(value) - mean_y) for t, value in usable
    ) / denominator
    intercept = mean_y - slope * mean_t
    if slope >= 0.0:
        return {"status": "non_decay", "points": len(usable), "transform": transform}
    residual = sum(
        (math.log(value) - (intercept + slope * t)) ** 2
        for t, value in usable
    )
    return {
        "status": "fit",
        "points": len(usable),
        "transform": transform,
        "decay_time_us": -1.0 / slope,
        "initial_amplitude": math.exp(intercept),
        "log_rmse": math.sqrt(residual / len(usable)),
    }


def _fit_damped_oscillation(
    points: list[tuple[float, float]],
    *,
    frequency_steps: int = 501,
    t2_steps: int = 401,
) -> dict[str, Any]:
    """Grid-fit C(t)=exp(-t/T2)(a cos(2 pi f t)+b sin(2 pi f t))."""
    if len(points) < 4:
        return {"status": "insufficient_points", "points": len(points)}
    best: dict[str, Any] | None = None
    candidates: list[dict[str, float]] = []
    # The 20 us spacing makes frequencies above 0.025 cycles/us aliased.
    for frequency_index in range(frequency_steps):
        frequency = 0.025 * frequency_index / max(1, frequency_steps - 1)
        for exponent_index in range(t2_steps):
            log_t2 = math.log(10.0) + (
                math.log(3000.0) - math.log(10.0)
            ) * exponent_index / max(1, t2_steps - 1)
            t2 = math.exp(log_t2)
            design: list[tuple[float, float, float]] = []
            for time_us, value in points:
                envelope = 1.0 if math.isinf(t2) else math.exp(-time_us / t2)
                angle = 2.0 * math.pi * frequency * time_us
                design.append((envelope * math.cos(angle), envelope * math.sin(angle), value))
            xx = sum(x * x for x, _, _ in design)
            yy = sum(y * y for _, y, _ in design)
            xy = sum(x * y for x, y, _ in design)
            xv = sum(x * value for x, _, value in design)
            yv = sum(y * value for _, y, value in design)
            determinant = xx * yy - xy * xy
            if determinant <= 1e-15:
                continue
            a = (xv * yy - yv * xy) / determinant
            b = (yv * xx - xv * xy) / determinant
            residual = sum(
                (value - (a * x + b * y)) ** 2
                for x, y, value in design
            )
            candidate = {
                "frequency_cycles_per_us": frequency,
                "t2_us": t2,
                "amplitude": math.hypot(a, b),
                "phase_rad": math.atan2(-b, a),
                "rmse": math.sqrt(residual / len(points)),
            }
            candidates.append(candidate)
            if best is None or candidate["rmse"] < best["rmse"]:
                best = candidate
    if best is None:
        return {"status": "fit_failed", "points": len(points)}
    candidates.sort(key=lambda item: item["rmse"])
    return {
        "status": "fit",
        "points": len(points),
        "model": "C(t)=A exp(-t/T2) cos(2 pi f t + phase)",
        **best,
        "top_candidate_rmse": [item["rmse"] for item in candidates[:5]],
        "identifiability_warning": (
            f"{len(points)} points improve identifiability, but this remains an "
            "exploratory single-tone fit; confidence intervals are not included."
        ),
    }


def analyze(raw_path: Path, output_path: Path) -> dict[str, Any]:
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    records = raw["raw_counts"]
    by_kind = {record["kind"]: record for record in records if record["kind"] in {"readout_zero", "readout_one", "x_prep"}}
    readout_zero_p1 = int(by_kind["readout_zero"]["counts"].get("1", 0)) / 256.0
    readout_one_p1 = int(by_kind["readout_one"]["counts"].get("1", 0)) / 256.0
    readout_span = readout_one_p1 - readout_zero_p1
    if readout_span <= 0.0:
        raise ValueError("readout calibration span must be positive")

    def correct_p1(record: dict[str, Any]) -> float:
        observed = int(record["counts"].get("1", 0)) / float(record["shots"])
        return min(1.0, max(0.0, (observed - readout_zero_p1) / readout_span))

    rows = []
    t1_points = []
    t2_points = []
    for record in records:
        kind = record["kind"]
        if kind not in {"x_prep", "t1", "t2_ramsey"}:
            continue
        corrected = correct_p1(record)
        row = {
            "kind": kind,
            "delay_dt": record["delay_dt"],
            "delay_us": record["delay_us"],
            "observed_p1": int(record["counts"].get("1", 0)) / float(record["shots"]),
            "readout_corrected_p1": corrected,
        }
        if kind == "t1":
            t1_points.append((float(record["delay_us"]), corrected))
            row["model_p1_t1_calibrated"] = math.exp(-float(record["delay_us"]) / 303.33)
        elif kind == "t2_ramsey":
            signed_contrast = 1.0 - 2.0 * corrected
            contrast = max(1e-9, abs(signed_contrast))
            t2_points.append((float(record["delay_us"]), signed_contrast))
            row["contrast"] = signed_contrast
            row["model_p1_t2_calibrated"] = (1.0 - math.exp(-float(record["delay_us"]) / 339.99)) / 2.0
        else:
            row["model_p1_x_ideal"] = 1.0
        rows.append(row)

    t1_fit = _fit_decay(t1_points, "corrected P1")
    if any(value <= 0.0 for _, value in t2_points):
        t2_fit = {
            "status": "oscillatory_or_inadequate",
            "points": len(t2_points),
            "reason": "Ramsey contrast changes sign; a damped oscillation fit is required",
        }
    else:
        t2_fit = _fit_decay(t2_points, "corrected Ramsey contrast")
    damped_fit = _fit_damped_oscillation(t2_points)
    t1_us = t1_fit.get("decay_time_us")
    t2_us = t2_fit.get("decay_time_us")
    tphi_zero_temperature_us = None
    if isinstance(t1_us, float) and isinstance(t2_us, float):
        inverse = 1.0 / t2_us - 1.0 / (2.0 * t1_us)
        if inverse > 0.0:
            tphi_zero_temperature_us = 1.0 / inverse

    report = {
        "analysis_id": "phase3b_decomposed_followup_analysis_v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_raw_result": str(raw_path.resolve().relative_to(ROOT)),
        "job_id": raw["job_id"],
        "backend": raw["backend_properties"],
        "readout_calibration": {
            "p_observed_1_given_0": readout_zero_p1,
            "p_observed_1_given_1": readout_one_p1,
            "assignment_span": readout_span,
        },
        "x_preparation": {
            "corrected_p1": next(row["readout_corrected_p1"] for row in rows if row["kind"] == "x_prep"),
            "interpretation": "X preparation plus finite gate-time error; readout-corrected",
        },
        "t1": {"fit": t1_fit, "calibration_reference_us": 303.33},
        "t2_ramsey": {"fit": t2_fit, "calibration_reference_us": 339.99},
        "tphi_derived_us": tphi_zero_temperature_us,
        "t2_damped_oscillation_fit": damped_fit,
        "rows": rows,
        "limitations": [
            "The simple fits use no thermal-equilibrium offset.",
            "Finite native gate duration and gate infidelity remain in the amplitudes.",
            "T2 Ramsey fitting uses absolute contrast and does not fit oscillation frequency.",
            "This is not a formal holdout or a model refit.",
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = analyze(args.raw, args.output)
    print(json.dumps({
        "readout": report["readout_calibration"],
        "x_preparation": report["x_preparation"],
        "t1": report["t1"],
        "t2_ramsey": report["t2_ramsey"],
        "t2_damped_oscillation_fit": report["t2_damped_oscillation_fit"],
        "tphi_derived_us": report["tphi_derived_us"],
    }, indent=2, ensure_ascii=True))
    print(f"WROTE: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
