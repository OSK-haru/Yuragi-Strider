"""Compare native SX/RZ-inspired CPTP evolution with the formal QPU result."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.gates import (
    apply_unitary_to_density,
    effective_hamiltonian_from_unitary,
    initial_density_matrix,
    multi_qubit_environment_collapse_operators,
    prepare_collapse_operators,
    rx_matrix,
    rz_matrix,
    scale,
    trace,
    Z,
)
from core.gate_aware_cptp import GateAwareCPTPEvolver
from core.results import EnvironmentConfig
from core.physical_environment import compute_environment_rates
from core.simulator import zero_hamiltonian

DEFAULT_RAW = ROOT / "validation_hardware" / "raw" / (
    "phase3b_formal_audit_d9nlh5ssfqic73ar6f30.json"
)
DEFAULT_CALIBRATION = ROOT / "validation_results" / "phase3b_runtime_calibration.json"
DEFAULT_OUTPUT = ROOT / "validation_results" / "phase3b_native_model_comparison.json"


def _p1(state: Any) -> float:
    return float(state[1][1].real)


def _apply_zero(state: Any, unitary: Any) -> Any:
    return apply_unitary_to_density(state, unitary)


def _evolve_gate(state: Any, unitary: Any, duration_us: float, evolver: GateAwareCPTPEvolver, collapse_ops: Any) -> Any:
    if duration_us <= 0.0:
        return _apply_zero(state, unitary)
    hamiltonian = effective_hamiltonian_from_unitary(unitary, duration_us)
    return evolver.evolve(state, hamiltonian, collapse_ops, duration_us)


def _h_native(state: Any, duration_us: float, evolver: GateAwareCPTPEvolver, collapse_ops: Any) -> Any:
    rz = rz_matrix(math.pi / 2.0)
    sx = rx_matrix(math.pi / 2.0)
    state = _apply_zero(state, rz)
    state = _evolve_gate(state, sx, duration_us, evolver, collapse_ops)
    return _apply_zero(state, rz)


def _x_native(state: Any, duration_us: float, evolver: GateAwareCPTPEvolver, collapse_ops: Any) -> Any:
    return _evolve_gate(state, rx_matrix(math.pi), duration_us, evolver, collapse_ops)


def _idle(
    state: Any,
    duration_us: float,
    evolver: GateAwareCPTPEvolver,
    collapse_ops: Any,
    detuning_cycles_per_us: float = 0.0,
) -> Any:
    if detuning_cycles_per_us == 0.0:
        hamiltonian = zero_hamiltonian(2)
    else:
        angular_frequency = 2.0 * math.pi * detuning_cycles_per_us
        hamiltonian = scale(angular_frequency / 2.0, Z)
    return evolver.evolve(state, hamiltonian, collapse_ops, duration_us)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    raw = json.loads(args.raw.read_text(encoding="utf-8"))
    calibration = json.loads(args.calibration.read_text(encoding="utf-8"))
    qubit = calibration["qubit_properties"]
    t1_us = float(qubit["T1"]["value"])
    t2_us = float(qubit["T2"]["value"])
    tphi_inverse = 1.0 / t2_us - 1.0 / (2.0 * t1_us)
    tphi_us = 1.0 / tphi_inverse if tphi_inverse > 0.0 else t2_us
    sx_length_us = next(
        item["parameters"]["gate_length"]["value"] * 1e-3
        for item in calibration["single_qubit_gate_properties"]
        if item["gate"] == "sx"
    )
    environment = EnvironmentConfig(
        temperature_mk=15.0,
        qubit_frequency_ghz=5.0,
        t1_zero_temperature_us=t1_us,
        tphi_us=tphi_us,
    )
    rates = compute_environment_rates(environment)
    collapse_ops = prepare_collapse_operators(
        multi_qubit_environment_collapse_operators(1, rates)
    )

    def simulate(
        kind: str,
        delay_us: float,
        detuning_cycles_per_us: float = 0.0,
    ) -> float:
        state = initial_density_matrix(["0"])
        evolver = GateAwareCPTPEvolver()
        if kind == "t1":
            state = _x_native(state, sx_length_us, evolver, collapse_ops)
            state = _idle(state, delay_us, evolver, collapse_ops)
        else:
            state = _h_native(state, sx_length_us, evolver, collapse_ops)
            state = _idle(
                state,
                delay_us,
                evolver,
                collapse_ops,
                detuning_cycles_per_us,
            )
            state = _h_native(state, sx_length_us, evolver, collapse_ops)
        return _p1(state)

    readout_zero = next(item for item in raw["raw_counts"] if item["kind"] == "readout_zero")
    readout_one = next(item for item in raw["raw_counts"] if item["kind"] == "readout_one")
    p10 = int(readout_zero["counts"].get("1", 0)) / readout_zero["shots"]
    p11 = int(readout_one["counts"].get("1", 0)) / readout_one["shots"]
    span = p11 - p10
    rows = []
    qpu_ramsey_points: list[tuple[float, float]] = []
    for record in raw["raw_counts"]:
        if record["kind"] not in {"t1", "t2_ramsey"}:
            continue
        observed = int(record["counts"].get("1", 0)) / record["shots"]
        corrected = (observed - p10) / span
        kind = "t1" if record["kind"] == "t1" else "t2"
        prediction = simulate(kind, float(record["delay_us"]))
        if kind == "t2":
            qpu_ramsey_points.append((float(record["delay_us"]), corrected))
        rows.append({
            "kind": kind,
            "delay_us": record["delay_us"],
            "qpu_corrected_p1": corrected,
            "native_model_p1": prediction,
            "absolute_difference": abs(corrected - prediction),
        })
    summaries = {}
    for kind in {row["kind"] for row in rows}:
        selected = [row for row in rows if row["kind"] == kind]
        summaries[kind] = {
            "points": len(selected),
            "max_absolute_difference": max(row["absolute_difference"] for row in selected),
            "mean_absolute_difference": sum(row["absolute_difference"] for row in selected) / len(selected),
        }
    detuning_scan = []
    for index in range(101):
        detuning = 0.01 * index / 100.0
        residuals = []
        for delay_us, measured_p1 in qpu_ramsey_points:
            predicted = simulate("t2", delay_us, detuning)
            residuals.append((predicted - measured_p1) ** 2)
        detuning_scan.append({
            "detuning_cycles_per_us": detuning,
            # cycles/us is numerically identical to MHz, so the kHz value is
            # the cycles/us value scaled by 1000.
            "detuning_khz": detuning * 1000.0,
            "rmse": math.sqrt(sum(residuals) / len(residuals)),
        })
    detuning_scan.sort(key=lambda item: item["rmse"])
    report = {
        "analysis_id": "phase3b_native_model_comparison_v1",
        "source_job_id": raw["job_id"],
        "calibration_source": str(args.calibration.resolve().relative_to(ROOT)),
        "physical_qubit": calibration["physical_qubit"],
        "native_gate_duration_us": {"sx": sx_length_us, "rz": 0.0},
        "environment_rates": {
            "t1_effective_us": rates.t1_effective_us,
            "t2_effective_us": rates.t2_effective_us,
            "tphi_effective_us": rates.tphi_effective_us,
            "gamma_down_per_us": rates.gamma_down_per_us,
            "gamma_up_per_us": rates.gamma_up_per_us,
            "gamma_phi_per_us": rates.gamma_phi_per_us,
        },
        "summaries": summaries,
        "detuning_scan": {
            "zero_detuning_rmse": next(
                item["rmse"] for item in detuning_scan
                if item["detuning_cycles_per_us"] == 0.0
            ),
            "best": detuning_scan[0],
            "top_candidates": detuning_scan[:5],
            "interpretation": "local detuning-only diagnostic; not a production model change",
        },
        "rows": rows,
        "limitations": [
            "SX is represented by RX(pi/2) up to global phase; pulse shape is not simulated.",
            "RZ is treated as virtual zero-duration evolution.",
            "Readout is corrected but native gate error channels are not independently inserted.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    print(json.dumps(summaries, indent=2, ensure_ascii=True))
    print(f"WROTE: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
