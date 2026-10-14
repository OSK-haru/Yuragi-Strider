import unittest

from pydantic import ValidationError

from api.pulse_models import (
    CoupledTransmonNetworkPulseSimulateRequest,
    PulseSimulateRequest,
    QutritPulseSimulateRequest,
)
from api.pulse_qutrit_service import run_qutrit_pulse_request
from api.pulse_service import run_pulse_request
from core.gates import apply_local_readout_error
from tests.test_pulse_api_baseline_a import _direct_square_payload
from tests.test_pulse_api_qutrit import _direct_payload
from tests.test_pulse_api_transmon_network import _network_payload


class PulseReadoutCoreTests(unittest.TestCase):
    def test_qutrit_readout_preserves_leakage_level(self) -> None:
        observed = apply_local_readout_error(
            {"0": 0.6, "1": 0.3, "2": 0.1},
            1,
            3,
            [(0.1, 0.2)],
        )

        self.assertAlmostEqual(observed["0"], 0.60)
        self.assertAlmostEqual(observed["1"], 0.30)
        self.assertAlmostEqual(observed["2"], 0.1)
        self.assertAlmostEqual(sum(observed.values()), 1.0)

    def test_multi_qutrit_errors_are_independent_and_normalized(self) -> None:
        probabilities = {
            left + right: 1.0 if left + right == "02" else 0.0
            for left in "012"
            for right in "012"
        }

        observed = apply_local_readout_error(
            probabilities,
            2,
            3,
            [(0.1, 0.0), (0.2, 0.3)],
        )

        self.assertAlmostEqual(observed["02"], 0.9)
        self.assertAlmostEqual(observed["12"], 0.1)
        self.assertAlmostEqual(sum(observed.values()), 1.0)


class PulseReadoutApiTests(unittest.TestCase):
    def test_two_level_readout_changes_observation_not_state(self) -> None:
        baseline_payload = _direct_square_payload()
        observed_payload = _direct_square_payload()
        observed_payload["readout_error"] = {"p10": 0.08, "p01": 0.03}

        baseline = run_pulse_request(PulseSimulateRequest.model_validate(baseline_payload))
        observed = run_pulse_request(PulseSimulateRequest.model_validate(observed_payload))

        self.assertFalse(baseline["readout"]["enabled"])
        self.assertTrue(observed["readout"]["enabled"])
        self.assertEqual(observed["final"], baseline["final"])
        self.assertNotEqual(
            observed["readout"]["observed_probabilities"],
            observed["readout"]["true_probabilities"],
        )
        self.assertAlmostEqual(
            sum(observed["readout"]["observed_probabilities"].values()),
            1.0,
        )

    def test_qutrit_readout_preserves_final_leakage_probability(self) -> None:
        payload = _direct_payload()
        payload["readout_error"] = {"p10": 0.08, "p01": 0.03}

        response = run_qutrit_pulse_request(
            QutritPulseSimulateRequest.model_validate(payload)
        )

        self.assertAlmostEqual(
            response["readout"]["true_probabilities"]["2"],
            response["readout"]["observed_probabilities"]["2"],
        )
        self.assertAlmostEqual(
            response["readout"]["true_probabilities"]["2"],
            response["final"]["leakage_probability"],
        )

    def test_network_accepts_one_pair_per_transmon(self) -> None:
        payload = _network_payload()
        payload["readout_error"] = {
            "per_qubit": [
                {"p10": 0.01, "p01": 0.02},
                {"p10": 0.03, "p01": 0.04},
                {"p10": 0.05, "p01": 0.06},
            ]
        }

        request = CoupledTransmonNetworkPulseSimulateRequest.model_validate(payload)

        self.assertEqual(
            request.readout_error.assignment_errors(3),
            [(0.01, 0.02), (0.03, 0.04), (0.05, 0.06)],
        )

    def test_network_rejects_a_per_qubit_length_mismatch(self) -> None:
        payload = _network_payload()
        payload["readout_error"] = {
            "per_qubit": [{"p10": 0.01, "p01": 0.02}]
        }

        with self.assertRaises(ValidationError):
            CoupledTransmonNetworkPulseSimulateRequest.model_validate(payload)

    def test_rejects_a_non_positive_assignment_span(self) -> None:
        payload = _direct_square_payload()
        payload["readout_error"] = {"p10": 0.6, "p01": 0.4}

        with self.assertRaises(ValidationError):
            PulseSimulateRequest.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
