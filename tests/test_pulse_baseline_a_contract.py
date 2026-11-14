import math
import unittest

from pydantic import ValidationError

from api.main import app, pulse_simulate
from api.pulse_models import PulseSimulateRequest
from core.capabilities import (
    DRIVEN_TWO_LEVEL_RWA_EXPERIMENTAL_MODEL,
    SUPPORTED_PULSE_MODELS,
    SUPPORTED_SIMULATION_MODELS,
    core_capabilities,
)
from core.circuit_model import CircuitConfig
from core.pulse_contract import (
    DETUNING_CONVENTION,
    KET_ONE,
    KET_ZERO,
    POSITIVE_PHASE_CONVENTION,
    SIGMA_Y,
    detuning_rad_per_us,
    ghz_to_rad_per_us,
    mhz_to_rad_per_us,
)
from core.results import EnvironmentConfig, SimulationConfig
from core.validation import validate_simulation_config


class PulsePhysicalContractTests(unittest.TestCase):
    def test_basis_and_sigma_y_are_fixed(self) -> None:
        self.assertEqual(KET_ZERO, (1.0 + 0.0j, 0.0 + 0.0j))
        self.assertEqual(KET_ONE, (0.0 + 0.0j, 1.0 + 0.0j))
        self.assertEqual(
            SIGMA_Y,
            (
                (0.0 + 0.0j, -1.0j),
                (1.0j, 0.0 + 0.0j),
            ),
        )

    def test_frequency_conversions_use_angular_frequency_units(self) -> None:
        self.assertAlmostEqual(mhz_to_rad_per_us(1.0), 2.0 * math.pi)
        self.assertAlmostEqual(
            ghz_to_rad_per_us(5.0),
            2.0 * math.pi * 5000.0,
        )
        self.assertAlmostEqual(
            mhz_to_rad_per_us(-250.0),
            -1570.7963267948965,
        )

    def test_frequency_conversions_reject_non_finite_values(self) -> None:
        for value in (math.inf, -math.inf, math.nan):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    mhz_to_rad_per_us(value)

    def test_detuning_is_drive_minus_qubit(self) -> None:
        self.assertEqual(DETUNING_CONVENTION, "drive_minus_qubit")
        self.assertEqual(
            POSITIVE_PHASE_CONVENTION,
            "positive_x_toward_positive_y",
        )
        self.assertAlmostEqual(detuning_rad_per_us(12.0, 10.0), 2.0)
        self.assertAlmostEqual(detuning_rad_per_us(8.0, 10.0), -2.0)


class PulseCapabilityTests(unittest.TestCase):
    def test_pulse_model_is_separate_from_gate_simulation_models(self) -> None:
        self.assertIn(
            DRIVEN_TWO_LEVEL_RWA_EXPERIMENTAL_MODEL,
            SUPPORTED_PULSE_MODELS,
        )
        self.assertNotIn(
            DRIVEN_TWO_LEVEL_RWA_EXPERIMENTAL_MODEL,
            SUPPORTED_SIMULATION_MODELS,
        )
        self.assertIn(
            DRIVEN_TWO_LEVEL_RWA_EXPERIMENTAL_MODEL,
            core_capabilities()["supported_pulse_models"],
        )

    def test_gate_config_rejects_pulse_model(self) -> None:
        config = SimulationConfig(
            circuit=CircuitConfig.one_qubit_h(),
            environment=EnvironmentConfig(),
            model=DRIVEN_TWO_LEVEL_RWA_EXPERIMENTAL_MODEL,
        )

        issues = validate_simulation_config(config)

        self.assertTrue(
            any(issue.code == "UNSUPPORTED_MODEL" for issue in issues)
        )


class PulseRequestContractTests(unittest.TestCase):
    def test_physical_gaussian_request_is_accepted(self) -> None:
        request = PulseSimulateRequest.model_validate(
            _physical_gaussian_payload()
        )

        self.assertEqual(request.environment.input_mode, "physical")
        self.assertAlmostEqual(
            request.pulse.derived_pulse_duration_us,
            6.4,
        )

    def test_direct_rate_square_request_is_accepted(self) -> None:
        payload = {
            "model_id": DRIVEN_TWO_LEVEL_RWA_EXPERIMENTAL_MODEL,
            "initial_state": "1",
            "pulse": {
                "shape": "square",
                "amplitude_mode": "peak_amplitude",
                "peak_amplitude_rad_per_us": math.pi,
                "pulse_duration_us": 1.0,
                "phase_rad": math.pi / 2.0,
                "detuning_rad_per_us": -0.25,
            },
            "total_simulation_time_us": 2.0,
            "environment": {
                "input_mode": "direct_rates",
                "gamma_down_per_us": 0.02,
                "gamma_up_per_us": 0.003,
                "gamma_phi_per_us": 0.015,
            },
        }

        request = PulseSimulateRequest.model_validate(payload)

        self.assertEqual(request.environment.input_mode, "direct_rates")
        self.assertAlmostEqual(
            request.pulse.derived_pulse_duration_us,
            1.0,
        )

    def test_physical_mode_rejects_direct_rate_fields(self) -> None:
        payload = _physical_gaussian_payload()
        payload["environment"]["gamma_down_per_us"] = 0.02

        with self.assertRaises(ValidationError):
            PulseSimulateRequest.model_validate(payload)

    def test_direct_rate_mode_rejects_physical_fields(self) -> None:
        payload = _physical_gaussian_payload()
        payload["environment"] = {
            "input_mode": "direct_rates",
            "gamma_down_per_us": 0.02,
            "gamma_up_per_us": 0.003,
            "gamma_phi_per_us": 0.015,
            "temperature_mk": 20.0,
        }

        with self.assertRaises(ValidationError):
            PulseSimulateRequest.model_validate(payload)

    def test_environment_mode_requires_its_fields(self) -> None:
        payload = _physical_gaussian_payload()
        del payload["environment"]["tphi_us"]

        with self.assertRaises(ValidationError):
            PulseSimulateRequest.model_validate(payload)

    def test_amplitude_modes_are_mutually_exclusive(self) -> None:
        payload = _physical_gaussian_payload()
        payload["pulse"]["peak_amplitude_rad_per_us"] = math.pi

        with self.assertRaises(ValidationError):
            PulseSimulateRequest.model_validate(payload)

    def test_gaussian_timing_fields_are_not_redundant(self) -> None:
        payload = _physical_gaussian_payload()
        payload["pulse"]["pulse_duration_us"] = 6.4

        with self.assertRaises(ValidationError):
            PulseSimulateRequest.model_validate(payload)

    def test_pulse_duration_must_fit_total_time(self) -> None:
        payload = _physical_gaussian_payload()
        payload["total_simulation_time_us"] = 6.39

        with self.assertRaises(ValidationError):
            PulseSimulateRequest.model_validate(payload)

    def test_nonzero_drag_is_rejected_in_baseline_a(self) -> None:
        payload = _physical_gaussian_payload()
        payload["pulse"]["drag_beta_us"] = 0.1

        with self.assertRaises(ValidationError):
            PulseSimulateRequest.model_validate(payload)

    def test_snapshot_time_must_fit_total_time(self) -> None:
        payload = _physical_gaussian_payload()
        payload["snapshot_options"]["custom_times_us"] = [20.1]

        with self.assertRaises(ValidationError):
            PulseSimulateRequest.model_validate(payload)

    def test_unknown_top_level_fields_are_rejected(self) -> None:
        payload = _physical_gaussian_payload()
        payload["circuit_preset"] = "bell"

        with self.assertRaises(ValidationError):
            PulseSimulateRequest.model_validate(payload)

    def test_endpoint_executes_the_frozen_baseline_a_contract(self) -> None:
        request = PulseSimulateRequest.model_validate(
            _physical_gaussian_payload()
        )

        response = pulse_simulate(request)

        self.assertEqual(response["contract_version"], "pulse-baseline-a-v1")
        self.assertEqual(
            response["model"]["model_id"],
            DRIVEN_TWO_LEVEL_RWA_EXPERIMENTAL_MODEL,
        )
        self.assertIn("pulse_end", response)
        self.assertIn("final", response)

    def test_openapi_contains_dedicated_pulse_path(self) -> None:
        operation = app.openapi()["paths"]["/api/pulse/simulate"]["post"]

        self.assertIn("200", operation["responses"])
        self.assertNotIn("501", operation["responses"])
        request_schema = operation["requestBody"]["content"][
            "application/json"
        ]["schema"]
        self.assertIn("oneOf", request_schema)
        self.assertIn("discriminator", request_schema)
        self.assertEqual(
            request_schema["discriminator"]["propertyName"],
            "model_id",
        )
        response_schema = operation["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        self.assertEqual(
            {
                item["$ref"] for item in response_schema["anyOf"]
            },
            {
                "#/components/schemas/PulseSimulateResponse",
                "#/components/schemas/QutritPulseSimulateResponse",
                "#/components/schemas/CoupledTransmonNetworkPulseSimulateResponse",
            },
        )


def _physical_gaussian_payload() -> dict[str, object]:
    return {
        "model_id": DRIVEN_TWO_LEVEL_RWA_EXPERIMENTAL_MODEL,
        "initial_state": "0",
        "pulse": {
            "shape": "gaussian",
            "amplitude_mode": "target_rotation_angle",
            "target_rotation_angle_rad": math.pi,
            "sigma_us": 0.8,
            "truncation_sigma": 4.0,
            "phase_rad": 0.0,
            "detuning_rad_per_us": 0.0,
            "drag_beta_us": 0.0,
        },
        "total_simulation_time_us": 20.0,
        "environment": {
            "input_mode": "physical",
            "t1_zero_temperature_us": 100.0,
            "tphi_us": 100.0,
            "temperature_mk": 20.0,
            "qubit_frequency_ghz": 5.0,
        },
        "snapshot_options": {
            "uniform_count": 101,
            "custom_times_us": [0.0, 6.4, 20.0],
        },
    }


if __name__ == "__main__":
    unittest.main()
