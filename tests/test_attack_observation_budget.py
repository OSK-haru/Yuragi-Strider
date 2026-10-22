"""Registered observation query labels mapped to scenario budget counters."""

import unittest

from core.attack_simulation.observation_budget import ObservationBudgetLedger
from core.attack_simulation.scenario_policy import ObservationBudget


class ObservationBudgetRegistryTests(unittest.TestCase):
    def test_scenario_can_register_a_query_label_without_core_enum_changes(self):
        ledger = ObservationBudgetLedger(
            ObservationBudget(shots_per_query=8, training_query_limit=2,
                              victim_observation_limit=1, retry_limit=0),
            query_kind_counters={"training": "training_queries", "tomography": "training_queries"},
        )
        usage = ledger.reserve(kind="tomography", shots=3)
        self.assertEqual(usage.training_queries, 1)
        with self.assertRaises(ValueError):
            ledger.reserve(kind="unregistered", shots=1)


if __name__ == "__main__":
    unittest.main()
