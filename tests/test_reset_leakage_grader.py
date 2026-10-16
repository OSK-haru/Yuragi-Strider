"""Focused tests for the reset-leakage hidden-evaluation policy."""

import unittest

from core.attack_simulation import HiddenEvaluationPolicy


class HiddenEvaluationPolicyTests(unittest.TestCase):
    def test_hidden_policy_requires_both_secret_values(self) -> None:
        with self.assertRaises(ValueError):
            HiddenEvaluationPolicy(
                policy_id="test-policy",
                policy_version="1",
                case_count=4,
                zero_case_count=4,
                shots_per_case=3,
                required_correct=3,
                case_order_seed=7,
                measurement_seed=13,
            )


if __name__ == "__main__":
    unittest.main()
