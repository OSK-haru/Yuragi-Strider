from contextlib import ExitStack
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from api.main import SimulateRequest, build_config_from_simulate_request, simulate


class ApiSimulateInputModesTest(unittest.TestCase):
    def test_removed_inputs_are_rejected(self) -> None:
        for parameters in ({"normalized_temperature": 0.2, "noise_level": 0.4},
                           {"device_quality": 0.8}, {"flux_noise_phi0": 1e-6}, {"t1_max_us": 100.0}):
            with self.subTest(parameters=parameters), self.assertRaises(ValidationError):
                SimulateRequest(
                    circuit_preset="bell",
                    simulation_backend="python_dense",
                    parameters={"temperature_mk": 15.0, "qubit_frequency_ghz": 5.0,
                                "t1_zero_temperature_us": 40.0, "tphi_us": 33.0,
                                "duration_us": 2.0, "time_steps": 11, "fidelity_threshold": 0.9,
                                **parameters},
                )

    def test_physical_payload_builds_physical_environment(self) -> None:
        request = SimulateRequest(
            circuit_preset="bell",
            simulation_backend="python_dense",
            parameters={
                "t1_zero_temperature_us": 39.810717055349734,
                "tphi_us": 33.20178309308936,
                "temperature_mk": 15.0,
                "qubit_frequency_ghz": 5.0,
                "duration_us": 2.0,
                "time_steps": 11,
                "fidelity_threshold": 0.9,
            },
        )

        config = build_config_from_simulate_request(request)

        self.assertAlmostEqual(config.environment.temperature_mk, 15.0)
        self.assertAlmostEqual(config.environment.qubit_frequency_ghz, 5.0)
        self.assertAlmostEqual(config.environment.t1_zero_temperature_us, 39.810717055349734)
        self.assertAlmostEqual(config.environment.tphi_us, 33.20178309308936)

    def test_gate_duration_defaults_override_bell_preset_gate_durations(self) -> None:
        request = SimulateRequest(
            circuit_preset="bell",
            simulation_backend="python_dense",
            gate_duration_defaults={
                "H": 0.05,
                "X": 0.02,
                "Z": 0.0,
                "CNOT": 0.4,
                "MEASURE": 0.0,
            },
            parameters={
                "t1_zero_temperature_us": 39.810717055349734,
                "tphi_us": 33.20178309308936,
                "temperature_mk": 15.0,
                "qubit_frequency_ghz": 5.0,
                "duration_us": 2.0,
                "time_steps": 11,
                "fidelity_threshold": 0.9,
            },
        )

        config = build_config_from_simulate_request(request)

        h_gate = config.circuit.columns[0].gates[0]
        cnot_gate = config.circuit.columns[1].gates[0]
        self.assertAlmostEqual(h_gate.params["duration_us"], 0.05)
        self.assertAlmostEqual(cnot_gate.params["duration_us"], 0.4)

    def test_gate_duration_defaults_reject_zero_hamiltonian_gate_duration(self) -> None:
        with self.assertRaises(ValidationError):
            SimulateRequest(
                circuit_preset="bell",
                simulation_backend="python_dense",
                gate_duration_defaults={
                    "H": 0.0,
                    "X": 0.02,
                    "Z": 0.0,
                    "CNOT": 0.2,
                    "MEASURE": 0.0,
                },
                parameters={
                    "t1_zero_temperature_us": 39.810717055349734,
                    "tphi_us": 33.20178309308936,
                    "temperature_mk": 15.0,
                    "qubit_frequency_ghz": 5.0,
                    "duration_us": 2.0,
                    "time_steps": 11,
                    "fidelity_threshold": 0.9,
                },
            )

    def test_simulate_accepts_physical_payload_with_rate_details(self) -> None:
        request = SimulateRequest(
            circuit_preset="bell",
            simulation_backend="python_dense",
            parameters={
                "t1_zero_temperature_us": 39.810717055349734,
                "tphi_us": 33.20178309308936,
                "temperature_mk": 15.0,
                "qubit_frequency_ghz": 5.0,
                "duration_us": 2.0,
                "time_steps": 11,
                "fidelity_threshold": 0.9,
            },
        )

        response = simulate(request)

        self.assertIn("circuit", response)
        self.assertIn("parameters", response)
        self.assertIn("rates", response)
        self.assertIn("diagnostics", response)
        self.assertIn("summary", response)
        self.assertIn("timeline", response)
        self.assertIn("output_probabilities", response)
        self.assertIn("run", response)
        self.assertIn("warnings", response)
        self.assertIn("issues", response)
        self.assertAlmostEqual(response["parameters"]["tphi_us"], 33.20178309308936)
        self.assertIn("gamma_down_per_us", response["rates"])
        self.assertIn("gamma_up_per_us", response["rates"])
        self.assertIn("gamma_population_relaxation_per_us", response["rates"])
        self.assertNotIn("gamma1_per_us", response["rates"])
        self.assertIn("api_total_request_ms", response["diagnostics"])
        self.assertIn("api_build_config_ms", response["diagnostics"])
        self.assertIn("api_run_simulation_ms", response["diagnostics"])
        self.assertIn("api_ui_response_ms", response["diagnostics"])
        self.assertIn("api_logical_qubits", response["diagnostics"])

    def test_simulate_returns_structured_error_detail_on_failure(self) -> None:
        # str(exc) can carry file paths and internal state, so the raw message is
        # opt-in via EXPOSE_INTERNAL_ERRORS. The structured envelope is always
        # present; only the "error" field is withheld by default.
        detail = self._failing_simulate_detail()

        self.assertIsInstance(detail, dict)
        self.assertEqual(detail["message"], "Simulation failed.")
        self.assertEqual(detail["error_type"], "ValueError")

    def test_simulate_withholds_the_raw_message_by_default(self) -> None:
        detail = self._failing_simulate_detail(expose_internal_errors=False)

        self.assertNotIn("error", detail)

    def test_simulate_includes_the_raw_message_when_errors_are_exposed(self) -> None:
        detail = self._failing_simulate_detail(expose_internal_errors=True)

        self.assertIn("boom", str(detail["error"]))

    def _failing_simulate_detail(
        self,
        expose_internal_errors: bool | None = None,
    ) -> dict[str, object]:
        request = SimulateRequest(
            circuit_preset="bell",
            simulation_backend="python_dense",
            parameters={
                "t1_zero_temperature_us": 39.810717055349734,
                "tphi_us": 33.20178309308936,
                "temperature_mk": 15.0,
                "qubit_frequency_ghz": 5.0,
                "duration_us": 2.0,
                "time_steps": 11,
                "fidelity_threshold": 0.9,
            },
        )

        with ExitStack() as stack:
            stack.enter_context(
                patch("api.main.run_simulation", side_effect=ValueError("boom"))
            )
            if expose_internal_errors is not None:
                stack.enter_context(
                    patch("api.main.EXPOSE_INTERNAL_ERRORS", expose_internal_errors)
                )
            context = stack.enter_context(self.assertRaises(HTTPException))
            simulate(request)

        return context.exception.detail

    def test_payload_requires_every_environment_field(self) -> None:
        with self.assertRaises(ValidationError):
            SimulateRequest(
                circuit_preset="bell",
                simulation_backend="python_dense",
                parameters={
                    "t1_zero_temperature_us": 39.810717055349734,
                    "temperature_mk": 15.0,
                    "qubit_frequency_ghz": 5.0,
                    "duration_us": 2.0,
                    "time_steps": 11,
                    "fidelity_threshold": 0.9,
                },
            )


if __name__ == "__main__":
    unittest.main()
