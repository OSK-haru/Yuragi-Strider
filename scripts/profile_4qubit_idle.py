"""Profile idle-only and light gate-aware dense simulations."""

from __future__ import annotations

import sys
import time
from argparse import ArgumentParser
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.main import SimulateRequest, simulate
from core.internal_profiling import enable_internal_profiling


def main() -> None:
    parser = ArgumentParser(description="Profile Python dense Lindblad simulations.")
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="Number of runs per benchmark case.",
    )
    args = parser.parse_args()
    repeat = max(1, int(args.repeat))

    cases = [
        ("3q empty default", _empty_request(3, 2.0, 101)),
        ("4q empty light", _empty_request(4, 0.5, 11)),
        ("4q empty default", _empty_request(4, 2.0, 101)),
        ("4q H(q0) light", _h_request(4, 0.5, 11)),
    ]

    headers = [
        "case",
        "qubits",
        "gates",
        "duration_us",
        "time_steps",
        "engine",
        "total_min_ms",
        "total_median_ms",
        "total_max_ms",
        "evolution_ms",
        "idle_evolution_ms",
        "rk4_count",
        "rk4_total_ms",
        "rk4_avg_ms",
        "rhs_calls",
        "rhs_total_ms",
        "rhs_avg_ms",
        "hamiltonian_ms",
        "dissipator_ms",
        "dissipator_iterations",
        "matmul_calls",
        "matmul_ms",
        "matmul_avg_ms",
        "numpy_matmul_calls",
        "numpy_matmul_ms",
        "python_matmul_calls",
        "python_matmul_ms",
        "conversion_count",
        "conversion_ms",
        "zero_h_skips",
        "adjoint_calls",
        "adjoint_ms",
        "add_scale_calls",
        "add_scale_ms",
        "l_dagger_build_count",
        "l_dagger_build_ms",
        "l_dagger_l_build_count",
        "l_dagger_l_build_ms",
        "collapse_ops",
        "idle_only",
    ]
    print("\t".join(headers))
    for case_name, request in cases:
        response = None
        totals: list[float] = []
        for _ in range(repeat):
            start = time.perf_counter()
            with enable_internal_profiling():
                response = simulate(request)
            totals.append((time.perf_counter() - start) * 1000.0)
        if response is None:
            raise RuntimeError("benchmark did not run")
        diagnostics = response["diagnostics"]
        print("\t".join([
            case_name,
            str(response["circuit"]["qubit_count"]),
            str(_gate_count(response)),
            _format_number(response["parameters"]["duration_us"]),
            str(response["parameters"]["time_steps"]),
            str(diagnostics.get("core_dense_execution_engine", "")),
            _format_ms(min(totals)),
            _format_ms(median(totals)),
            _format_ms(max(totals)),
            _format_ms(diagnostics.get("core_total_evolution_ms")),
            _format_ms(diagnostics.get("core_idle_evolution_ms")),
            str(int(diagnostics.get("core_profile_rk4_step_count", 0))),
            _format_ms(diagnostics.get("core_profile_rk4_total_ms")),
            _format_ms(diagnostics.get("core_profile_rk4_average_ms")),
            str(int(diagnostics.get("core_profile_rhs_call_count", 0))),
            _format_ms(diagnostics.get("core_profile_rhs_total_ms")),
            _format_ms(diagnostics.get("core_profile_rhs_average_ms")),
            _format_ms(diagnostics.get("core_profile_hamiltonian_term_ms")),
            _format_ms(diagnostics.get("core_profile_dissipator_total_ms")),
            str(int(diagnostics.get("core_profile_dissipator_operator_iterations", 0))),
            str(int(diagnostics.get("core_profile_matmul_call_count", 0))),
            _format_ms(diagnostics.get("core_profile_matmul_total_ms")),
            _format_ms(diagnostics.get("core_profile_matmul_average_ms")),
            str(int(diagnostics.get("core_profile_numpy_matmul_call_count", 0))),
            _format_ms(diagnostics.get("core_profile_numpy_matmul_total_ms")),
            str(int(diagnostics.get("core_profile_python_matmul_call_count", 0))),
            _format_ms(diagnostics.get("core_profile_python_matmul_total_ms")),
            str(int(diagnostics.get("core_profile_conversion_count", 0))),
            _format_ms(diagnostics.get("core_profile_conversion_total_ms")),
            str(int(diagnostics.get("core_profile_zero_hamiltonian_skip_count", 0))),
            str(int(diagnostics.get("core_profile_adjoint_call_count", 0))),
            _format_ms(diagnostics.get("core_profile_adjoint_total_ms")),
            str(int(diagnostics.get("core_profile_matrix_add_scale_call_count", 0))),
            _format_ms(diagnostics.get("core_profile_matrix_add_scale_total_ms")),
            str(int(diagnostics.get("core_profile_collapse_adjoint_build_count", 0))),
            _format_ms(diagnostics.get("core_profile_collapse_adjoint_build_ms")),
            str(int(diagnostics.get("core_profile_ldagger_l_build_count", 0))),
            _format_ms(diagnostics.get("core_profile_ldagger_l_build_ms")),
            str(int(diagnostics.get("core_collapse_operator_count", 0))),
            str(bool(diagnostics.get("core_idle_only", False))).lower(),
        ]))


def _base_request() -> dict[str, object]:
    return {
        "simulation_backend": "python_dense",
        "input_mode": "physical",
        "gate_duration_defaults": {
            "H": 0.02,
            "X": 0.02,
            "Z": 0.0,
            "CNOT": 0.2,
            "MEASURE": 0.0,
        },
        "parameters": {
            "temperature_mk": 15.0,
            "qubit_frequency_ghz": 5.0,
            "t1_zero_temperature_us": 39.810717055349734,
            "tphi_zero_temperature_us": 33.20178309308936,
            "readout_resonator_frequency_ghz": 7.0,
            "readout_resonator_kappa_mhz": 1.0,
            "readout_resonator_chi_mhz": 0.5,
            "duration_us": 2.0,
            "time_steps": 101,
            "fidelity_threshold": 0.9,
        },
    }


def _bell_request() -> SimulateRequest:
    payload = _base_request()
    payload["circuit_preset"] = "bell"
    return SimulateRequest(**payload)


def _empty_request(qubits: int, duration_us: float, time_steps: int) -> SimulateRequest:
    payload = _base_request()
    payload["circuit_config"] = {
        "logical_qubits": qubits,
        "initial_states": [0] * qubits,
        "columns": [],
    }
    payload["parameters"] = {
        **payload["parameters"],
        "duration_us": duration_us,
        "time_steps": time_steps,
    }
    return SimulateRequest(**payload)


def _h_request(qubits: int, duration_us: float, time_steps: int) -> SimulateRequest:
    payload = _base_request()
    payload["circuit_config"] = {
        "logical_qubits": qubits,
        "initial_states": [0] * qubits,
        "columns": [
            {
                "step": 0,
                "gates": [
                    {
                        "type": "H",
                        "targets": [0],
                        "controls": [],
                        "params": {},
                    }
                ],
            }
        ],
    }
    payload["parameters"] = {
        **payload["parameters"],
        "duration_us": duration_us,
        "time_steps": time_steps,
    }
    return SimulateRequest(**payload)


def _gate_count(response: dict[str, object]) -> int:
    circuit = response["circuit"]
    return sum(len(column["gates"]) for column in circuit["columns"])


def _format_ms(value: object) -> str:
    number = float(value) if value is not None else 0.0
    return f"{number:.3f}"


def _format_number(value: object) -> str:
    number = float(value) if value is not None else 0.0
    if number.is_integer():
        return str(int(number))
    return f"{number:.3f}"


if __name__ == "__main__":
    main()
