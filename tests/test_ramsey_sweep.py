import asyncio
import math
import unittest

from api.main import app
from core.ramsey_analysis import fit_ramsey_envelopes
from tests.test_pulse_api_baseline_a import _asgi_post_json


def _payload() -> dict[str, object]:
    return {
        "simulation_backend": "python_dense",
        "evolution_method": "fixed_step_rk4",
        "maximum_delay_us": 300.0,
        "point_count": 61,
        "detuning_mhz": 0.01,
        "parameters": {
            "device_quality": 1.0,
            "temperature_mk": 0.0,
            "flux_noise_phi0": 0.0,
            "qubit_frequency_ghz": 5.0,
            "t1_max_us": 100.0,
            "tphi_max_us": 100.0,
        },
    }


class RamseyEnvelopeFitTests(unittest.TestCase):
    def test_recovers_exponential_envelope_parameters(self) -> None:
        times = [index * 5.0 for index in range(61)]
        observed = [
            0.49 + 0.44 * math.exp(-time / 120.0)
            * math.cos(2.0 * math.pi * 0.01 * time + 0.3)
            for time in times
        ]

        selected, alternative = fit_ramsey_envelopes(times, observed)

        self.assertEqual(selected.model, "exponential_envelope")
        self.assertAlmostEqual(selected.t2_star_us, 120.0, places=5)
        self.assertAlmostEqual(selected.frequency_mhz, 0.01, places=9)
        self.assertAlmostEqual(selected.phase_rad, 0.3, places=8)
        self.assertAlmostEqual(selected.observation_floor, 0.49, places=9)
        self.assertLess(selected.root_mean_square_error, 1e-12)
        self.assertLess(selected.aicc, alternative.aicc)

    def test_selects_a_true_gaussian_envelope(self) -> None:
        times = [index * 5.0 for index in range(61)]
        observed = [
            0.5 + 0.45 * math.exp(-((time / 140.0) ** 2))
            * math.cos(2.0 * math.pi * 0.012 * time - 0.2)
            for time in times
        ]

        selected, _ = fit_ramsey_envelopes(times, observed)

        self.assertEqual(selected.model, "gaussian_envelope")
        self.assertAlmostEqual(selected.t2_star_us, 140.0, places=5)
        self.assertAlmostEqual(selected.frequency_mhz, 0.012, places=8)


class RamseySweepApiTests(unittest.TestCase):
    def test_http_sweep_recovers_the_derived_t2_and_detuning(self) -> None:
        status, response = asyncio.run(
            _asgi_post_json(app, "/api/analysis/ramsey-sweep", _payload())
        )

        self.assertEqual(status, 200)
        self.assertEqual(response["schema_version"], "ramsey-sweep-v1")
        self.assertEqual(response["selected_model"], "exponential_envelope")
        self.assertEqual(len(response["points"]), 61)
        self.assertAlmostEqual(
            response["fit"]["t2_star_us"],
            response["reference"]["t2_effective_us"],
            places=4,
        )
        self.assertAlmostEqual(response["fit"]["frequency_mhz"], 0.01, places=8)
        self.assertGreater(response["fit"]["r_squared"], 0.999999)
        self.assertEqual(len(response["candidate_ranking_by_aicc"]), 2)

    def test_readout_changes_observation_not_the_ramsey_state(self) -> None:
        payload = _payload()
        payload["readout_error"] = {"p10": 0.02, "p01": 0.04}

        status, response = asyncio.run(
            _asgi_post_json(app, "/api/analysis/ramsey-sweep", payload)
        )

        self.assertEqual(status, 200)
        self.assertAlmostEqual(response["fit"]["observation_floor"], 0.49, places=6)
        self.assertAlmostEqual(response["fit"]["amplitude"], 0.47, places=6)
        for point in response["points"]:
            expected_observed = 0.02 + 0.94 * point["true_p1"]
            self.assertAlmostEqual(point["observed_p1"], expected_observed)

    def test_rejects_a_detuning_above_the_sampling_nyquist_limit(self) -> None:
        payload = _payload()
        payload["point_count"] = 9
        payload["detuning_mhz"] = 0.02

        status, _ = asyncio.run(
            _asgi_post_json(app, "/api/analysis/ramsey-sweep", payload)
        )

        self.assertEqual(status, 422)


if __name__ == "__main__":
    unittest.main()
