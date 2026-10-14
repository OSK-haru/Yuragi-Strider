import asyncio
import math
import unittest

from api.main import app
from core.t1_analysis import fit_exponential_with_floor
from tests.test_pulse_api_baseline_a import _asgi_post_json


def _payload() -> dict[str, object]:
    return {
        "simulation_backend": "python_dense",
        "evolution_method": "fixed_step_rk4",
        "maximum_delay_us": 300.0,
        "point_count": 21,
        "parameters": {
            "device_quality": 1.0,
            "temperature_mk": 0.0,
            "flux_noise_phi0": 0.0,
            "qubit_frequency_ghz": 5.0,
            "t1_max_us": 100.0,
            "tphi_max_us": 100.0,
        },
    }


class ExponentialFloorFitTests(unittest.TestCase):
    def test_recovers_an_exact_exponential_with_a_floor(self) -> None:
        times = [index * 20.0 for index in range(21)]
        observed = [0.82 * math.exp(-time / 125.0) + 0.11 for time in times]

        fit = fit_exponential_with_floor(times, observed)

        self.assertAlmostEqual(fit.amplitude, 0.82, places=8)
        self.assertAlmostEqual(fit.floor, 0.11, places=8)
        self.assertAlmostEqual(fit.t1_us, 125.0, places=6)
        self.assertLess(fit.root_mean_square_error, 1e-12)
        self.assertAlmostEqual(fit.r_squared or 0.0, 1.0, places=12)

    def test_rejects_too_few_points(self) -> None:
        with self.assertRaises(ValueError):
            fit_exponential_with_floor([0.0, 1.0], [1.0, 0.9])


class T1SweepApiTests(unittest.TestCase):
    def test_http_sweep_returns_fit_and_all_points(self) -> None:
        status, response = asyncio.run(
            _asgi_post_json(app, "/api/analysis/t1-sweep", _payload())
        )

        self.assertEqual(status, 200)
        self.assertEqual(response["schema_version"], "t1-sweep-v1")
        self.assertEqual(len(response["points"]), 21)
        self.assertAlmostEqual(response["points"][0]["true_p1"], 1.0)
        self.assertAlmostEqual(response["fit"]["t1_us"], 100.0, places=3)
        self.assertAlmostEqual(response["reference"]["t1_effective_us"], 100.0, places=3)
        self.assertGreater(response["fit"]["r_squared"], 0.999999)

    def test_readout_offset_appears_as_the_observation_floor_only(self) -> None:
        payload = _payload()
        payload["readout_error"] = {"p10": 0.02, "p01": 0.03}

        status, response = asyncio.run(
            _asgi_post_json(app, "/api/analysis/t1-sweep", payload)
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["readout"]["enabled"])
        self.assertAlmostEqual(response["fit"]["observation_floor"], 0.02, places=5)
        self.assertAlmostEqual(response["fit"]["amplitude"], 0.95, places=5)
        self.assertAlmostEqual(response["fit"]["t1_us"], 100.0, places=3)
        for point in response["points"]:
            expected_true = math.exp(-point["delay_us"] / 100.0)
            self.assertAlmostEqual(point["true_p1"], expected_true, places=7)

    def test_invalid_assignment_span_is_rejected(self) -> None:
        payload = _payload()
        payload["readout_error"] = {"p10": 0.6, "p01": 0.4}

        status, _ = asyncio.run(
            _asgi_post_json(app, "/api/analysis/t1-sweep", payload)
        )

        self.assertEqual(status, 422)


if __name__ == "__main__":
    unittest.main()
