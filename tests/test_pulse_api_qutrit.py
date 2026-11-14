import math
import unittest

from fastapi import HTTPException

from api.main import pulse_simulate
from api.pulse_models import (
    QutritPulseSimulateRequest,
    QutritPulseSimulateResponse,
)
from api.pulse_qutrit_service import QUTRIT_API_MAX_INTERNAL_STEPS
from core.capabilities import (
    DRIVEN_TRANSMON_QUTRIT_RWA_EXPERIMENTAL_MODEL,
)
from core.rust_dense_kernel import is_rust_kernel_available
from core.quasi_static_noise import gaussian_quasi_static_detuning_samples


class PulseQutritApiTests(unittest.TestCase):
    def test_gauss_hermite_samples_reproduce_gaussian_moments(self) -> None:
        sigma = 2.5
        samples = gaussian_quasi_static_detuning_samples(sigma, 5)
        self.assertAlmostEqual(sum(weight for _, weight in samples), 1.0, places=14)
        self.assertAlmostEqual(
            sum(offset * weight for offset, weight in samples),
            0.0,
            places=14,
        )
        self.assertAlmostEqual(
            sum(offset * offset * weight for offset, weight in samples),
            sigma * sigma,
            places=12,
        )

    def test_direct_rate_qutrit_response_exposes_three_level_state(self) -> None:
        request = QutritPulseSimulateRequest.model_validate(
            _direct_payload()
        )
        response = pulse_simulate(request)
        validated = QutritPulseSimulateResponse.model_validate(response)

        self.assertEqual(validated.contract_version, "pulse-extension-b-v1")
        self.assertEqual(validated.model.state_levels, 3)
        self.assertEqual(validated.model.subsystem_dimensions, [3])
        self.assertEqual(len(validated.final.density_matrix), 3)
        self.assertEqual(
            len(validated.final.density_matrix[0]),
            3,
        )
        probability_sum = (
            validated.final.population_0
            + validated.final.population_1
            + validated.final.population_2
        )
        self.assertAlmostEqual(probability_sum, 1.0, places=10)
        self.assertEqual(
            validated.step_policy.maximum_internal_step_count,
            QUTRIT_API_MAX_INTERNAL_STEPS,
        )
        self.assertEqual(validated.diagnostics.backend.requested, "python")
        self.assertEqual(validated.diagnostics.backend.resolved, "python")

    @unittest.skipUnless(
        is_rust_kernel_available(),
        "yuragi_strider_rust is not importable",
    )
    def test_rust_qutrit_backend_matches_python_and_auto_resolves_rust(self) -> None:
        python_payload = _direct_payload()
        rust_payload = _direct_payload()
        auto_payload = _direct_payload()
        rust_payload["backend"] = "rust"
        auto_payload["backend"] = "auto"

        python_response = pulse_simulate(
            QutritPulseSimulateRequest.model_validate(python_payload)
        )
        rust_response = pulse_simulate(
            QutritPulseSimulateRequest.model_validate(rust_payload)
        )
        auto_response = pulse_simulate(
            QutritPulseSimulateRequest.model_validate(auto_payload)
        )

        self.assertEqual(rust_response["diagnostics"]["backend"], {
            "requested": "rust",
            "resolved": "rust",
            "fallback_used": False,
        })
        self.assertEqual(auto_response["diagnostics"]["backend"], {
            "requested": "auto",
            "resolved": "rust",
            "fallback_used": False,
        })
        for row_python, row_rust in zip(
            python_response["final"]["density_matrix"],
            rust_response["final"]["density_matrix"],
        ):
            for value_python, value_rust in zip(row_python, row_rust):
                self.assertAlmostEqual(
                    value_python["real"], value_rust["real"], places=12
                )
                self.assertAlmostEqual(
                    value_python["imag"], value_rust["imag"], places=12
                )

    def test_physical_environment_rates_are_returned(self) -> None:
        payload = _direct_payload()
        payload["environment"] = {
            "input_mode": "physical",
            "t1_zero_temperature_us": 39.810717055349734,
            "tphi_us": 33.20178309308936,
            "temperature_mk": 15.0,
            "qubit_frequency_ghz": 5.0,
        }
        response = pulse_simulate(
            QutritPulseSimulateRequest.model_validate(payload)
        )
        self.assertEqual(response["rates"]["input_mode"], "physical")
        self.assertIsNotNone(response["rates"]["n_01"])
        self.assertIsNotNone(response["rates"]["n_12"])

    def test_gaussian_drag_is_executable(self) -> None:
        payload = _direct_payload()
        payload["pulse"]["drag_beta_us"] = 0.001
        response = pulse_simulate(
            QutritPulseSimulateRequest.model_validate(payload)
        )
        self.assertEqual(response["input"]["drag_beta_us"], 0.001)
        self.assertLessEqual(
            response["step_policy"]["estimated_internal_step_count"],
            QUTRIT_API_MAX_INTERNAL_STEPS,
        )

    def test_quasi_static_detuning_returns_ensemble_density_matrix(self) -> None:
        payload = _direct_payload()
        payload["quasi_static_noise"] = {
            "enabled": True,
            "sigma_detuning_rad_per_us": 20.0,
            "quadrature_order": 3,
        }
        response = pulse_simulate(
            QutritPulseSimulateRequest.model_validate(payload)
        )

        self.assertTrue(response["input"]["quasi_static_noise_enabled"])
        self.assertEqual(response["input"]["quasi_static_quadrature_order"], 3)
        diagnostics = response["diagnostics"]["quasi_static_noise"]
        self.assertEqual(diagnostics["quadrature_method"], "Gauss-Hermite")
        self.assertEqual(len(diagnostics["samples"]), 3)
        self.assertAlmostEqual(
            sum(
                response["final"][f"population_{index}"]
                for index in range(3)
            ),
            1.0,
            places=10,
        )
        self.assertLessEqual(
            response["step_policy"]["estimated_internal_step_count"],
            QUTRIT_API_MAX_INTERNAL_STEPS,
        )

    def test_quasi_static_detuning_supports_explicit_cptp(self) -> None:
        payload = _direct_payload()
        payload["evolution_method"] = "explicit_cptp"
        payload["snapshot_options"] = {
            "uniform_count": 3,
            "custom_times_us": [0.016],
        }
        payload["quasi_static_noise"] = {
            "enabled": True,
            "sigma_detuning_rad_per_us": 1.0,
            "quadrature_order": 3,
        }
        response = pulse_simulate(
            QutritPulseSimulateRequest.model_validate(payload)
        )

        audit = response["diagnostics"]["evolution"]["open_pulse_audit"]
        self.assertIsNotNone(audit)
        self.assertTrue(audit["all_maps_cptp"])
        self.assertGreater(audit["map_count"], 0)

    def test_density_matrix_can_be_handed_to_the_next_pulse(self) -> None:
        first_payload = _direct_payload()
        first_response = pulse_simulate(
            QutritPulseSimulateRequest.model_validate(first_payload)
        )

        second_payload = _direct_payload()
        second_payload["initial_density_matrix"] = first_response["final"][
            "density_matrix"
        ]
        second_response = pulse_simulate(
            QutritPulseSimulateRequest.model_validate(second_payload)
        )

        self.assertEqual(
            second_response["input"]["initial_state_source"],
            "density_matrix",
        )
        self.assertAlmostEqual(
            sum(
                second_response["final"]["density_matrix"][index][index]["real"]
                for index in range(3)
            ),
            1.0,
            places=10,
        )

    def test_heavy_qutrit_request_is_rejected_before_execution(self) -> None:
        payload = _direct_payload()
        payload["anharmonicity_mhz"] = -250.0
        payload["pulse"]["sigma_us"] = 0.02
        payload["total_simulation_time_us"] = 0.5
        with self.assertRaises(HTTPException) as context:
            pulse_simulate(
                QutritPulseSimulateRequest.model_validate(payload)
            )
        self.assertEqual(context.exception.status_code, 422)
        self.assertIn(
            "internal-step limit",
            str(context.exception.detail),
        )


def _direct_payload() -> dict[str, object]:
    return {
        "model_id": DRIVEN_TRANSMON_QUTRIT_RWA_EXPERIMENTAL_MODEL,
        "initial_state": "0",
        "anharmonicity_mhz": -100.0,
        "pulse": {
            "shape": "gaussian",
            "amplitude_mode": "target_rotation_angle",
            "target_rotation_angle_rad": math.pi / 2.0,
            "sigma_us": 0.002,
            "truncation_sigma": 4.0,
            "phase_rad": 0.2,
            "detuning_rad_per_us": 0.1,
            "drag_beta_us": 0.0,
        },
        "total_simulation_time_us": 0.02,
        "environment": {
            "input_mode": "direct_rates",
            "gamma_10_down_per_us": 0.2,
            "gamma_01_up_per_us": 0.02,
            "gamma_21_down_per_us": 0.4,
            "gamma_12_up_per_us": 0.03,
            "gamma_phi_adjacent_per_us": 0.08,
        },
        "snapshot_options": {
            "uniform_count": 9,
            "custom_times_us": [0.016, 0.02],
        },
    }


if __name__ == "__main__":
    unittest.main()
