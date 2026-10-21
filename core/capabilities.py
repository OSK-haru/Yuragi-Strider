"""Shared core capability declarations.

Keep these values small and JSON-friendly so UI, save/load, and future backend
adapters can discover the same basic contract without importing implementation
details from environment/evolution/metrics.
"""

from __future__ import annotations


DEFAULT_SIMULATION_MODEL = "weak_coupling_lindblad"
POST_CIRCUIT_DEGRADATION_MODEL = "post_circuit_degradation_v1"
GATE_AWARE_SPLIT_STEP_MODEL = "gate_aware_split_step_v1"
GATE_AWARE_HAMILTONIAN_LINDBLAD_MODEL = "gate_aware_hamiltonian_lindblad_v1"
DRIVEN_TWO_LEVEL_RWA_EXPERIMENTAL_MODEL = (
    "driven_two_level_rwa_experimental_v1"
)
DRIVEN_TRANSMON_QUTRIT_RWA_EXPERIMENTAL_MODEL = (
    "driven_transmon_qutrit_rwa_experimental_v1"
)
DRIVEN_COUPLED_TRANSMON_NETWORK_RWA_EXPERIMENTAL_MODEL = (
    "driven_coupled_transmon_network_rwa_experimental_v1"
)
SUPPORTED_SIMULATION_MODELS = frozenset({
    DEFAULT_SIMULATION_MODEL,
    POST_CIRCUIT_DEGRADATION_MODEL,
    GATE_AWARE_SPLIT_STEP_MODEL,
    GATE_AWARE_HAMILTONIAN_LINDBLAD_MODEL,
})
SUPPORTED_PULSE_MODELS = frozenset({
    DRIVEN_TWO_LEVEL_RWA_EXPERIMENTAL_MODEL,
    DRIVEN_TRANSMON_QUTRIT_RWA_EXPERIMENTAL_MODEL,
    DRIVEN_COUPLED_TRANSMON_NETWORK_RWA_EXPERIMENTAL_MODEL,
})
DECLARED_PULSE_MODELS = frozenset({
    DRIVEN_TWO_LEVEL_RWA_EXPERIMENTAL_MODEL,
    DRIVEN_TRANSMON_QUTRIT_RWA_EXPERIMENTAL_MODEL,
    DRIVEN_COUPLED_TRANSMON_NETWORK_RWA_EXPERIMENTAL_MODEL,
})
PULSE_MODEL_STATUSES = {
    DRIVEN_TWO_LEVEL_RWA_EXPERIMENTAL_MODEL: "available",
    DRIVEN_TRANSMON_QUTRIT_RWA_EXPERIMENTAL_MODEL: "available",
    DRIVEN_COUPLED_TRANSMON_NETWORK_RWA_EXPERIMENTAL_MODEL: "experimental",
}
SUPPORTED_GATES = frozenset({
    "I", "H", "X", "Y", "Z", "S", "T", "RX", "RY", "RZ",
    "CNOT", "CZ", "ECR", "CP", "CCX", "SWAP", "QFT", "ORACLE", "MEASURE", "MESSAGE",
})
MAX_LOGICAL_QUBITS = 18
MAX_DENSITY_MATRIX_QUBITS = 8
STATEVECTOR_AUTO_SWITCH_QUBITS = 5
MAX_STATEVECTOR_QUBITS = 18


def normalize_gate_type(gate_type: str) -> str:
    """Normalize gate labels while preserving JSON-level string inputs."""

    return str(gate_type).upper()


def core_capabilities() -> dict[str, object]:
    """Return a backend/UI neutral summary of the current core contract."""

    return {
        "default_simulation_model": DEFAULT_SIMULATION_MODEL,
        "default_simulation_mode": GATE_AWARE_HAMILTONIAN_LINDBLAD_MODEL,
        "supported_simulation_models": sorted(SUPPORTED_SIMULATION_MODELS),
        "supported_pulse_models": sorted(SUPPORTED_PULSE_MODELS),
        "declared_pulse_models": sorted(DECLARED_PULSE_MODELS),
        "pulse_model_statuses": dict(PULSE_MODEL_STATUSES),
        "supported_gates": sorted(SUPPORTED_GATES),
        "max_logical_qubits": MAX_LOGICAL_QUBITS,
        "max_density_matrix_qubits": MAX_DENSITY_MATRIX_QUBITS,
        "statevector_auto_switch_qubits": STATEVECTOR_AUTO_SWITCH_QUBITS,
        "max_statevector_qubits": MAX_STATEVECTOR_QUBITS,
    }
