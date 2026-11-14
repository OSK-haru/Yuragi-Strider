import asyncio
import json
import math
import unittest
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextlib import ExitStack
from unittest.mock import patch

from fastapi import HTTPException

from api.main import (
    PULSE_API_MAX_CONCURRENT_REQUESTS,
    _PULSE_EXECUTION_SLOTS,
    app,
    pulse_simulate,
)
from api.pulse_models import PulseSimulateRequest, PulseSimulateResponse
from api.pulse_service import (
    PULSE_API_MAX_INTERNAL_STEPS,
    run_pulse_request,
)
from core.rust_dense_kernel import is_rust_kernel_available


class PulseBaselineAApiTests(unittest.TestCase):
    def test_http_success_smoke(self) -> None:
        status_code, response = asyncio.run(_asgi_post_json(
            app,
            "/api/pulse/simulate",
            _direct_square_payload(),
        ))

        self.assertEqual(status_code, 200)
        self.assertEqual(
            response["contract_version"],
            "pulse-baseline-a-v1",
        )
        self.assertEqual(
            response["model"]["model_id"],
            "driven_two_level_rwa_experimental_v1",
        )

    def test_http_rejection_smoke(self) -> None:
        payload = _direct_square_payload()
        payload["model_id"] = "unsupported-pulse-model"

        status_code, _ = asyncio.run(_asgi_post_json(
            app,
            "/api/pulse/simulate",
            payload,
        ))

        self.assertEqual(status_code, 422)

    def test_direct_rate_square_response_contract(self) -> None:
        request = PulseSimulateRequest.model_validate(
            _direct_square_payload()
        )

        response = pulse_simulate(request)
        parsed = PulseSimulateResponse.model_validate(response)

        self.assertEqual(parsed.contract_version, "pulse-baseline-a-v1")
        self.assertEqual(parsed.model.frame, "rotating")
        self.assertEqual(parsed.model.approximation, "RWA")
        self.assertEqual(parsed.model.internal_units.time, "us")
        self.assertEqual(
            parsed.model.internal_units.angular_frequency,
            "rad/us",
        )
        self.assertTrue(parsed.model.experimental)
        self.assertFalse(parsed.model.hardware_calibrated)
        self.assertEqual(parsed.rates.input_mode, "direct_rates")
        self.assertAlmostEqual(parsed.input.pulse_duration_us, 0.2)
        self.assertAlmostEqual(parsed.input.idle_duration_us, 1.0)
        self.assertEqual(parsed.sample_times_us, sorted(
            set(parsed.sample_times_us)
        ))
        self.assertEqual(len(parsed.trajectory), len(parsed.sample_times_us))
        self.assertLessEqual(
            parsed.step_policy.estimated_internal_steps,
            PULSE_API_MAX_INTERNAL_STEPS,
        )
        self.assertGreater(
            parsed.pulse_end.open_population_1,
            parsed.final.open_population_1,
        )
        self.assertGreater(parsed.pulse_end.time_us, 0.0)
        self.assertAlmostEqual(parsed.final.time_us, 1.2)
        self.assertEqual(len(parsed.final.open_density_matrix), 2)
        self.assertEqual(len(parsed.final.open_density_matrix[0]), 2)
        self.assertEqual(parsed.diagnostics.backend.requested, "python")
        self.assertEqual(parsed.diagnostics.backend.resolved, "python")
        self.assertFalse(parsed.diagnostics.backend.fallback_used)

    @unittest.skipUnless(
        is_rust_kernel_available(),
        "yuragi_strider_rust is not importable",
    )
    def test_rust_and_auto_backends_report_resolution_and_match_python(self) -> None:
        python_payload = _direct_square_payload()
        rust_payload = _direct_square_payload()
        auto_payload = _direct_square_payload()
        rust_payload["backend"] = "rust"
        auto_payload["backend"] = "auto"

        python_response = run_pulse_request(
            PulseSimulateRequest.model_validate(python_payload)
        )
        rust_response = run_pulse_request(
            PulseSimulateRequest.model_validate(rust_payload)
        )
        auto_response = run_pulse_request(
            PulseSimulateRequest.model_validate(auto_payload)
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
            python_response["final"]["open_density_matrix"],
            rust_response["final"]["open_density_matrix"],
        ):
            for value_python, value_rust in zip(row_python, row_rust):
                self.assertAlmostEqual(
                    value_python["real"], value_rust["real"], places=12
                )
                self.assertAlmostEqual(
                    value_python["imag"], value_rust["imag"], places=12
                )

    def test_auto_backend_falls_back_to_python_when_rust_is_unavailable(self) -> None:
        payload = _direct_square_payload()
        payload["backend"] = "auto"

        with patch(
            "core.pulse_evolution.is_rust_kernel_available",
            return_value=False,
        ):
            response = run_pulse_request(
                PulseSimulateRequest.model_validate(payload)
            )

        self.assertEqual(response["diagnostics"]["backend"], {
            "requested": "auto",
            "resolved": "python",
            "fallback_used": True,
        })

    def test_backend_selection_is_written_to_structured_runtime_log(self) -> None:
        payload = _direct_square_payload()
        payload["backend"] = "auto"

        with self.assertLogs("api.pulse_backend_logging", level="INFO") as captured:
            run_pulse_request(PulseSimulateRequest.model_validate(payload))

        self.assertEqual(len(captured.records), 1)
        self.assertEqual(captured.records[0].message, "pulse_backend_selected")
        self.assertEqual(captured.records[0].pulse_backend, {
            "model_id": "driven_two_level_rwa_experimental_v1",
            "requested": "auto",
            "resolved": "rust" if is_rust_kernel_available() else "python",
            "fallback_used": not is_rust_kernel_available(),
        })

    def test_physical_gaussian_reports_derived_rates(self) -> None:
        request = PulseSimulateRequest.model_validate(
            _physical_gaussian_payload()
        )

        response = run_pulse_request(request)
        rates = response["rates"]

        self.assertEqual(rates["input_mode"], "physical")
        self.assertGreater(rates["gamma_down_per_us"], 0.0)
        self.assertGreaterEqual(rates["gamma_up_per_us"], 0.0)
        self.assertGreater(rates["t1_effective_us"], 0.0)
        self.assertGreater(rates["t2_effective_us"], 0.0)
        self.assertEqual(response["input"]["sample_count"], 21)

    def test_open_and_closed_trajectory_metrics_are_reported(self) -> None:
        request = PulseSimulateRequest.model_validate(
            _direct_square_payload()
        )

        response = run_pulse_request(request)

        for point in response["trajectory"]:
            self.assertIn("open_population_1", point)
            self.assertIn("closed_population_1", point)
            self.assertIn("fidelity_to_closed", point)
            self.assertIn("purity", point)
            self.assertIn("raw_physicality", point)
            self.assertIn("cleaned_physicality", point)
            self.assertGreaterEqual(point["fidelity_to_closed"], 0.0)
            self.assertLessEqual(point["fidelity_to_closed"], 1.0)

    def test_over_budget_request_is_rejected_with_422(self) -> None:
        payload = _direct_square_payload()
        payload["environment"] = {
            "input_mode": "direct_rates",
            "gamma_down_per_us": 1e9,
            "gamma_up_per_us": 1e9,
            "gamma_phi_per_us": 1e9,
        }
        request = PulseSimulateRequest.model_validate(payload)

        with self.assertRaises(HTTPException) as context:
            pulse_simulate(request)

        self.assertEqual(context.exception.status_code, 422)
        self.assertIn(
            "internal-step limit",
            context.exception.detail["error"],
        )

    def test_timeout_returns_504(self) -> None:
        request = PulseSimulateRequest.model_validate(
            _direct_square_payload()
        )
        fake_future = _TimeoutFuture()

        with patch(
            "api.main._PULSE_EXECUTOR.submit",
            return_value=fake_future,
        ):
            with self.assertRaises(HTTPException) as context:
                pulse_simulate(request)

        self.assertEqual(context.exception.status_code, 504)
        self.assertTrue(
            context.exception.detail["previous_results_preserved"]
        )

    def test_busy_capacity_returns_503(self) -> None:
        acquired = [
            _PULSE_EXECUTION_SLOTS.acquire(blocking=False)
            for _ in range(PULSE_API_MAX_CONCURRENT_REQUESTS)
        ]
        self.assertTrue(all(acquired))
        try:
            request = PulseSimulateRequest.model_validate(
                _direct_square_payload()
            )
            with self.assertRaises(HTTPException) as context:
                pulse_simulate(request)
            self.assertEqual(context.exception.status_code, 503)
        finally:
            for did_acquire in acquired:
                if did_acquire:
                    _PULSE_EXECUTION_SLOTS.release()

    def test_execution_failure_returns_structured_500(self) -> None:
        # str(exc) can carry file paths and internal state, so the raw message is
        # opt-in via EXPOSE_INTERNAL_ERRORS. The structured envelope is always
        # present; only the "error" field is withheld by default.
        status_code, detail = self._failing_pulse_detail()

        self.assertEqual(status_code, 500)
        self.assertEqual(detail["error_type"], "RuntimeError")

    def test_execution_failure_withholds_the_raw_message_by_default(self) -> None:
        _, detail = self._failing_pulse_detail(expose_internal_errors=False)

        self.assertNotIn("error", detail)

    def test_execution_failure_includes_the_raw_message_when_exposed(self) -> None:
        _, detail = self._failing_pulse_detail(expose_internal_errors=True)

        self.assertIn("pulse boom", detail["error"])

    def _failing_pulse_detail(
        self,
        expose_internal_errors: bool | None = None,
    ) -> tuple[int, dict[str, object]]:
        request = PulseSimulateRequest.model_validate(_direct_square_payload())

        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "api.main.run_pulse_request",
                    side_effect=RuntimeError("pulse boom"),
                )
            )
            if expose_internal_errors is not None:
                stack.enter_context(
                    patch("api.main.EXPOSE_INTERNAL_ERRORS", expose_internal_errors)
                )
            context = stack.enter_context(self.assertRaises(HTTPException))
            pulse_simulate(request)

        return context.exception.status_code, context.exception.detail


class _TimeoutFuture:
    def __init__(self) -> None:
        self._callback = None

    def add_done_callback(self, callback) -> None:
        self._callback = callback

    def result(self, timeout=None):
        del timeout
        raise FuturesTimeoutError()

    def cancel(self) -> bool:
        if self._callback is not None:
            self._callback(self)
        return True


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


def _direct_square_payload() -> dict[str, object]:
    return {
        "model_id": "driven_two_level_rwa_experimental_v1",
        "initial_state": "0",
        "pulse": {
            "shape": "square",
            "amplitude_mode": "target_rotation_angle",
            "target_rotation_angle_rad": math.pi,
            "pulse_duration_us": 0.2,
            "phase_rad": 0.0,
            "detuning_rad_per_us": 0.0,
            "drag_beta_us": 0.0,
        },
        "total_simulation_time_us": 1.2,
        "environment": {
            "input_mode": "direct_rates",
            "gamma_down_per_us": 0.1,
            "gamma_up_per_us": 0.02,
            "gamma_phi_per_us": 0.05,
        },
        "snapshot_options": {
            "uniform_count": 21,
            "custom_times_us": [0.2, 1.2],
        },
    }


def _physical_gaussian_payload() -> dict[str, object]:
    return {
        "model_id": "driven_two_level_rwa_experimental_v1",
        "initial_state": "0",
        "pulse": {
            "shape": "gaussian",
            "amplitude_mode": "target_rotation_angle",
            "target_rotation_angle_rad": math.pi / 2.0,
            "sigma_us": 0.2,
            "truncation_sigma": 4.0,
            "phase_rad": math.pi / 4.0,
            "detuning_rad_per_us": 0.25 * math.pi,
            "drag_beta_us": 0.0,
        },
        "total_simulation_time_us": 2.0,
        "environment": {
            "input_mode": "physical",
            "t1_zero_temperature_us": 39.810717055349734,
            "tphi_zero_temperature_us": 33.20178309308936,
            "readout_resonator_frequency_ghz": 7.0,
            "readout_resonator_kappa_mhz": 1.0,
            "readout_resonator_chi_mhz": 0.5,
            "temperature_mk": 15.0,
            "qubit_frequency_ghz": 5.0,
        },
        "snapshot_options": {
            "uniform_count": 21,
            "custom_times_us": [0.8, 2.0],
        },
    }


if __name__ == "__main__":
    unittest.main()
