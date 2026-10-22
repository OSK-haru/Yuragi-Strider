"""Gate-aware explicit-CPTP execution contract and regressions."""

import unittest

from pydantic import ValidationError

from api.main import SimulateRequest, build_config_from_simulate_request, simulate
from core.capabilities import GATE_AWARE_HAMILTONIAN_LINDBLAD_MODEL
from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.gates import (
    column_duration_us,
    column_unitary,
    effective_hamiltonian_from_involution,
    initial_density_matrix,
    multi_qubit_environment_collapse_operators,
    zero_hamiltonian,
)
from core.physical_environment import compute_environment_rates
from core.results import EnvironmentConfig, SimulationConfig
from core.rust_dense_kernel import is_rust_kernel_available
from core.simulator import run_simulation
from validation_pulse.qutip_adapter import (
    QUTIP_AVAILABLE,
    compare_density_matrices,
    run_qutip_piecewise_segments,
)
from tests.physics_test_helpers import make_ideal_environment


class GateAwareCPTPTests(unittest.TestCase):
    def test_noiseless_bell_is_ideal_without_cleanup(self) -> None:
        result = run_simulation(_bell_config(
            environment=make_ideal_environment(),
            evolution_method="explicit_cptp",
        ))

        self.assertFalse(result.issues)
        self.assertAlmostEqual(result.output_probabilities["00"], 0.5, delta=1e-10)
        self.assertAlmostEqual(result.output_probabilities["11"], 0.5, delta=1e-10)
        self.assertAlmostEqual(result.fidelity[-1], 1.0, delta=1e-10)
        self.assertAlmostEqual(result.purity[-1], 1.0, delta=1e-10)
        self.assertTrue(result.diagnostics["cptp_guaranteed_by_construction"])
        self.assertFalse(result.diagnostics["cleanup_applied"])
        self.assertTrue(result.diagnostics["cptp_all_maps_passed_audit"])
        self.assertEqual(result.diagnostics["integration_substeps"], 0.0)

    def test_finite_noise_agrees_with_validated_rk4_path(self) -> None:
        rk4 = run_simulation(_bell_config(
            environment=_finite_environment(),
            evolution_method="fixed_step_rk4",
        ))
        cptp = run_simulation(_bell_config(
            environment=_finite_environment(),
            evolution_method="explicit_cptp",
        ))

        self.assertAlmostEqual(cptp.fidelity[-1], rk4.fidelity[-1], delta=3e-4)
        self.assertAlmostEqual(cptp.purity[-1], rk4.purity[-1], delta=5e-4)
        self.assertLessEqual(cptp.diagnostics["max_trace_error"], 1e-12)

    def test_config_round_trip_preserves_evolution_method(self) -> None:
        config = _bell_config(
            environment=_finite_environment(),
            evolution_method="explicit_cptp",
        )
        restored = SimulationConfig.from_dict(config.to_dict())
        self.assertEqual(restored.evolution_method, "explicit_cptp")


class GateAwareCPTPApiTests(unittest.TestCase):
    def test_api_selects_and_reports_explicit_cptp(self) -> None:
        request = SimulateRequest(**_physical_payload("explicit_cptp"))
        config = build_config_from_simulate_request(request)
        response = simulate(request)

        self.assertEqual(config.evolution_method, "explicit_cptp")
        self.assertEqual(
            response["diagnostics"]["evolution_method_resolved"],
            "explicit_cptp",
        )
        self.assertTrue(
            response["diagnostics"]["cptp_guaranteed_by_construction"]
        )

    def test_api_defaults_to_existing_rk4_path(self) -> None:
        payload = _physical_payload("fixed_step_rk4")
        payload.pop("evolution_method")
        config = build_config_from_simulate_request(SimulateRequest(**payload))
        self.assertEqual(config.evolution_method, "fixed_step_rk4")

    def test_unknown_api_evolution_method_is_rejected(self) -> None:
        payload = _physical_payload("fixed_step_rk4")
        payload["evolution_method"] = "implicit_magic"
        with self.assertRaises(ValidationError):
            SimulateRequest(**payload)

    def test_api_accepts_rust_dense_preview_backend(self) -> None:
        payload = _physical_payload("fixed_step_rk4")
        payload["simulation_backend"] = "rust_dense_preview"
        request = SimulateRequest(**payload)
        config = build_config_from_simulate_request(request)

        self.assertEqual(config.simulation_backend, "rust_dense_preview")

    def test_api_rejects_unknown_backend(self) -> None:
        payload = _physical_payload("fixed_step_rk4")
        payload["simulation_backend"] = "cuda_dense"
        with self.assertRaises(ValidationError):
            SimulateRequest(**payload)


@unittest.skipUnless(QUTIP_AVAILABLE, "QuTiP is a validation-only dependency")
class GateAwareCPTPQuTiPTests(unittest.TestCase):
    def test_bell_trajectory_final_state_matches_qutip(self) -> None:
        config = _bell_config(
            environment=_finite_environment(),
            evolution_method="explicit_cptp",
        )
        result = run_simulation(config)
        rates = compute_environment_rates(config.environment)
        collapse_ops = multi_qubit_environment_collapse_operators(
            config.circuit.logical_qubits,
            rates,
        )
        segments = []
        for column in config.circuit.columns:
            duration = column_duration_us(column)
            segments.append({
                "duration_us": duration,
                "hamiltonian": effective_hamiltonian_from_involution(
                    column_unitary(column, config.circuit.logical_qubits),
                    duration,
                ),
            })
        gate_duration = sum(item["duration_us"] for item in segments)
        segments.append({
            "duration_us": config.duration_us - gate_duration,
            "hamiltonian": zero_hamiltonian(
                2 ** config.circuit.logical_qubits
            ),
        })
        qutip_state = run_qutip_piecewise_segments(
            initial_density_matrix(config.circuit.initial_states),
            segments,
            collapse_ops,
            config.circuit.logical_qubits,
        )[-1]
        final_snapshot = next(
            snapshot
            for snapshot in result.state_snapshots
            if snapshot.kind == "final"
        )
        metrics = compare_density_matrices(
            final_snapshot.density_matrix,
            qutip_state,
        )

        self.assertLessEqual(metrics["max_element_difference"], 2e-9)
        self.assertLessEqual(metrics["trace_distance"], 2e-9)


@unittest.skipUnless(
    is_rust_kernel_available(),
    "yuragi_strider_rust is not importable",
)
class GateAwareCPTPRustParityTests(unittest.TestCase):
    def test_bell_python_and_rust_cptp_paths_match(self) -> None:
        python_config = _bell_config(
            environment=_finite_environment(),
            evolution_method="explicit_cptp",
        )
        rust_config = SimulationConfig.from_dict(python_config.to_dict())
        rust_config.simulation_backend = "rust_dense_preview"

        python_result = run_simulation(python_config)
        rust_result = run_simulation(rust_config)

        self.assertAlmostEqual(
            python_result.fidelity[-1],
            rust_result.fidelity[-1],
            delta=2e-11,
        )
        self.assertAlmostEqual(
            python_result.purity[-1],
            rust_result.purity[-1],
            delta=2e-11,
        )
        self.assertEqual(python_result.diagnostics["cptp_backend"], "python")
        self.assertEqual(rust_result.diagnostics["cptp_backend"], "rust")
        self.assertTrue(rust_result.diagnostics["cptp_all_maps_passed_audit"])


def _bell_config(
    *,
    environment: EnvironmentConfig,
    evolution_method: str,
) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=2,
            initial_states=["0", "0"],
            columns=[
                GateColumn(
                    step=0,
                    gates=[
                        GateOperation(
                            type="H",
                            targets=[0],
                            params={"duration_us": 0.02},
                        )
                    ],
                ),
                GateColumn(
                    step=1,
                    gates=[
                        GateOperation(
                            type="CNOT",
                            targets=[1],
                            controls=[0],
                            params={"duration_us": 0.2},
                        )
                    ],
                ),
            ],
        ),
        environment=environment,
        duration_us=0.4,
        time_steps=17,
        fidelity_threshold=0.9,
        model=GATE_AWARE_HAMILTONIAN_LINDBLAD_MODEL,
        evolution_method=evolution_method,
    )


def _finite_environment() -> EnvironmentConfig:
    return EnvironmentConfig(
        input_mode="physical",
        device_quality=1.0,
        temperature_mk=15.0,
        flux_noise_phi0=1e-6,
        qubit_frequency_ghz=5.0,
        t1_max_us=100.0,
        tphi_max_us=100.0,
    )


def _physical_payload(evolution_method: str) -> dict[str, object]:
    return {
        "circuit_preset": "bell",
        "simulation_backend": "python_dense",
        "evolution_method": evolution_method,
        "input_mode": "physical",
        "parameters": {
            "device_quality": 0.8,
            "temperature_mk": 15.0,
            "flux_noise_phi0": 1e-6,
            "qubit_frequency_ghz": 5.0,
            "t1_max_us": 100.0,
            "tphi_max_us": 100.0,
            "duration_us": 0.4,
            "time_steps": 9,
            "fidelity_threshold": 0.9,
        },
    }
