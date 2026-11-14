import asyncio
import json
import math
import unittest

from pydantic import TypeAdapter, ValidationError

from api.main import app
from api.pulse_models import (
    PulseApiRequest,
    PulseSimulateRequest,
    QutritPulseSimulateRequest,
)
from core.capabilities import (
    DECLARED_PULSE_MODELS,
    DRIVEN_TRANSMON_QUTRIT_RWA_EXPERIMENTAL_MODEL,
    DRIVEN_TWO_LEVEL_RWA_EXPERIMENTAL_MODEL,
    PULSE_MODEL_STATUSES,
    SUPPORTED_PULSE_MODELS,
    core_capabilities,
)
from core.gates import adjoint, matmul
from core.pulse_qutrit_contract import (
    ANNIHILATION_QUTRIT,
    CREATION_QUTRIT,
    KET_ONE_QUTRIT,
    KET_TWO_QUTRIT,
    NUMBER_QUTRIT,
    QUTRIT_SUBSYSTEM_DIMENSIONS,
    qutrit_rotating_frame_hamiltonian,
    transition_12_frequency_ghz,
    transmon_anharmonicity_rad_per_us,
)


class PulseB0QutritOperatorTests(unittest.TestCase):
    def test_qutrit_operator_contract(self) -> None:
        self.assertEqual(
            CREATION_QUTRIT,
            adjoint(ANNIHILATION_QUTRIT),
        )
        computed_number = matmul(
            CREATION_QUTRIT,
            ANNIHILATION_QUTRIT,
        )
        for expected_row, computed_row in zip(
            NUMBER_QUTRIT,
            computed_number,
            strict=True,
        ):
            for expected, computed in zip(
                expected_row,
                computed_row,
                strict=True,
            ):
                self.assertAlmostEqual(expected, computed)
        self.assertEqual(QUTRIT_SUBSYSTEM_DIMENSIONS, (3,))

    def test_annihilation_and_number_actions(self) -> None:
        self.assertEqual(
            _matrix_vector(ANNIHILATION_QUTRIT, KET_ONE_QUTRIT),
            (1.0 + 0.0j, 0.0 + 0.0j, 0.0 + 0.0j),
        )
        lowered_two = _matrix_vector(
            ANNIHILATION_QUTRIT,
            KET_TWO_QUTRIT,
        )
        self.assertAlmostEqual(lowered_two[1].real, math.sqrt(2.0))
        self.assertEqual(
            _matrix_vector(NUMBER_QUTRIT, KET_TWO_QUTRIT),
            (0.0 + 0.0j, 0.0 + 0.0j, 2.0 + 0.0j),
        )

    def test_anharmonicity_unit_conversion(self) -> None:
        self.assertAlmostEqual(
            transmon_anharmonicity_rad_per_us(-250.0),
            -1570.7963267948965,
        )

    def test_transition_12_frequency_contract(self) -> None:
        self.assertAlmostEqual(
            transition_12_frequency_ghz(5.0, -250.0),
            4.75,
        )
        with self.assertRaises(ValueError):
            transition_12_frequency_ghz(0.1, -200.0)
        with self.assertRaises(ValueError):
            transition_12_frequency_ghz(5.0, 250.0)

    def test_qutrit_hamiltonian_is_hermitian(self) -> None:
        hamiltonian = qutrit_rotating_frame_hamiltonian(
            0.7,
            transmon_anharmonicity_rad_per_us(-250.0),
            1.2,
            -0.4,
        )
        self.assertEqual(hamiltonian, adjoint(hamiltonian))

    def test_two_level_block_matches_baseline_up_to_global_shift(self) -> None:
        detuning = 0.7
        omega_x = 1.2
        omega_y = -0.4
        hamiltonian = qutrit_rotating_frame_hamiltonian(
            detuning,
            transmon_anharmonicity_rad_per_us(-250.0),
            omega_x,
            omega_y,
        )
        shifted_block = (
            (
                hamiltonian[0][0] + detuning / 2.0,
                hamiltonian[0][1],
            ),
            (
                hamiltonian[1][0],
                hamiltonian[1][1] + detuning / 2.0,
            ),
        )
        expected = (
            (
                detuning / 2.0 + 0.0j,
                complex(omega_x, -omega_y) / 2.0,
            ),
            (
                complex(omega_x, omega_y) / 2.0,
                -detuning / 2.0 + 0.0j,
            ),
        )
        self.assertEqual(shifted_block, expected)


class PulseB0QutritRequestTests(unittest.TestCase):
    def test_qutrit_model_is_declared_and_available_after_b5(self) -> None:
        self.assertIn(
            DRIVEN_TRANSMON_QUTRIT_RWA_EXPERIMENTAL_MODEL,
            DECLARED_PULSE_MODELS,
        )
        self.assertIn(
            DRIVEN_TRANSMON_QUTRIT_RWA_EXPERIMENTAL_MODEL,
            SUPPORTED_PULSE_MODELS,
        )
        self.assertEqual(
            PULSE_MODEL_STATUSES[
                DRIVEN_TRANSMON_QUTRIT_RWA_EXPERIMENTAL_MODEL
            ],
            "available",
        )
        capabilities = core_capabilities()
        self.assertEqual(
            capabilities["pulse_model_statuses"][
                DRIVEN_TRANSMON_QUTRIT_RWA_EXPERIMENTAL_MODEL
            ],
            "available",
        )

    def test_discriminator_preserves_baseline_request_type(self) -> None:
        request = TypeAdapter(PulseApiRequest).validate_python(
            _baseline_payload()
        )
        self.assertIsInstance(request, PulseSimulateRequest)
        self.assertEqual(
            request.model_id,
            DRIVEN_TWO_LEVEL_RWA_EXPERIMENTAL_MODEL,
        )

    def test_discriminator_selects_qutrit_request_type(self) -> None:
        request = TypeAdapter(PulseApiRequest).validate_python(
            _qutrit_physical_payload()
        )
        self.assertIsInstance(request, QutritPulseSimulateRequest)
        self.assertEqual(request.initial_state, "2")
        self.assertEqual(request.anharmonicity_mhz, -250.0)

    def test_qutrit_direct_rate_contract(self) -> None:
        payload = _qutrit_physical_payload()
        payload["environment"] = {
            "input_mode": "direct_rates",
            "gamma_10_down_per_us": 0.1,
            "gamma_01_up_per_us": 0.01,
            "gamma_21_down_per_us": 0.2,
            "gamma_12_up_per_us": 0.02,
            "gamma_phi_adjacent_per_us": 0.03,
        }
        request = QutritPulseSimulateRequest.model_validate(payload)
        self.assertEqual(request.environment.input_mode, "direct_rates")

    def test_invalid_qutrit_frequency_and_anharmonicity_are_rejected(
        self,
    ) -> None:
        payload = _qutrit_physical_payload()
        payload["environment"]["qubit_frequency_ghz"] = 0.1
        payload["anharmonicity_mhz"] = -200.0
        with self.assertRaises(ValidationError):
            QutritPulseSimulateRequest.model_validate(payload)

        payload = _qutrit_physical_payload()
        payload["anharmonicity_mhz"] = 250.0
        with self.assertRaises(ValidationError):
            QutritPulseSimulateRequest.model_validate(payload)

    def test_invalid_qutrit_state_and_square_drag_are_rejected(self) -> None:
        payload = _qutrit_physical_payload()
        payload["initial_state"] = "3"
        with self.assertRaises(ValidationError):
            QutritPulseSimulateRequest.model_validate(payload)

        payload = _qutrit_physical_payload()
        payload["pulse"] = {
            "shape": "square",
            "amplitude_mode": "target_rotation_angle",
            "target_rotation_angle_rad": math.pi,
            "pulse_duration_us": 0.1,
            "drag_beta_us": 0.1,
        }
        with self.assertRaises(ValidationError):
            QutritPulseSimulateRequest.model_validate(payload)

    def test_gaussian_qutrit_drag_is_valid_but_http_stays_unexposed(
        self,
    ) -> None:
        payload = _qutrit_physical_payload()
        payload["pulse"]["drag_beta_us"] = 0.1
        request = QutritPulseSimulateRequest.model_validate(payload)
        self.assertEqual(request.pulse.drag_beta_us, 0.1)

    def test_qutrit_fields_remain_forbidden_on_baseline_a(self) -> None:
        payload = _baseline_payload()
        payload["anharmonicity_mhz"] = -250.0
        with self.assertRaises(ValidationError):
            PulseSimulateRequest.model_validate(payload)

    def test_http_qutrit_contract_rejects_over_budget_fixture(self) -> None:
        status_code, response = asyncio.run(_asgi_post_json(
            app,
            "/api/pulse/simulate",
            _qutrit_physical_payload(),
        ))
        self.assertEqual(status_code, 422)
        self.assertIn("detail", response)
        self.assertIn("execution limits", str(response["detail"]))

    def test_openapi_exposes_discriminated_pulse_models(self) -> None:
        operation = app.openapi()["paths"]["/api/pulse/simulate"]["post"]
        request_schema = operation["requestBody"]["content"][
            "application/json"
        ]["schema"]
        self.assertIn("oneOf", request_schema)
        self.assertIn("discriminator", request_schema)
        self.assertNotIn("501", operation["responses"])


def _matrix_vector(
    matrix: tuple[tuple[complex, ...], ...],
    vector: tuple[complex, ...],
) -> tuple[complex, ...]:
    return tuple(
        sum(value * vector[index] for index, value in enumerate(row))
        for row in matrix
    )


async def _asgi_post_json(asgi_app, path, payload):
    body = json.dumps(payload).encode("utf-8")
    request_sent = False
    messages = []

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {
                "type": "http.request",
                "body": body,
                "more_body": False,
            }
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await asgi_app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [
                (b"host", b"testserver"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        },
        receive,
        send,
    )
    status_code = next(
        message["status"]
        for message in messages
        if message["type"] == "http.response.start"
    )
    response_body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return status_code, json.loads(response_body)


def _baseline_payload() -> dict[str, object]:
    return {
        "model_id": DRIVEN_TWO_LEVEL_RWA_EXPERIMENTAL_MODEL,
        "initial_state": "0",
        "pulse": {
            "shape": "square",
            "amplitude_mode": "target_rotation_angle",
            "target_rotation_angle_rad": math.pi,
            "pulse_duration_us": 0.2,
        },
        "total_simulation_time_us": 0.2,
        "environment": {
            "input_mode": "direct_rates",
            "gamma_down_per_us": 0.0,
            "gamma_up_per_us": 0.0,
            "gamma_phi_per_us": 0.0,
        },
    }


def _qutrit_physical_payload() -> dict[str, object]:
    return {
        "model_id": DRIVEN_TRANSMON_QUTRIT_RWA_EXPERIMENTAL_MODEL,
        "initial_state": "2",
        "anharmonicity_mhz": -250.0,
        "pulse": {
            "shape": "gaussian",
            "amplitude_mode": "target_rotation_angle",
            "target_rotation_angle_rad": math.pi,
            "sigma_us": 0.02,
            "truncation_sigma": 4.0,
            "phase_rad": 0.0,
            "detuning_rad_per_us": 0.0,
            "drag_beta_us": 0.0,
        },
        "total_simulation_time_us": 0.5,
        "environment": {
            "input_mode": "physical",
            "t1_zero_temperature_us": 39.810717055349734,
            "tphi_us": 33.20178309308936,
            "temperature_mk": 15.0,
            "qubit_frequency_ghz": 5.0,
        },
        "snapshot_options": {
            "uniform_count": 21,
            "custom_times_us": [0.16, 0.5],
        },
    }


if __name__ == "__main__":
    unittest.main()
