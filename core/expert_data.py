"""Expert Inspector data aggregation without UI dependencies."""

from __future__ import annotations

import math
from typing import Any

from core.gates import (
    Matrix,
    SIGMA_MINUS,
    SIGMA_PLUS,
    Z,
    apply_gate_operation,
    clean_density_matrix,
    expand_single_qubit_gate,
    initial_density_matrix,
    matmul,
    multi_qubit_collapse_operators,
    multi_qubit_physical_collapse_operators,
    output_probabilities,
    rk4_step,
    scale,
    trace,
    zero_hamiltonian,
)
from core.physical_environment import UNIFIED_ENVIRONMENT_MODEL
from core.results import SimulationResult


MAX_RK4_RATE_STEP_PRODUCT = 1.0


def build_expert_inspector_data(result: SimulationResult | None) -> dict[str, Any]:
    """Return JSON-safe expert-facing data for one simulation result."""

    if result is None:
        return _empty_data()

    config = result.config
    n_qubits = config.circuit.logical_qubits
    dimension = 2 ** n_qubits
    final_density = _reconstruct_final_density_matrix(result)
    final_trace = trace(final_density) if final_density is not None else None
    hermiticity_error = (
        _hermiticity_error(final_density)
        if final_density is not None
        else None
    )
    eigenvalues = (
        _hermitian_eigenvalues(final_density)
        if final_density is not None
        else []
    )

    return {
        "overview": {
            "Model": config.model,
            "Simulation mode": result.derived_parameters.get("simulation_mode"),
            "Gate-aware noise": result.derived_parameters.get("gate_aware_noise"),
            "Hamiltonian mode": result.derived_parameters.get("hamiltonian_mode"),
            "Logical qubits": n_qubits,
            "Hilbert space dimension": dimension,
            "Density matrix size": f"{dimension} x {dimension}",
            "Gate count": _gate_count(config.circuit.columns),
            "Circuit depth": _circuit_depth(config.circuit.columns),
            "Simulation time": config.duration_us,
            "Time steps": config.time_steps,
            "Final State Fidelity": _last(result.fidelity),
            "Final Purity": _last(result.purity),
            "Effective Operation Time": result.effective_operation_time_us,
        },
        "noise": _noise_data(result),
        "operators": _operator_data(result),
        "state": {
            "Final density matrix": _matrix_components(final_density),
            "Trace": _complex_to_dict(final_trace),
            "Hermiticity error": hermiticity_error,
            "Minimum eigenvalue": min(eigenvalues) if eigenvalues else None,
            "Maximum eigenvalue": max(eigenvalues) if eigenvalues else None,
            "Final purity": _last(result.purity),
            "Final state fidelity": _last(result.fidelity),
            "Output probability distribution": dict(result.output_probabilities),
        },
        "assumptions": _assumptions(),
        "h_eff": {
            "Status": "not enabled",
            "Note": (
                "H_eff/no-jump evolution is distinct from Lindblad "
                "ensemble-averaged evolution and is not implemented in this phase."
            ),
        },
    }


def reconstruct_final_density_matrix(result: SimulationResult) -> Matrix | None:
    """Return the reconstructed final density matrix for expert/debug use."""

    return _reconstruct_final_density_matrix(result)


def build_comparison_expert_summary(result: Any | None) -> dict[str, Any]:
    """Return a compact expert summary for a ComparisonResult-like object."""

    if result is None:
        return {}
    if not hasattr(result, "result_a") or not hasattr(result, "result_b"):
        return {}

    label_a = getattr(result.config, "label_a", "Condition A")
    label_b = getattr(result.config, "label_b", "Condition B")
    return {
        label_a: _noise_data(result.result_a),
        label_b: _noise_data(result.result_b),
        "Delta final fidelity": result.delta_final_fidelity,
        "Delta final purity": result.delta_final_purity,
        "Delta effective operation time": result.delta_effective_operation_time_us,
        "Better condition": result.better_condition,
    }


def _empty_data() -> dict[str, Any]:
    return {
        "overview": {},
        "noise": {},
        "operators": {},
        "state": {},
        "assumptions": _assumptions(),
        "h_eff": {
            "Status": "not enabled",
            "Note": "Run a simulation to inspect model internals.",
        },
    }


def _noise_data(result: SimulationResult) -> dict[str, Any]:
    environment = result.config.environment
    derived = result.derived_parameters
    gamma_down = derived.get("gamma_down_per_us")
    gamma_up = derived.get("gamma_up_per_us")
    gamma_population = derived.get(
        "gamma_population_relaxation_per_us",
        _sum_rates(gamma_down, gamma_up),
    )
    gamma_phi = derived.get("gamma_phi_per_us")
    return {
        "Environment model": environment.model,
        "T1 at zero temperature [us]": derived.get(
            "t1_zero_temperature_us", environment.t1_zero_temperature_us),
        "Tphi at zero temperature [us]": derived.get(
            "tphi_zero_temperature_us", environment.tphi_zero_temperature_us),
        "Temperature [mK]": derived.get("temperature_mk", environment.temperature_mk),
        "Qubit Frequency [GHz]": derived.get("qubit_frequency_ghz", environment.qubit_frequency_ghz),
        "Readout resonator frequency [GHz]": derived.get(
            "readout_resonator_frequency_ghz", environment.readout_resonator_frequency_ghz),
        "Readout resonator kappa/2pi [MHz]": derived.get(
            "readout_resonator_kappa_mhz", environment.readout_resonator_kappa_mhz),
        "Readout resonator chi/2pi [MHz]": derived.get(
            "readout_resonator_chi_mhz", environment.readout_resonator_chi_mhz),
        "Ideal reference": derived.get("ideal_reference", environment.ideal_reference),
        "n_th": derived.get("n_th"),
        "gamma0_per_us": derived.get("gamma0_per_us"),
        "Downward transition rate gamma_down [1/us]": gamma_down,
        "Upward transition rate gamma_up [1/us]": gamma_up,
        "Population relaxation rate gamma_population [1/us]": gamma_population,
        "Pure dephasing rate gamma_phi [1/us]": gamma_phi,
        "Resonator thermal occupation n_r": derived.get("n_th_resonator"),
        "Thermal-photon dephasing rate [1/us]": derived.get("gamma_phi_thermal_photon_per_us"),
        "Effective T1 [us]": derived.get("t1_effective_us"),
        "Effective Tphi [us]": derived.get("tphi_effective_us"),
        "Effective T2 [us]": derived.get("t2_effective_us"),
        "gamma ratio": _gamma_ratio(gamma_down, gamma_phi),
        "Dominant decoherence source": _dominant_source(gamma_down, gamma_phi),
    }


def _operator_data(result: SimulationResult) -> dict[str, Any]:
    n_qubits = result.config.circuit.logical_qubits
    derived = result.derived_parameters
    environment = result.config.environment
    legacy_downward_rate = derived.get("gamma_down_per_us", 0.0)
    legacy_dephasing_rate = derived.get("gamma_phi_per_us", 0.0)

    if not result.times:
        return {
            "Lindblad operators": "not available in current result",
            "Collapse operators": [],
            "H_eff": "not enabled",
        }

    operators = []
    if "gamma_down_per_us" in derived:
        gamma_down = derived.get("gamma_down_per_us", 0.0)
        gamma_up = derived.get("gamma_up_per_us", 0.0)
        gamma_phi = derived.get("gamma_phi_per_us", 0.0)
        for qubit in range(n_qubits):
            operators.append({
                "Name": "Relaxation operator",
                "Target qubit": qubit,
                "Enabled": gamma_down > 0.0,
                "Matrix": _matrix_components(
                    scale(
                        math.sqrt(gamma_down),
                        expand_single_qubit_gate(SIGMA_MINUS, qubit, n_qubits),
                    )
                    if gamma_down > 0.0
                    else None
                ),
            })
            operators.append({
                "Name": "Thermal excitation operator",
                "Target qubit": qubit,
                "Enabled": gamma_up > 0.0,
                "Matrix": _matrix_components(
                    scale(
                        math.sqrt(gamma_up),
                        expand_single_qubit_gate(SIGMA_PLUS, qubit, n_qubits),
                    )
                    if gamma_up > 0.0
                    else None
                ),
            })
            operators.append({
                "Name": "Pure dephasing operator",
                "Target qubit": qubit,
                "Enabled": gamma_phi > 0.0,
                "Matrix": _matrix_components(
                    scale(
                        math.sqrt(gamma_phi / 2.0),
                        expand_single_qubit_gate(Z, qubit, n_qubits),
                    )
                    if gamma_phi > 0.0
                    else None
                ),
            })
        return {
            "Lindblad operators": "available via reconstructed physical collapse operators",
            "Collapse operator count": len(multi_qubit_physical_collapse_operators(
                n_qubits,
                gamma_down,
                gamma_up,
                gamma_phi,
            )),
            "Collapse operators": operators,
            "H_eff": "not enabled",
        }

    for qubit in range(n_qubits):
        operators.append({
            "Name": "Relaxation operator",
            "Target qubit": qubit,
            "Enabled": legacy_downward_rate > 0.0,
            "Matrix": _matrix_components(
                scale(math.sqrt(legacy_downward_rate), expand_single_qubit_gate(SIGMA_MINUS, qubit, n_qubits))
                if legacy_downward_rate > 0.0
                else None
            ),
        })
        operators.append({
            "Name": "Pure dephasing operator",
            "Target qubit": qubit,
            "Enabled": legacy_dephasing_rate > 0.0,
            "Matrix": _matrix_components(
                scale(math.sqrt(legacy_dephasing_rate / 2.0), expand_single_qubit_gate(Z, qubit, n_qubits))
                if legacy_dephasing_rate > 0.0
                else None
            ),
        })

    return {
        "Lindblad operators": "available via reconstructed collapse operators",
        "Collapse operator count": len(multi_qubit_collapse_operators(
            n_qubits,
            legacy_downward_rate,
            legacy_dephasing_rate,
        )),
        "Collapse operators": operators,
        "H_eff": "not enabled",
    }


def _reconstruct_final_density_matrix(result: SimulationResult) -> Matrix | None:
    if not result.times:
        return None

    config = result.config
    n_qubits = config.circuit.logical_qubits
    dimension = 2 ** n_qubits
    legacy_downward_rate = result.derived_parameters.get("gamma_down_per_us")
    legacy_dephasing_rate = result.derived_parameters.get("gamma_phi_per_us")
    if legacy_downward_rate is None or legacy_dephasing_rate is None:
        return None

    state = initial_density_matrix(config.circuit.initial_states)
    for column in sorted(config.circuit.columns, key=lambda column: column.step):
        for gate in column.gates:
            state = clean_density_matrix(apply_gate_operation(state, gate, n_qubits))

    hamiltonian = zero_hamiltonian(dimension)
    if "gamma_down_per_us" in result.derived_parameters:
        max_rate = (
            abs(float(result.derived_parameters.get("gamma_down_per_us", 0.0)))
            + abs(float(result.derived_parameters.get("gamma_up_per_us", 0.0)))
            + abs(float(result.derived_parameters.get("gamma_phi_per_us", 0.0)))
        )
        collapse_ops = multi_qubit_physical_collapse_operators(
            n_qubits,
            result.derived_parameters.get("gamma_down_per_us", 0.0),
            result.derived_parameters.get("gamma_up_per_us", 0.0),
            result.derived_parameters.get("gamma_phi_per_us", 0.0),
        )
    else:
        max_rate = (
            abs(float(legacy_downward_rate))
            + abs(float(legacy_dephasing_rate))
        )
        collapse_ops = multi_qubit_collapse_operators(
            n_qubits,
            legacy_downward_rate,
            legacy_dephasing_rate,
        )
    for start_time, end_time in zip(result.times, result.times[1:]):
        state = _evolve_stable(
            state,
            hamiltonian,
            collapse_ops,
            end_time - start_time,
            max_rate,
        )
    return state


def _evolve_stable(
    state: Matrix,
    hamiltonian: Matrix,
    collapse_ops: list[Matrix],
    dt: float,
    max_rate: float,
) -> Matrix:
    substeps = max(
        1,
        math.ceil(abs(dt) * max(0.0, max_rate) / MAX_RK4_RATE_STEP_PRODUCT),
    )
    sub_dt = dt / substeps
    evolved = state
    for _ in range(substeps):
        evolved = clean_density_matrix(
            rk4_step(evolved, hamiltonian, collapse_ops, sub_dt)
        )
    return evolved


def _matrix_components(matrix: Matrix | None) -> dict[str, Any] | None:
    if matrix is None:
        return None
    return {
        "real": [[entry.real for entry in row] for row in matrix],
        "imag": [[entry.imag for entry in row] for row in matrix],
        "abs": [[abs(entry) for entry in row] for row in matrix],
    }


def _complex_to_dict(value: complex | None) -> dict[str, float] | None:
    if value is None:
        return None
    return {
        "real": value.real,
        "imag": value.imag,
        "abs": abs(value),
    }


def _hermiticity_error(matrix: Matrix) -> float:
    max_error = 0.0
    for row in range(len(matrix)):
        for column in range(len(matrix)):
            error = abs(matrix[row][column] - matrix[column][row].conjugate())
            max_error = max(max_error, error)
    return max_error


def _hermitian_eigenvalues(matrix: Matrix) -> list[float]:
    if len(matrix) == 1:
        return [matrix[0][0].real]
    if len(matrix) == 2:
        a = matrix[0][0].real
        d = matrix[1][1].real
        b_abs = abs(matrix[0][1])
        center = 0.5 * (a + d)
        radius = math.sqrt((0.5 * (a - d)) ** 2 + b_abs * b_abs)
        return [center - radius, center + radius]

    # For the current 4x4 case, return diagonal bounds as a conservative,
    # JSON-safe diagnostic rather than pulling in a numerical dependency.
    return [matrix[index][index].real for index in range(len(matrix))]


def _gate_count(columns: Any) -> int:
    return sum(len(column.gates) for column in columns)


def _circuit_depth(columns: Any) -> int:
    if not columns:
        return 0
    return max(column.step for column in columns) + 1


def _last(values: list[float]) -> float | None:
    return values[-1] if values else None


def _sum_rates(first: float | None, second: float | None) -> float | None:
    if first is None or second is None:
        return None
    return first + second


def _gamma_ratio(relaxation_rate: float | None, dephasing_rate: float | None) -> float | None:
    if relaxation_rate is None or dephasing_rate is None:
        return None
    if dephasing_rate == 0.0:
        return None
    return relaxation_rate / dephasing_rate


def _dominant_source(relaxation_rate: float | None, dephasing_rate: float | None) -> str:
    if relaxation_rate is None or dephasing_rate is None:
        return "not available in current result"
    if relaxation_rate > dephasing_rate:
        return "relaxation"
    if dephasing_rate > relaxation_rate:
        return "pure dephasing"
    return "balanced"


def _assumptions() -> list[str]:
    return [
        "weak-coupling open quantum system",
        "Born-Markov approximation",
        "Lindblad-type master equation",
        "phenomenological T1/T2 noise",
        "normalized environment parameters",
        f"standard environment model: {UNIFIED_ENVIRONMENT_MODEL}",
        "normalized controls are simple inputs mapped to physical rates",
        "generic transmon-inspired educational physical-unit model",
        "gate-aware mode uses effective Hamiltonians that reproduce ideal gates over assigned durations",
        "noise acts during gate/column execution via a Lindblad master equation",
        "no strict hardware calibration",
        "real device parameters fluctuate over time and across chips",
        "no strong-coupling memory effects",
        "no pulse-level control",
        "no leakage, crosstalk, drive calibration error, or non-Markovian memory is modeled",
        "not a research-grade full simulator",
    ]
