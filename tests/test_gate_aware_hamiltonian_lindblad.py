import unittest

from core.capabilities import (
    GATE_AWARE_HAMILTONIAN_LINDBLAD_MODEL,
    POST_CIRCUIT_DEGRADATION_MODEL,
)
from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.gates import (
    column_duration_us,
    column_unitary,
    effective_hamiltonian_from_involution,
)
from core.results import EnvironmentConfig, SimulationConfig
from core.simulator import run_simulation
from tests.physics_test_helpers import make_ideal_environment


class GateAwareHamiltonianLindbladTest(unittest.TestCase):
    def test_noiseless_h_matches_ideal_unitary(self) -> None:
        result = run_simulation(_one_qubit_h_config(make_ideal_environment()))

        self.assertAlmostEqual(result.output_probabilities["0"], 0.5, delta=1e-10)
        self.assertAlmostEqual(result.output_probabilities["1"], 0.5, delta=1e-10)
        self.assertAlmostEqual(result.fidelity[-1], 1.0, delta=1e-10)
        self.assertAlmostEqual(result.purity[-1], 1.0, delta=1e-10)

    def test_noiseless_bell_matches_ideal_unitary(self) -> None:
        result = run_simulation(_bell_config(make_ideal_environment()))

        self.assertAlmostEqual(result.output_probabilities["00"], 0.5, delta=1e-10)
        self.assertAlmostEqual(result.output_probabilities["11"], 0.5, delta=1e-10)
        self.assertAlmostEqual(result.output_probabilities["01"], 0.0, delta=1e-10)
        self.assertAlmostEqual(result.output_probabilities["10"], 0.0, delta=1e-10)
        self.assertAlmostEqual(result.fidelity[-1], 1.0, delta=1e-10)
        self.assertAlmostEqual(result.purity[-1], 1.0, delta=1e-10)

    def test_zero_rates_equivalence_for_non_h_gate(self) -> None:
        result = run_simulation(_one_qubit_x_config(make_ideal_environment()))

        self.assertAlmostEqual(result.output_probabilities["0"], 0.0, delta=1e-10)
        self.assertAlmostEqual(result.output_probabilities["1"], 1.0, delta=1e-10)
        self.assertAlmostEqual(result.fidelity[-1], 1.0, delta=1e-10)
        self.assertAlmostEqual(result.purity[-1], 1.0, delta=1e-10)

    def test_longer_cnot_duration_causes_more_degradation(self) -> None:
        environment = _finite_environment()
        short = run_simulation(_bell_config(
            environment,
            cnot_duration_us=0.20,
            duration_us=0.22,
        ))
        long = run_simulation(_bell_config(
            environment,
            cnot_duration_us=2.00,
            duration_us=2.02,
        ))

        self.assertLessEqual(long.fidelity[-1], short.fidelity[-1] + 1e-10)

    def test_very_long_cnot_duration_changes_bell_result(self) -> None:
        environment = _finite_environment()
        short = run_simulation(_bell_config(
            environment,
            cnot_duration_us=0.20,
            duration_us=0.22,
        ))
        long = run_simulation(_bell_config(
            environment,
            cnot_duration_us=20.00,
            duration_us=20.02,
        ))

        self.assertLess(long.purity[-1], short.purity[-1] - 1e-3)
        self.assertNotAlmostEqual(long.fidelity[-1], short.fidelity[-1], delta=1e-4)

    def test_h_duration_override_changes_result_under_finite_dephasing(self) -> None:
        environment = _finite_environment()
        short = run_simulation(_one_qubit_h_config(
            environment,
            h_duration_us=0.02,
            duration_us=0.02,
        ))
        long = run_simulation(_one_qubit_h_config(
            environment,
            h_duration_us=10.0,
            duration_us=10.0,
        ))

        self.assertAlmostEqual(short.diagnostics["total_gate_duration_us"], 0.02)
        self.assertAlmostEqual(long.diagnostics["total_gate_duration_us"], 10.0)
        self.assertNotAlmostEqual(long.purity[-1], short.purity[-1], delta=1e-4)

    def test_nonzero_duration_idle_i_columns_add_noise(self) -> None:
        environment = _finite_environment()
        just_h = run_simulation(_one_qubit_h_config(
            environment,
            h_duration_us=0.02,
            duration_us=0.02,
        ))
        h_plus_idle_i = run_simulation(_h_with_idle_i_columns_config(
            environment,
            idle_column_count=5,
            idle_duration_us=1.0,
        ))

        self.assertAlmostEqual(
            h_plus_idle_i.diagnostics["total_gate_duration_us"],
            5.02,
            delta=1e-10,
        )
        self.assertLess(h_plus_idle_i.purity[-1], just_h.purity[-1] - 1e-3)

    def test_idle_duration_causes_additional_degradation(self) -> None:
        environment = _finite_environment()
        short = run_simulation(_bell_config(environment, duration_us=0.22))
        long = run_simulation(_bell_config(environment, duration_us=5.0))

        self.assertLessEqual(long.fidelity[-1], short.fidelity[-1] + 1e-10)

    def test_gate_aware_differs_from_post_circuit_degradation_under_finite_rates(self) -> None:
        environment = _finite_environment()
        gate_aware = run_simulation(_bell_config(
            environment,
            duration_us=0.22,
            model=GATE_AWARE_HAMILTONIAN_LINDBLAD_MODEL,
        ))
        post_circuit = run_simulation(_bell_config(
            environment,
            duration_us=0.22,
            model=POST_CIRCUIT_DEGRADATION_MODEL,
        ))

        self.assertGreater(
            abs(gate_aware.fidelity[-1] - post_circuit.fidelity[-1]),
            1e-8,
        )

    def test_gate_aware_metadata_is_reported(self) -> None:
        result = run_simulation(_bell_config(_finite_environment(), duration_us=5.0))

        for key in [
            "simulation_mode",
            "total_gate_duration_us",
            "idle_duration_us",
            "gate_aware_noise",
            "hamiltonian_mode",
        ]:
            self.assertIn(key, result.derived_parameters)
        self.assertEqual(
            result.derived_parameters["simulation_mode"],
            GATE_AWARE_HAMILTONIAN_LINDBLAD_MODEL,
        )
        self.assertTrue(result.derived_parameters["gate_aware_noise"])
        self.assertEqual(
            result.diagnostics["simulation_mode"],
            GATE_AWARE_HAMILTONIAN_LINDBLAD_MODEL,
        )
        self.assertEqual(
            result.diagnostics["hamiltonian_mode"],
            "effective_unitary_spectral_generator_v2",
        )
        self.assertIn("total_gate_duration_us", result.diagnostics)
        self.assertIn("idle_duration_us", result.diagnostics)

    def test_effective_hamiltonian_for_nonzero_h_gate_is_not_zero(self) -> None:
        column = GateColumn(
            step=0,
            gates=[
                GateOperation(
                    type="H",
                    targets=[0],
                    params={"duration_us": 5.0},
                )
            ],
        )

        hamiltonian = effective_hamiltonian_from_involution(
            column_unitary(column, 1),
            column_duration_us(column),
        )

        self.assertGreater(max(abs(entry) for row in hamiltonian for entry in row), 0.0)


def _finite_environment() -> EnvironmentConfig:
    return EnvironmentConfig(
        input_mode="physical",
        device_quality=1.0,
        temperature_mk=0.0,
        flux_noise_phi0=0.0,
        t1_max_us=10.0,
        tphi_max_us=10.0,
    )


def _one_qubit_h_config(
    environment: EnvironmentConfig,
    h_duration_us: float | None = None,
    duration_us: float = 1.0,
) -> SimulationConfig:
    params = {} if h_duration_us is None else {"duration_us": h_duration_us}
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=1,
            initial_states=["0"],
            columns=[
                GateColumn(
                    step=0,
                    gates=[GateOperation(type="H", targets=[0], params=params)],
                )
            ],
        ),
        environment=environment,
        duration_us=duration_us,
        time_steps=101,
        fidelity_threshold=0.9,
        model=GATE_AWARE_HAMILTONIAN_LINDBLAD_MODEL,
    )


def _one_qubit_x_config(environment: EnvironmentConfig) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=1,
            initial_states=["0"],
            columns=[
                GateColumn(
                    step=0,
                    gates=[GateOperation(type="X", targets=[0])],
                )
            ],
        ),
        environment=environment,
        duration_us=1.0,
        time_steps=101,
        fidelity_threshold=0.9,
        model=GATE_AWARE_HAMILTONIAN_LINDBLAD_MODEL,
    )


def _bell_config(
    environment: EnvironmentConfig,
    cnot_duration_us: float = 0.20,
    duration_us: float = 1.0,
    model: str = GATE_AWARE_HAMILTONIAN_LINDBLAD_MODEL,
) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=2,
            initial_states=["0", "0"],
            columns=[
                GateColumn(
                    step=0,
                    gates=[GateOperation(type="H", targets=[0])],
                ),
                GateColumn(
                    step=1,
                    gates=[
                        GateOperation(
                            type="CNOT",
                            targets=[1],
                            controls=[0],
                            params={"duration_us": cnot_duration_us},
                        )
                    ],
                ),
            ],
        ),
        environment=environment,
        duration_us=duration_us,
        time_steps=101,
        fidelity_threshold=0.9,
        model=model,
    )


def _h_with_idle_i_columns_config(
    environment: EnvironmentConfig,
    idle_column_count: int,
    idle_duration_us: float,
) -> SimulationConfig:
    columns = [
        GateColumn(
            step=0,
            gates=[
                GateOperation(
                    type="H",
                    targets=[0],
                    params={"duration_us": 0.02},
                )
            ],
        )
    ]
    for index in range(idle_column_count):
        columns.append(GateColumn(
            step=index + 1,
            gates=[
                GateOperation(
                    type="I",
                    targets=[0],
                    params={"duration_us": idle_duration_us},
                )
            ],
        ))
    total_duration = 0.02 + idle_column_count * idle_duration_us
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=1,
            initial_states=["0"],
            columns=columns,
        ),
        environment=environment,
        duration_us=total_duration,
        time_steps=101,
        fidelity_threshold=0.9,
        model=GATE_AWARE_HAMILTONIAN_LINDBLAD_MODEL,
    )


if __name__ == "__main__":
    unittest.main()
