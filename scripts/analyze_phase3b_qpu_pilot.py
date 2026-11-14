"""Compare the Phase 3B QPU pilot with the frozen gate-aware model.

This is an analysis-only tool. It does not refit the model, change the API,
or submit another QPU job. The calibration values are explicit command-line
inputs so the comparison remains reproducible and auditable.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.results import EnvironmentConfig, SimulationConfig
from core.simulator import run_simulation

DEFAULT_RAW = ROOT / "validation_hardware" / "raw" / (
    "phase3b_pilot_d9njjeoqs0bc73e3gss0.json"
)
DEFAULT_OUTPUT = ROOT / "validation_results" / "phase3b_qpu_model_comparison.json"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--t1-us", type=float, default=303.33)
    parser.add_argument("--t2-us", type=float, default=339.99)
    parser.add_argument(
        "--readout-one-p1",
        type=float,
        default=31.0 / 32.0,
        help="Observed P(1) for prepared |1> used for optional T1 correction.",
    )
    parser.add_argument("--temperature-mk", type=float, default=15.0)
    parser.add_argument("--qubit-frequency-ghz", type=float, default=5.0)
    parser.add_argument(
        "--gate-duration-us",
        type=float,
        default=0.02,
        help="Logical H/X duration used by the gate-aware model.",
    )
    return parser.parse_args()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("raw result must be a JSON object")
    return value


def _tphi_from_t1_t2(t1_us: float, t2_us: float) -> float:
    inverse = 1.0 / t2_us - 1.0 / (2.0 * t1_us)
    if inverse <= 0.0:
        raise ValueError("T2 must satisfy 1/T2 > 1/(2*T1) for this comparison")
    return 1.0 / inverse


def _probability_from_counts(counts: dict[str, Any], state: str) -> float:
    shots = sum(int(value) for value in counts.values())
    if shots <= 0:
        raise ValueError("raw count record has no shots")
    return int(counts.get(state, 0)) / shots


def _fit_t1(rows: list[dict[str, Any]], readout_one_p1: float) -> dict[str, Any] | None:
    t1_rows = [row for row in rows if row["case_id"] == "t1_delay_pilot"]
    if len(t1_rows) < 2 or not 0.0 < readout_one_p1 <= 1.0:
        return None
    points: list[tuple[float, float]] = []
    for row in t1_rows:
        corrected = min(1.0, max(1e-12, row["measured_p1"] / readout_one_p1))
        points.append((float(row["delay_us"]), math.log(corrected)))
    mean_t = sum(point[0] for point in points) / len(points)
    mean_log_p = sum(point[1] for point in points) / len(points)
    denominator = sum((t - mean_t) ** 2 for t, _ in points)
    if denominator <= 0.0:
        return None
    slope = sum(
        (t - mean_t) * (log_p - mean_log_p) for t, log_p in points
    ) / denominator
    if slope >= 0.0:
        return {"status": "non_decay", "points": len(points)}
    intercept = mean_log_p - slope * mean_t
    predicted = [intercept + slope * t for t, _ in points]
    residual_sum = sum((observed - fitted) ** 2 for (_, observed), fitted in zip(points, predicted))
    return {
        "status": "fit",
        "points": len(points),
        "readout_one_p1_correction": readout_one_p1,
        "t1_fit_us": -1.0 / slope,
        "initial_population_fit": math.exp(intercept),
        "log_population_rmse": math.sqrt(residual_sum / len(points)),
        "method": "linear least squares on log(corrected P1); no thermal offset fit",
    }


def _model_probability(
    *,
    gate_type: str | None,
    delay_us: float,
    gate_duration_us: float,
    environment: EnvironmentConfig,
) -> tuple[float, dict[str, Any]]:
    columns: list[GateColumn] = []
    if gate_type is not None:
        columns.append(
            GateColumn(
                step=0,
                gates=[
                    GateOperation(
                        type=gate_type,
                        targets=[0],
                        params={"duration_us": gate_duration_us},
                    )
                ],
            )
        )
    total_duration = max(gate_duration_us if gate_type else 0.0, delay_us)
    config = SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=1,
            initial_states=["0"],
            columns=columns,
        ),
        environment=environment,
        duration_us=max(total_duration, 1e-9),
        time_steps=2,
        fidelity_threshold=0.0,
        evolution_method="explicit_cptp",
        measurement_shots=1,
    )
    result = run_simulation(config)
    probabilities = result.output_probabilities
    probability_one = float(probabilities.get("1", 0.0))
    return probability_one, {
        "duration_us": total_duration,
        "evolution_method": result.diagnostics.get("evolution_method_resolved"),
        "rates": {
            key: result.derived_parameters.get(key)
            for key in (
                "t1_effective_us",
                "t2_effective_us",
                "tphi_effective_us",
                "gamma_down_per_us",
                "gamma_up_per_us",
                "gamma_phi_per_us",
            )
        },
    }


def _case_model_inputs(case_id: str) -> tuple[str | None, str]:
    if case_id == "t1_delay_pilot":
        return "X", "1"
    if case_id == "single_qubit_gate_idle_pilot":
        return "H", "1"
    if case_id == "readout_zero_calibration":
        return None, "0"
    if case_id == "readout_one_calibration":
        return "X", "1"
    raise ValueError(f"unsupported case: {case_id}")


def analyze(args: argparse.Namespace) -> dict[str, Any]:
    raw = _read_json(args.raw)
    if args.t1_us <= 0.0 or args.t2_us <= 0.0 or args.gate_duration_us <= 0.0:
        raise ValueError("T1, T2, and gate duration must be positive")
    tphi_us = _tphi_from_t1_t2(args.t1_us, args.t2_us)
    environment = EnvironmentConfig(
        temperature_mk=args.temperature_mk,
        qubit_frequency_ghz=args.qubit_frequency_ghz,
        t1_zero_temperature_us=args.t1_us,
        tphi_us=tphi_us,
    )

    rows: list[dict[str, Any]] = []
    for record in raw.get("raw_counts", []):
        case_id = str(record["case_id"])
        gate_type, expected_state = _case_model_inputs(case_id)
        delay_us = float(record["delay_dt"]) * float(raw["native_dt_seconds"]) * 1e6
        if case_id.startswith("readout_"):
            # The gate-aware core intentionally does not model assignment error.
            model_probability = 1.0 if expected_state == "1" else 0.0
            model_meta = {"not_simulated": True, "reason": "readout calibration"}
        else:
            model_probability, model_meta = _model_probability(
                gate_type=gate_type,
                delay_us=delay_us,
                gate_duration_us=args.gate_duration_us,
                environment=environment,
            )
        measured_probability = _probability_from_counts(record["counts"], "1")
        rows.append(
            {
                "case_id": case_id,
                "delay_dt": int(record["delay_dt"]),
                "delay_us": delay_us,
                "shots": int(record["shots"]),
                "measured_p1": measured_probability,
                "model_p1": model_probability,
                "absolute_difference": abs(measured_probability - model_probability),
                "counts": record["counts"],
                "model_details": model_meta,
            }
        )

    grouped: dict[str, dict[str, float]] = {}
    for row in rows:
        group = grouped.setdefault(row["case_id"], {"max_absolute_difference": 0.0})
        group["max_absolute_difference"] = max(
            group["max_absolute_difference"], row["absolute_difference"]
        )

    return {
        "analysis_id": "phase3b_qpu_model_comparison_v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_raw_result": str(args.raw.resolve().relative_to(ROOT)),
        "job_id": raw.get("job_id"),
        "backend": raw.get("backend_properties"),
        "comparison_parameters": {
            "t1_us": args.t1_us,
            "t2_us": args.t2_us,
            "tphi_derived_us": tphi_us,
            "temperature_mk": args.temperature_mk,
            "qubit_frequency_ghz": args.qubit_frequency_ghz,
            "logical_gate_duration_us": args.gate_duration_us,
            "model_refit": False,
        },
        "rows": rows,
        "group_summary": grouped,
        "t1_fit": _fit_t1(rows, args.readout_one_p1),
        "limitations": [
            "Readout assignment error is not part of the gate-aware core model.",
            "The logical H/X duration is an explicit comparison assumption; the QPU used native SX/RZ decomposition.",
            "Thirty-two shots per point are exploratory and do not support a formal pass/fail decision.",
            "This analysis does not refit the model and does not make the dataset formal holdout eligible.",
        ],
    }


def main() -> int:
    args = _parse_args()
    report = analyze(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["group_summary"], indent=2, ensure_ascii=True))
    print(f"WROTE: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
