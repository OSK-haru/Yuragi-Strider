import unittest

import numpy as np
from pydantic import ValidationError

from api.main import SimulateRequest, build_config_from_simulate_request, simulate
from api.main import build_bit_flip_repetition_circuit, build_teleportation_circuit
from core.backend_boundary import PYTHON_DENSE_BACKEND, RUST_DENSE_PREVIEW_BACKEND
from core.circuit_model import (
    CircuitConfig,
    ClassicalCondition,
    GateColumn,
    GateOperation,
)
from core.gate_compiler import AUTO_DECOMPOSE
from core.gates import (
    apply_non_selective_computational_measurement,
    computational_measurement_outcomes,
)
from core.results import EnvironmentConfig, SimulationConfig
from core.simulator import run_simulation
from core.ui_response import simulation_result_to_ui_response


class ComputationalMeasurementChannelTests(unittest.TestCase):
    def test_measurement_instrument_returns_conditional_bell_states(self) -> None:
        bell_density = (
            (0.5 + 0.0j, 0.0j, 0.0j, 0.5 + 0.0j),
            (0.0j, 0.0j, 0.0j, 0.0j),
            (0.0j, 0.0j, 0.0j, 0.0j),
            (0.5 + 0.0j, 0.0j, 0.0j, 0.5 + 0.0j),
        )
        outcomes = computational_measurement_outcomes(bell_density, [0], 2)

        self.assertAlmostEqual(outcomes["0"][0], 0.5, delta=1e-12)
        self.assertAlmostEqual(outcomes["1"][0], 0.5, delta=1e-12)
        self.assertAlmostEqual(outcomes["0"][1][0][0].real, 1.0, delta=1e-12)
        self.assertAlmostEqual(outcomes["1"][1][3][3].real, 1.0, delta=1e-12)

    def test_classical_register_schema_round_trips(self) -> None:
        config = CircuitConfig(
            logical_qubits=1,
            initial_states=["0"],
            classical_bits=1,
            columns=[GateColumn(0, [GateOperation(
                "MEASURE",
                [0],
                classical_targets=[0],
            )]), GateColumn(1, [GateOperation(
                "X",
                [0],
                condition=ClassicalCondition(bit=0, value=1),
            )])],
        )

        restored = CircuitConfig.from_dict(config.to_dict())
        self.assertEqual(restored.classical_bits, 1)
        self.assertEqual(restored.columns[0].gates[0].classical_targets, [0])
        self.assertEqual(restored.columns[1].gates[0].condition.value, 1)

    def test_multiple_classical_conditions_round_trip(self) -> None:
        gate = GateOperation(
            "X",
            [0],
            conditions=[
                ClassicalCondition(bit=0, value=1),
                ClassicalCondition(bit=1, value=0),
            ],
        )
        restored = GateOperation.from_dict(gate.to_dict())
        self.assertEqual([(item.bit, item.value) for item in restored.conditions], [(0, 1), (1, 0)])

    def test_discarded_outcome_measurement_removes_only_target_coherences(self) -> None:
        bell_density = (
            (0.5 + 0.0j, 0.0j, 0.0j, 0.5 + 0.0j),
            (0.0j, 0.0j, 0.0j, 0.0j),
            (0.0j, 0.0j, 0.0j, 0.0j),
            (0.5 + 0.0j, 0.0j, 0.0j, 0.5 + 0.0j),
        )
        measured = apply_non_selective_computational_measurement(
            bell_density,
            [0],
            2,
        )

        expected = np.diag([0.5, 0.0, 0.0, 0.5])
        self.assertLess(
            float(np.max(np.abs(np.asarray(measured) - expected))),
            1e-12,
        )
        measured_twice = apply_non_selective_computational_measurement(
            measured,
            [0],
            2,
        )
        self.assertEqual(measured_twice, measured)

    def test_invalid_measurement_target_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            apply_non_selective_computational_measurement(
                ((1.0 + 0.0j, 0.0j), (0.0j, 0.0j)),
                [1],
                1,
            )


class GateAwareMeasurementExecutionTests(unittest.TestCase):
    def test_mid_circuit_measurement_changes_following_gate_result(self) -> None:
        for evolution_method in ("fixed_step_rk4", "explicit_cptp"):
            with self.subTest(evolution_method=evolution_method):
                result = run_simulation(_measurement_config(evolution_method))

                self.assertFalse(result.issues)
                self.assertAlmostEqual(result.output_probabilities["0"], 0.5, delta=1e-10)
                self.assertAlmostEqual(result.output_probabilities["1"], 0.5, delta=1e-10)
                self.assertAlmostEqual(result.purity[-1], 0.5, delta=1e-10)
                self.assertAlmostEqual(result.fidelity[-1], 1.0, delta=1e-10)

                measurement_snapshot = next(
                    snapshot for snapshot in result.state_snapshots
                    if snapshot.kind == "measurement"
                )
                self.assertAlmostEqual(
                    measurement_snapshot.density_matrix[0][1].real,
                    0.0,
                    delta=1e-12,
                )
                self.assertAlmostEqual(
                    measurement_snapshot.density_matrix[0][1].imag,
                    0.0,
                    delta=1e-12,
                )

    def test_shot_counts_are_seeded_and_match_final_distribution(self) -> None:
        first = run_simulation(_measurement_config("fixed_step_rk4"))
        second = run_simulation(_measurement_config("fixed_step_rk4"))

        self.assertEqual(first.measurement_counts, second.measurement_counts)
        self.assertEqual(sum(first.measurement_counts.values()), 4096)
        self.assertLess(abs(first.measurement_counts["0"] / 4096 - 0.5), 0.04)
        self.assertLess(abs(first.measurement_counts["1"] / 4096 - 0.5), 0.04)

    def test_auto_decomposed_measurement_has_python_rust_parity(self) -> None:
        results = []
        for backend in (PYTHON_DENSE_BACKEND, RUST_DENSE_PREVIEW_BACKEND):
            config = _measurement_config("explicit_cptp")
            config.compilation_mode = AUTO_DECOMPOSE
            config.simulation_backend = backend
            result = run_simulation(config)
            results.append(result)

            self.assertFalse(result.issues)
            self.assertAlmostEqual(result.output_probabilities["0"], 0.5, delta=1e-10)
            self.assertAlmostEqual(result.output_probabilities["1"], 0.5, delta=1e-10)
            self.assertAlmostEqual(result.purity[-1], 0.5, delta=1e-10)
            self.assertTrue(any(
                snapshot.kind == "measurement"
                for snapshot in result.state_snapshots
            ))

        for state in results[0].output_probabilities:
            self.assertAlmostEqual(
                results[1].output_probabilities[state],
                results[0].output_probabilities[state],
                delta=1e-10,
            )
        self.assertEqual(results[1].measurement_counts, results[0].measurement_counts)

    def test_ui_response_discloses_measurement_semantics_and_counts(self) -> None:
        config = _measurement_config("fixed_step_rk4")
        config.environment.ideal_reference = False
        config.environment.t1_max_us = 1e12
        config.environment.tphi_max_us = 1e12
        response = simulation_result_to_ui_response(
            run_simulation(config)
        )
        measurement = response["measurement"]

        self.assertEqual(measurement["shots"], 4096)
        self.assertEqual(measurement["seed"], 37)
        self.assertEqual(sum(measurement["counts"].values()), 4096)
        self.assertEqual(measurement["explicit_measurement_count"], 1)
        self.assertEqual(measurement["explicit_measurement_targets"], [0])
        self.assertIn("classical_register_mode", measurement)
        self.assertEqual(
            measurement["explicit_measurement_mode"],
            "non_selective_computational_basis_v1",
        )
        self.assertFalse(measurement["classical_conditioning_supported"])


class GateAwareMeasurementApiTests(unittest.TestCase):
    def test_teleportation_and_repetition_presets_have_explicit_feed_forward(self) -> None:
        teleportation = build_teleportation_circuit()
        repetition = build_bit_flip_repetition_circuit()
        self.assertEqual(teleportation.logical_qubits, 3)
        self.assertEqual(teleportation.classical_bits, 2)
        self.assertEqual(repetition.logical_qubits, 5)
        corrections = [
            gate
            for column in repetition.columns
            for gate in column.gates
            if gate.type == "X" and gate.conditions
        ]
        self.assertEqual(len(corrections), 3)
        self.assertTrue(all(len(gate.conditions) == 2 for gate in corrections))

    def test_repetition_preset_is_executable_through_api(self) -> None:
        response = simulate(SimulateRequest(
            circuit_preset="bit_flip_repetition",
            simulation_backend="python_dense",
            parameters={
                "normalized_temperature": 0.0,
                "normalized_magnetic_field": 0.0,
                "noise_level": 0.0,
                "duration_us": 2.0,
                "time_steps": 5,
                "fidelity_threshold": 0.9,
            },
        ))
        self.assertFalse(response["issues"])
        self.assertEqual(response["circuit"]["qubit_count"], 5)
        self.assertEqual(response["measurement"]["classical_register_bits"], 2)

    def test_ideal_teleportation_preserves_input_on_target(self) -> None:
        result = run_simulation(SimulationConfig(
            circuit=build_teleportation_circuit(),
            environment=EnvironmentConfig(input_mode="physical", ideal_reference=True),
            duration_us=2.0,
            time_steps=11,
        ))
        self.assertFalse(result.issues)
        target_one = sum(
            probability
            for label, probability in result.output_probabilities.items()
            if label[-1] == "1"
        )
        self.assertAlmostEqual(target_one, 0.5, delta=1e-8)
        self.assertTrue(result.diagnostics["classical_branching_noise_applied"])
        self.assertEqual(result.diagnostics["execution_representation"], "density_matrix")

    def test_ideal_repetition_code_corrects_injected_bit_flip(self) -> None:
        result = run_simulation(SimulationConfig(
            circuit=build_bit_flip_repetition_circuit(),
            environment=EnvironmentConfig(input_mode="physical", ideal_reference=True),
            duration_us=2.0,
            time_steps=11,
        ))
        self.assertFalse(result.issues)
        data_one = sum(
            probability
            for label, probability in result.output_probabilities.items()
            if label[:3] == "111"
        )
        self.assertAlmostEqual(data_one, 1.0, delta=1e-8)

    def test_ideal_statevector_path_scales_to_editor_limit(self) -> None:
        config = SimulationConfig(
            circuit=CircuitConfig(
                logical_qubits=8,
                initial_states=["0"] * 8,
                columns=[GateColumn(0, [GateOperation("H", [0])])],
            ),
            environment=EnvironmentConfig(input_mode="physical", ideal_reference=True),
            duration_us=1.0,
            time_steps=3,
            simulation_backend=PYTHON_DENSE_BACKEND,
        )
        result = run_simulation(config)
        self.assertFalse(result.issues)
        self.assertEqual(result.diagnostics["execution_representation"], "statevector")
        self.assertAlmostEqual(result.output_probabilities["00000000"], 0.5, delta=1e-12)
        self.assertAlmostEqual(result.output_probabilities["10000000"], 0.5, delta=1e-12)

    def test_api_preserves_shots_and_seed(self) -> None:
        request = SimulateRequest(**_api_payload(shots=2048, seed=123))
        config = build_config_from_simulate_request(request)

        self.assertEqual(config.measurement_shots, 2048)
        self.assertEqual(config.measurement_seed, 123)

    def test_api_rejects_invalid_shot_count(self) -> None:
        with self.assertRaises(ValidationError):
            SimulateRequest(**_api_payload(shots=0, seed=0))

    def test_api_preserves_classical_measurement_binding(self) -> None:
        payload = _api_payload(shots=128, seed=9)
        payload["circuit_config"]["classical_bits"] = 1
        payload["circuit_config"]["columns"][0]["gates"][0]["classical_targets"] = [0]
        request = SimulateRequest(**payload)
        config = build_config_from_simulate_request(request)

        self.assertEqual(config.circuit.classical_bits, 1)
        self.assertEqual(
            config.circuit.columns[0].gates[0].classical_targets,
            [0],
        )
        response = simulate(request)
        self.assertEqual(response["circuit"]["classical_bit_count"], 1)
        self.assertEqual(response["measurement"]["classical_register_bits"], 1)

    def test_conditional_gate_uses_shot_branching_preview(self) -> None:
        payload = _api_payload(shots=128, seed=9)
        payload["circuit_config"]["classical_bits"] = 1
        payload["circuit_config"]["columns"] = [
            {"step": 0, "gates": [{"type": "H", "targets": [0]}]},
            {
                "step": 1,
                "gates": [{
                    "type": "MEASURE",
                    "targets": [0],
                    "classical_targets": [0],
                }],
            },
            {
                "step": 2,
                "gates": [{
                    "type": "X",
                    "targets": [0],
                    "condition": {"bit": 0, "value": 1},
                }],
            },
        ]
        response = simulate(SimulateRequest(**payload))

        self.assertFalse(response["issues"])
        self.assertGreater(response["output_probabilities"]["0"], 0.0)
        self.assertLess(response["output_probabilities"]["0"], 1.0)
        self.assertEqual(response["measurement"]["classical_branch_count"], 2)
        self.assertTrue(response["measurement"]["classical_branching_noise_applied"])
        self.assertEqual(len(response["measurement"]["classical_branches"]), 2)
        self.assertEqual(len(response["measurement"]["classical_shot_preview"]), 64)
        self.assertTrue(all("classical_bits" in shot for shot in response["measurement"]["classical_shot_preview"]))


def _measurement_config(evolution_method: str) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=1,
            initial_states=["0"],
            columns=[
                GateColumn(0, [GateOperation(
                    "H", [0], params={"duration_us": 0.02},
                )]),
                GateColumn(1, [GateOperation(
                    "MEASURE", [0], params={"duration_us": 0.0},
                )]),
                GateColumn(2, [GateOperation(
                    "H", [0], params={"duration_us": 0.02},
                )]),
            ],
        ),
        environment=EnvironmentConfig(
            input_mode="physical",
            ideal_reference=True,
            device_quality=1.0,
            temperature_mk=0.0,
            flux_noise_phi0=0.0,
        ),
        duration_us=0.1,
        time_steps=21,
        fidelity_threshold=0.9,
        evolution_method=evolution_method,
        simulation_backend=PYTHON_DENSE_BACKEND,
        measurement_shots=4096,
        measurement_seed=37,
        snapshot_options={
            "enabled": True,
            "uniform_count": 0,
            "include_initial": True,
            "include_final": True,
            "include_column_boundaries": True,
            "include_after_circuit": True,
        },
    )


def _api_payload(*, shots: int, seed: int) -> dict[str, object]:
    return {
        "simulation_backend": "python_dense",
        "input_mode": "normalized",
        "measurement_options": {"shots": shots, "seed": seed},
        "circuit_config": {
            "logical_qubits": 1,
            "initial_states": [0],
            "columns": [{
                "step": 0,
                "gates": [{"type": "MEASURE", "targets": [0]}],
            }],
        },
        "parameters": {
            "normalized_temperature": 0.0,
            "normalized_magnetic_field": 0.0,
            "noise_level": 0.0,
            "duration_us": 0.1,
            "time_steps": 11,
            "fidelity_threshold": 0.9,
        },
    }


if __name__ == "__main__":
    unittest.main()
