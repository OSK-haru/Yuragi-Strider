import unittest

import numpy as np

from api.main import SimulateRequest, build_config_from_simulate_request
from core.capabilities import GATE_AWARE_HAMILTONIAN_LINDBLAD_MODEL
from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.gates import (
    S,
    T,
    X,
    effective_hamiltonian_from_involution,
    effective_hamiltonian_from_unitary,
    unitary_from_hamiltonian,
)
from core.results import EnvironmentConfig, SimulationConfig
from core.simulator import run_simulation


class GeneralUnitaryGeneratorTests(unittest.TestCase):
    def test_legacy_involution_branch_is_unchanged(self) -> None:
        legacy = effective_hamiltonian_from_involution(X, 0.02)
        generalized = effective_hamiltonian_from_unitary(X, 0.02)
        self.assertEqual(generalized, legacy)

    def test_s_and_t_are_reconstructed_from_hermitian_generators(self) -> None:
        for unitary in (S, T):
            hamiltonian = effective_hamiltonian_from_unitary(unitary, 0.02)
            reconstructed = unitary_from_hamiltonian(hamiltonian, 0.02)
            self.assertLess(
                float(np.max(np.abs(
                    np.asarray(reconstructed) - np.asarray(unitary)
                ))),
                1e-12,
            )

    def test_nonunitary_matrix_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be unitary"):
            effective_hamiltonian_from_unitary(
                ((1.0 + 0.0j, 1.0 + 0.0j), (0.0 + 0.0j, 1.0 + 0.0j)),
                0.02,
            )


class GeneralUnitaryGateAwareExecutionTests(unittest.TestCase):
    def test_finite_duration_s_gate_runs_in_gate_aware_model(self) -> None:
        config = SimulationConfig(
            circuit=CircuitConfig(
                logical_qubits=1,
                initial_states=["0"],
                columns=[
                    GateColumn(0, [GateOperation("H", [0])]),
                    GateColumn(1, [
                        GateOperation("S", [0], params={"duration_us": 0.02})
                    ]),
                ],
            ),
            environment=EnvironmentConfig(temperature_mk=0.0, ideal_reference=True),
            duration_us=0.04,
            time_steps=21,
            fidelity_threshold=0.9,
            model=GATE_AWARE_HAMILTONIAN_LINDBLAD_MODEL,
        )
        result = run_simulation(config)
        self.assertAlmostEqual(result.fidelity[-1], 1.0, delta=1e-10)
        self.assertAlmostEqual(result.output_probabilities["0"], 0.5, delta=1e-10)
        self.assertAlmostEqual(result.output_probabilities["1"], 0.5, delta=1e-10)
        self.assertEqual(
            result.diagnostics["hamiltonian_mode"],
            "effective_unitary_spectral_generator_v2",
        )

        cptp_config = SimulationConfig.from_dict(config.to_dict())
        cptp_config.evolution_method = "explicit_cptp"
        cptp_result = run_simulation(cptp_config)
        self.assertAlmostEqual(cptp_result.fidelity[-1], 1.0, delta=1e-10)
        self.assertTrue(cptp_result.diagnostics["cptp_all_maps_passed_audit"])

    def test_api_accepts_universal_single_qubit_gate_set(self) -> None:
        payload = {
            "simulation_backend": "python_dense",
            "circuit_config": {
                "logical_qubits": 1,
                "initial_states": [0],
                "columns": [
                    {"step": 0, "gates": [{"type": "Y", "targets": [0]}]},
                    {"step": 1, "gates": [{"type": "S", "targets": [0]}]},
                    {"step": 2, "gates": [{"type": "T", "targets": [0]}]},
                ],
            },
            "parameters": {
                "temperature_mk": 10.0,
                "qubit_frequency_ghz": 5.0,
                "t1_zero_temperature_us": 100.0,
                "tphi_us": 66.66666666666667,
                "duration_us": 0.02,
                "time_steps": 11,
                "fidelity_threshold": 0.9,
            },
        }
        request = SimulateRequest(**payload)
        config = build_config_from_simulate_request(request)
        self.assertEqual(
            [column.gates[0].type for column in config.circuit.columns],
            ["Y", "S", "T"],
        )
        self.assertAlmostEqual(config.circuit.columns[0].gates[0].params["duration_us"], 0.02)
        self.assertAlmostEqual(config.circuit.columns[1].gates[0].params["duration_us"], 0.0)
        self.assertAlmostEqual(config.circuit.columns[2].gates[0].params["duration_us"], 0.0)


if __name__ == "__main__":
    unittest.main()
