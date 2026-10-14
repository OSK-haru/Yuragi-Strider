from __future__ import annotations

import unittest

import numpy as np

from scripts.analyze_phase3b_decay_models import (
    _binomial_nll,
    _observed_probability,
    _rank,
    _t1_candidates,
)


class Phase3BDecayModelTests(unittest.TestCase):
    def test_observed_probability_applies_assignment_span(self) -> None:
        true_probability = np.asarray([0.0, 0.5, 1.0])
        observed = _observed_probability(true_probability, 0.1, 0.2)
        np.testing.assert_allclose(observed, [0.1, 0.45, 0.8])

    def test_binomial_likelihood_prefers_matching_probability(self) -> None:
        ones = np.asarray([80.0])
        shots = np.asarray([100.0])
        matching = _binomial_nll(np.asarray([0.8]), ones, shots)
        mismatching = _binomial_nll(np.asarray([0.5]), ones, shots)
        self.assertLess(matching, mismatching)

    def test_biexponential_parameterization_stays_physical(self) -> None:
        candidate = next(
            item for item in _t1_candidates() if item.name == "t1_biexponential_offset"
        )
        probability = candidate.true_probability(
            np.asarray([0.0, 100.0, 1000.0]),
            np.asarray([1.0, 0.1, 0.4, 100.0, 900.0]),
        )
        self.assertTrue(np.all(probability >= 0.0))
        self.assertTrue(np.all(probability <= 1.0))
        self.assertTrue(np.all(np.diff(probability) <= 0.0))

    def test_aicc_ranking_assigns_largest_weight_to_best(self) -> None:
        fits = [{"model": "b", "aicc": 12.0}, {"model": "a", "aicc": 10.0}]
        ranked = _rank(fits)
        self.assertEqual(ranked[0]["model"], "a")
        self.assertGreater(ranked[0]["aicc_weight"], ranked[1]["aicc_weight"])


if __name__ == "__main__":
    unittest.main()
