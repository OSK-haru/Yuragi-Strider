"""Compare alternative T1 and Ramsey decay models on the saved QPU job."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
from scipy.optimize import minimize


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RAW = ROOT / "validation_hardware" / "raw" / (
    "phase3b_formal_audit_d9nlh5ssfqic73ar6f30.json"
)
DEFAULT_CALIBRATION = ROOT / "validation_results" / "phase3b_runtime_calibration.json"
DEFAULT_OUTPUT = ROOT / "validation_results" / "phase3b_decay_model_diagnostics.json"
DEFAULT_FIGURE = ROOT / "validation_results" / "phase3b_decay_model_diagnostics.png"
DEFAULT_RAMSEY_SENSITIVITY = (
    ROOT / "validation_hardware" / "raw" / "phase3b_ramsey_dense_followup_d9nl3d6ij12s73fu19kg.json",
    ROOT / "validation_hardware" / "raw" / "phase3b_ramsey_dense_followup_d9ntoj460llc73cagtgg.json",
)


@dataclass(frozen=True)
class Candidate:
    name: str
    parameter_names: tuple[str, ...]
    bounds: tuple[tuple[float, float], ...]
    starts: tuple[tuple[float, ...], ...]
    true_probability: Callable[[np.ndarray, np.ndarray], np.ndarray]


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _observed_counts(record: dict[str, Any]) -> tuple[int, int]:
    shots = int(record["shots"])
    ones = int(record["counts"].get("1", 0))
    total = sum(int(value) for value in record["counts"].values())
    if shots <= 0 or total != shots or not 0 <= ones <= shots:
        raise ValueError("invalid shot counts")
    return ones, shots


def _readout_calibration(records: list[dict[str, Any]]) -> tuple[float, float]:
    zero = [record for record in records if record.get("kind") == "readout_zero"]
    one = [record for record in records if record.get("kind") == "readout_one"]
    if len(zero) != 1 or len(one) != 1:
        raise ValueError("exactly one readout-zero and readout-one record are required")
    zero_ones, zero_shots = _observed_counts(zero[0])
    one_ones, one_shots = _observed_counts(one[0])
    p10 = zero_ones / zero_shots
    p01 = 1.0 - one_ones / one_shots
    if p10 + p01 >= 1.0:
        raise ValueError("readout assignment span must be positive")
    return p10, p01


def _series(
    records: list[dict[str, Any]], kind: str
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    selected = sorted(
        (record for record in records if record.get("kind") == kind),
        key=lambda record: float(record["delay_us"]),
    )
    if len(selected) != 21:
        raise ValueError(f"expected 21 {kind} records, found {len(selected)}")
    delays = np.asarray([float(record["delay_us"]) for record in selected])
    counts_and_shots = [_observed_counts(record) for record in selected]
    ones = np.asarray([item[0] for item in counts_and_shots], dtype=float)
    shots = np.asarray([item[1] for item in counts_and_shots], dtype=float)
    if len(np.unique(delays)) != len(delays):
        raise ValueError(f"duplicate delays in {kind} series")
    return delays, ones, shots


def _observed_probability(true_probability: np.ndarray, p10: float, p01: float) -> np.ndarray:
    return p10 + (1.0 - p10 - p01) * true_probability


def _binomial_nll(predicted: np.ndarray, ones: np.ndarray, shots: np.ndarray) -> float:
    probability = np.clip(predicted, 1e-12, 1.0 - 1e-12)
    return float(
        -np.sum(ones * np.log(probability) + (shots - ones) * np.log1p(-probability))
    )


def _fit(
    candidate: Candidate,
    delays: np.ndarray,
    ones: np.ndarray,
    shots: np.ndarray,
    p10: float,
    p01: float,
) -> dict[str, Any]:
    def objective(parameters: np.ndarray) -> float:
        true_probability = candidate.true_probability(delays, parameters)
        if np.any(~np.isfinite(true_probability)) or np.any(true_probability < 0.0) or np.any(true_probability > 1.0):
            return 1e100
        return _binomial_nll(
            _observed_probability(true_probability, p10, p01), ones, shots
        )

    solutions = []
    for start in candidate.starts:
        result = minimize(
            objective,
            np.asarray(start, dtype=float),
            method="L-BFGS-B",
            bounds=candidate.bounds,
            options={"maxiter": 20_000, "ftol": 1e-12},
        )
        if result.success and math.isfinite(float(result.fun)):
            solutions.append(result)
    if not solutions:
        raise RuntimeError(f"all fits failed for {candidate.name}")
    best = min(solutions, key=lambda result: float(result.fun))
    parameters = np.asarray(best.x, dtype=float)
    true_probability = candidate.true_probability(delays, parameters)
    predicted = _observed_probability(true_probability, p10, p01)
    observed = ones / shots
    residual = observed - predicted
    n = len(delays)
    k = len(candidate.parameter_names)
    nll = float(best.fun)
    aic = 2.0 * k + 2.0 * nll
    aicc = aic + (2.0 * k * (k + 1.0)) / (n - k - 1.0)
    bic = k * math.log(n) + 2.0 * nll
    parameter_values = {
        name: float(value) for name, value in zip(candidate.parameter_names, parameters)
    }
    if candidate.name.startswith("t1_biexponential"):
        parameter_values["t_slow_us"] = (
            parameter_values["t_fast_us"] + parameter_values.pop("t_delta_us")
        )
    return {
        "model": candidate.name,
        "parameters": parameter_values,
        "negative_log_likelihood_without_constant": nll,
        "aic": aic,
        "aicc": aicc,
        "bic": bic,
        "mean_absolute_probability_error": float(np.mean(np.abs(residual))),
        "root_mean_square_probability_error": float(np.sqrt(np.mean(residual**2))),
        "optimizer_successful_starts": len(solutions),
        "optimizer_attempted_starts": len(candidate.starts),
    }


def _leave_one_out(
    candidate: Candidate,
    delays: np.ndarray,
    ones: np.ndarray,
    shots: np.ndarray,
    p10: float,
    p01: float,
) -> dict[str, float | int]:
    absolute_errors = []
    failed = 0
    for index in range(len(delays)):
        keep = np.arange(len(delays)) != index
        try:
            fitted = _fit(candidate, delays[keep], ones[keep], shots[keep], p10, p01)
            parameters = []
            stored = dict(fitted["parameters"])
            for name in candidate.parameter_names:
                if name == "t_delta_us":
                    parameters.append(stored["t_slow_us"] - stored["t_fast_us"])
                else:
                    parameters.append(stored[name])
            prediction = candidate.true_probability(
                np.asarray([delays[index]]), np.asarray(parameters)
            )
            observed_prediction = _observed_probability(prediction, p10, p01)[0]
            absolute_errors.append(abs(ones[index] / shots[index] - observed_prediction))
        except (RuntimeError, ValueError):
            failed += 1
    return {
        "successful_folds": len(absolute_errors),
        "failed_folds": failed,
        "mean_absolute_probability_error": (
            float(np.mean(absolute_errors)) if absolute_errors else math.nan
        ),
        "max_absolute_probability_error": (
            max(absolute_errors) if absolute_errors else math.nan
        ),
    }


def _t1_candidates() -> tuple[Candidate, ...]:
    single = Candidate(
        name="t1_single_exponential",
        parameter_names=("amplitude", "t1_us"),
        bounds=((0.0, 1.0), (1.0, 5000.0)),
        starts=tuple((amplitude, lifetime) for amplitude in (0.9, 0.98, 1.0) for lifetime in (250.0, 400.0, 800.0)),
        true_probability=lambda t, p: p[0] * np.exp(-t / p[1]),
    )
    offset = Candidate(
        name="t1_single_exponential_offset",
        parameter_names=("initial_fraction", "floor", "t1_us"),
        bounds=((0.0, 1.0), (0.0, 0.5), (1.0, 5000.0)),
        starts=tuple((amplitude, floor, lifetime) for amplitude in (0.9, 1.0) for floor in (0.0, 0.02, 0.1) for lifetime in (250.0, 400.0, 800.0)),
        true_probability=lambda t, p: p[1] + (1.0 - p[1]) * p[0] * np.exp(-t / p[2]),
    )
    biexponential = Candidate(
        name="t1_biexponential_offset",
        parameter_names=("initial_fraction", "floor", "fast_weight", "t_fast_us", "t_delta_us"),
        bounds=((0.0, 1.0), (0.0, 0.5), (0.0, 1.0), (1.0, 5000.0), (0.01, 5000.0)),
        starts=tuple(
            (0.98, floor, weight, fast, slow - fast)
            for floor in (0.0, 0.05)
            for weight in (0.25, 0.5, 0.75)
            for fast, slow in ((100.0, 400.0), (250.0, 800.0), (350.0, 1500.0))
        ),
        true_probability=lambda t, p: p[1]
        + (1.0 - p[1])
        * p[0]
        * (p[2] * np.exp(-t / p[3]) + (1.0 - p[2]) * np.exp(-t / (p[3] + p[4]))),
    )
    biexponential_no_offset = Candidate(
        name="t1_biexponential_no_offset",
        parameter_names=("amplitude", "fast_weight", "t_fast_us", "t_delta_us"),
        bounds=((0.0, 1.0), (0.0, 1.0), (1.0, 5000.0), (0.01, 20_000.0)),
        starts=tuple(
            (0.98, weight, fast, slow - fast)
            for weight in (0.25, 0.5, 0.75)
            for fast, slow in (
                (100.0, 500.0),
                (250.0, 1000.0),
                (300.0, 5000.0),
                (350.0, 15_000.0),
            )
        ),
        true_probability=lambda t, p: p[0]
        * (
            p[1] * np.exp(-t / p[2])
            + (1.0 - p[1]) * np.exp(-t / (p[2] + p[3]))
        ),
    )
    return single, offset, biexponential, biexponential_no_offset


def _ramsey_candidates() -> tuple[Candidate, ...]:
    starts = tuple(
        (amplitude, lifetime, frequency, phase)
        for amplitude in (0.9, 1.0)
        for lifetime in (200.0, 400.0, 800.0)
        for frequency in (0.004, 0.0065, 0.009, 0.015, 0.022)
        for phase in (-0.25, 0.0, 0.25)
    )
    bounds = ((0.0, 1.0), (1.0, 5000.0), (0.0, 0.025), (-math.pi, math.pi))

    def probability(power: float) -> Callable[[np.ndarray, np.ndarray], np.ndarray]:
        def evaluate(t: np.ndarray, p: np.ndarray) -> np.ndarray:
            envelope = np.exp(-np.power(t / p[1], power))
            contrast = p[0] * envelope * np.cos(2.0 * math.pi * p[2] * t + p[3])
            return 0.5 * (1.0 - contrast)

        return evaluate

    return (
        Candidate(
            name="ramsey_exponential_envelope",
            parameter_names=("amplitude", "t2_us", "frequency_cycles_per_us", "phase_rad"),
            bounds=bounds,
            starts=starts,
            true_probability=probability(1.0),
        ),
        Candidate(
            name="ramsey_gaussian_envelope",
            parameter_names=("amplitude", "t2_star_us", "frequency_cycles_per_us", "phase_rad"),
            bounds=bounds,
            starts=starts,
            true_probability=probability(2.0),
        ),
    )


def _rank(fits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(fits, key=lambda item: float(item["aicc"]))
    minimum = float(ordered[0]["aicc"])
    weights = np.exp(-0.5 * np.asarray([float(item["aicc"]) - minimum for item in ordered]))
    weights /= np.sum(weights)
    for item, weight in zip(ordered, weights):
        item["delta_aicc"] = float(item["aicc"] - minimum)
        item["aicc_weight"] = float(weight)
    return ordered


def _parameter_array(candidate: Candidate, stored: dict[str, Any]) -> np.ndarray:
    values = []
    for name in candidate.parameter_names:
        if name == "t_delta_us":
            values.append(float(stored["t_slow_us"]) - float(stored["t_fast_us"]))
        else:
            values.append(float(stored[name]))
    return np.asarray(values)


def _plot_report(
    report: dict[str, Any], raw_path: Path, figure_path: Path
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    raw = _load_object(raw_path)
    records = raw["raw_counts"]
    p10 = float(report["readout_calibration"]["p10"])
    p01 = float(report["readout_calibration"]["p01"])
    dense_time = np.linspace(0.0, 400.0, 401)

    t1_t, t1_ones, t1_shots = _series(records, "t1")
    ramsey_t, ramsey_ones, ramsey_shots = _series(records, "t2_ramsey")
    t1_observed = t1_ones / t1_shots
    ramsey_observed = ramsey_ones / ramsey_shots
    t1_error = 1.96 * np.sqrt(t1_observed * (1.0 - t1_observed) / t1_shots)
    ramsey_error = 1.96 * np.sqrt(
        ramsey_observed * (1.0 - ramsey_observed) / ramsey_shots
    )

    blue = "#2563EB"
    orange = "#D97706"
    charcoal = "#262626"
    grey = "#737373"
    grid = "#E5E7EB"

    fig, axes = plt.subplots(1, 2, figsize=(12.5, 5.2), constrained_layout=True)
    fig.suptitle(
        "Saved ibm_kingston decay-model comparison\n"
        "physical qubit 150; 21 points per series; 1,024 shots per point",
        fontsize=13,
        color=charcoal,
    )

    axes[0].errorbar(
        t1_t,
        t1_observed,
        yerr=t1_error,
        fmt="o",
        color=charcoal,
        markersize=4,
        capsize=2,
        label="QPU raw P(1), 95% normal interval",
        zorder=4,
    )
    calibrated_t1 = float(
        report["t1"]["fixed_runtime_calibration_reference"]["t1_us"]
    )
    axes[0].plot(
        dense_time,
        _observed_probability(np.exp(-dense_time / calibrated_t1), p10, p01),
        color=grey,
        linestyle=":",
        linewidth=2,
        label=f"Fixed calibration T1={calibrated_t1:.1f} μs",
    )
    t1_candidates = {candidate.name: candidate for candidate in _t1_candidates()}
    t1_fits = {
        fit["model"]: fit for fit in report["t1"]["candidate_ranking_by_aicc"]
    }
    for name, color, linestyle, label in (
        ("t1_single_exponential_offset", blue, "-", "Exponential + floor"),
        ("t1_biexponential_no_offset", orange, "--", "Two exponentials, no floor"),
    ):
        candidate = t1_candidates[name]
        parameters = _parameter_array(candidate, t1_fits[name]["parameters"])
        prediction = _observed_probability(
            candidate.true_probability(dense_time, parameters), p10, p01
        )
        axes[0].plot(
            dense_time,
            prediction,
            color=color,
            linestyle=linestyle,
            linewidth=2,
            label=label,
        )
    axes[0].set_title("T1 population decay", color=charcoal)
    axes[0].set_xlabel("Delay [μs]")
    axes[0].set_ylabel("Observed P(1)")

    axes[1].errorbar(
        ramsey_t,
        ramsey_observed,
        yerr=ramsey_error,
        fmt="o",
        color=charcoal,
        markersize=4,
        capsize=2,
        label="QPU raw P(1), 95% normal interval",
        zorder=4,
    )
    ramsey_candidates = {
        candidate.name: candidate for candidate in _ramsey_candidates()
    }
    ramsey_fits = {
        fit["model"]: fit
        for fit in report["ramsey"]["candidate_ranking_by_aicc"]
    }
    for name, color, linestyle, label in (
        ("ramsey_exponential_envelope", blue, "-", "Exponential envelope"),
        ("ramsey_gaussian_envelope", orange, "--", "Gaussian envelope"),
    ):
        candidate = ramsey_candidates[name]
        parameters = _parameter_array(candidate, ramsey_fits[name]["parameters"])
        prediction = _observed_probability(
            candidate.true_probability(dense_time, parameters), p10, p01
        )
        axes[1].plot(
            dense_time,
            prediction,
            color=color,
            linestyle=linestyle,
            linewidth=2,
            label=label,
        )
    axes[1].set_title("Ramsey probability", color=charcoal)
    axes[1].set_xlabel("Delay [μs]")
    axes[1].set_ylabel("Observed P(1)")

    for axis in axes:
        axis.set_xlim(0.0, 400.0)
        axis.set_ylim(-0.03, 1.03)
        axis.grid(axis="y", color=grid, linewidth=0.8)
        axis.spines["top"].set_visible(False)
        axis.spines["right"].set_visible(False)
        axis.legend(frameon=False, fontsize=8, loc="best")

    figure_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(figure_path, dpi=180, facecolor="white")
    plt.close(fig)


def _ramsey_sensitivity(path: Path) -> dict[str, Any]:
    raw = _load_object(path)
    records = raw.get("raw_counts")
    if not isinstance(records, list):
        raise ValueError("raw_counts must be a list")
    p10, p01 = _readout_calibration(records)
    delays, ones, shots = _series(records, "ramsey")
    fits = [
        _fit(candidate, delays, ones, shots, p10, p01)
        for candidate in _ramsey_candidates()
    ]
    return {
        "job_id": raw["job_id"],
        "raw_result": str(path.resolve().relative_to(ROOT)),
        "raw_result_sha256": _sha256(path),
        "shots_per_point": sorted({int(value) for value in shots}),
        "readout_calibration": {
            "p10": p10,
            "p01": p01,
            "assignment_span": 1.0 - p10 - p01,
        },
        "candidate_ranking_by_aicc": _rank(fits),
    }


def analyze(
    raw_path: Path,
    calibration_path: Path,
    ramsey_sensitivity_paths: tuple[Path, ...] = DEFAULT_RAMSEY_SENSITIVITY,
) -> dict[str, Any]:
    raw = _load_object(raw_path)
    calibration = _load_object(calibration_path)
    if calibration.get("source_job_id") != raw.get("job_id"):
        raise ValueError("calibration and raw data must refer to the same job")
    records = raw.get("raw_counts")
    if not isinstance(records, list):
        raise ValueError("raw_counts must be a list")
    p10, p01 = _readout_calibration(records)
    t1_delays, t1_ones, t1_shots = _series(records, "t1")
    ramsey_delays, ramsey_ones, ramsey_shots = _series(records, "t2_ramsey")

    t1_fits = []
    for candidate in _t1_candidates():
        fit = _fit(candidate, t1_delays, t1_ones, t1_shots, p10, p01)
        fit["leave_one_out"] = _leave_one_out(
            candidate, t1_delays, t1_ones, t1_shots, p10, p01
        )
        t1_fits.append(fit)
    ramsey_fits = []
    for candidate in _ramsey_candidates():
        fit = _fit(candidate, ramsey_delays, ramsey_ones, ramsey_shots, p10, p01)
        fit["leave_one_out"] = _leave_one_out(
            candidate, ramsey_delays, ramsey_ones, ramsey_shots, p10, p01
        )
        ramsey_fits.append(fit)

    calibrated_t1_us = float(calibration["qubit_properties"]["T1"]["value"])
    calibrated_true = np.exp(-t1_delays / calibrated_t1_us)
    calibrated_observed = _observed_probability(calibrated_true, p10, p01)
    t1_observed = t1_ones / t1_shots
    fixed_reference = {
        "model": "t1_fixed_runtime_calibration",
        "t1_us": calibrated_t1_us,
        "mean_absolute_probability_error": float(np.mean(np.abs(t1_observed - calibrated_observed))),
        "root_mean_square_probability_error": float(np.sqrt(np.mean((t1_observed - calibrated_observed) ** 2))),
        "negative_log_likelihood_without_constant": _binomial_nll(calibrated_observed, t1_ones, t1_shots),
    }

    return {
        "analysis_id": "phase3b_decay_model_diagnostics_v1",
        "analysis_type": "retrospective_model_diagnostic_not_formal_holdout",
        "source": {
            "job_id": raw["job_id"],
            "backend": raw["backend_properties"],
            "raw_result": str(raw_path.resolve().relative_to(ROOT)),
            "raw_result_sha256": _sha256(raw_path),
            "runtime_calibration": str(calibration_path.resolve().relative_to(ROOT)),
            "runtime_calibration_sha256": _sha256(calibration_path),
        },
        "data_quality": {
            "t1_points": len(t1_delays),
            "ramsey_points": len(ramsey_delays),
            "shots_per_point": sorted({int(value) for value in np.concatenate((t1_shots, ramsey_shots))}),
            "unique_delays": True,
            "counts_equal_shots": True,
            "calibration_job_matches": True,
        },
        "readout_calibration": {"p10": p10, "p01": p01, "assignment_span": 1.0 - p10 - p01},
        "t1": {
            "fixed_runtime_calibration_reference": fixed_reference,
            "candidate_ranking_by_aicc": _rank(t1_fits),
        },
        "ramsey": {
            "candidate_ranking_by_aicc": _rank(ramsey_fits),
            "repeat_dataset_sensitivity": [
                _ramsey_sensitivity(path) for path in ramsey_sensitivity_paths
            ],
        },
        "limitations": [
            "All models are fitted after the hardware data were observed; this is diagnosis, not prospective validation.",
            "AICc and leave-one-out error can rank these candidates but cannot identify a unique microscopic mechanism.",
            "The biexponential model has five fitted parameters for 21 points and is vulnerable to weak identifiability.",
            "The 20 us Ramsey grid has a 25 kHz Nyquist limit and can leave frequency-envelope tradeoffs.",
            "Only aggregate counts are available, so within-job temporal drift cannot be reconstructed.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--figure", type=Path, default=DEFAULT_FIGURE)
    args = parser.parse_args()
    report = analyze(args.raw, args.calibration)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    _plot_report(report, args.raw, args.figure)
    print(json.dumps({"t1": report["t1"], "ramsey": report["ramsey"]}, indent=2, ensure_ascii=True))
    print(f"WROTE: {args.output}")
    print(f"WROTE: {args.figure}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
