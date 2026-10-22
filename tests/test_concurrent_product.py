"""Product settings for concurrent sharing: choice, validation, frozen manifest and the assembled executor."""

import json
import unittest

from api.attack_product_factory import (
    ProductAssemblyLimits, ProductEnvironmentFactory, _parse_frozen_inputs, concurrent_regions_for,
)
from api.attack_device_snapshot import default_device_snapshot
from api.attack_product_profiles import product_free_scenarios
from api.attack_sessions import SessionHostLimits, WebOwnerContext
from api.attack_settings_resolver import ScenarioSettingsResolver
from core.attack_simulation.qpu_topology import TopologyLimits
from core.attack_simulation.request_receipts import ReceiptLimits
from core.attack_simulation.concurrency_policy import CrosstalkDeclaration, cross_region_transfers

PREFIX = "qpu_service.runtime_execution."
DEDICATED = {PREFIX + "placement_policy": "tenant_dedicated_region"}
OVERLAP = {**DEDICATED, PREFIX + "concurrency_policy": "overlap_disjoint_regions"}
LIMITS = ProductAssemblyLimits(
    session=SessionHostLimits(16384, 4, ReceiptLimits(16, 64), max_queued_jobs=8),
    topology=TopologyLimits(256, 2048, 4096),
    max_runtime_jobs=160, max_events_per_job=1100, max_unresolved_events=32,
    max_workload_pending=16, max_workload_history=64, retained_jobs=128,
)


def resolve(settings):
    resolver = ScenarioSettingsResolver(free_scenarios=product_free_scenarios(default_device_snapshot()))
    payload = {"mode": "free", "idle_noise_mode": "calibration_values", "settings": settings}
    return resolver, resolver.resolve(json.dumps(payload).encode())


class ConcurrentProductSettingsTests(unittest.TestCase):
    def test_declared_transfer_list_handles_three_named_regions(self):
        transfers = cross_region_transfers(
            ((1, 2), (2, 3)),
            {"north": (1,), "middle": (2,), "south": (3,)},
            (CrosstalkDeclaration("north", "middle", 0.1, 2.0),
             CrosstalkDeclaration("middle", "south", 0.2, -3.0)),
        )
        self.assertEqual(
            tuple((item.source, item.target, item.coefficient, item.detuning_rad_per_us)
                  for item in transfers),
            ((1, 2, 0.1, 2.0), (2, 3, 0.2, -3.0)),
        )

    def test_default_is_the_exclusive_fifo_with_a_declared_sample_strength(self):
        _, environment = resolve({})
        execution = environment.establish().qpu_service_config["runtime_execution"]
        self.assertEqual(execution["schema_version"], "shared_qpu_runtime_v3")
        self.assertEqual(execution["concurrency_policy"], "exclusive_fifo")
        self.assertEqual(execution["drive_crosstalk_strength"], 0.02)
        self.assertEqual(execution["drive_crosstalk_direction"], "both")

    def test_overlap_needs_dedicated_regions(self):
        with self.assertRaises(ValueError):
            resolve({PREFIX + "concurrency_policy": "overlap_disjoint_regions"})
        _, environment = resolve(OVERLAP)
        self.assertEqual(environment.establish().qpu_service_config["runtime_execution"]["concurrency_policy"],
                         "overlap_disjoint_regions")

    def test_the_new_fields_are_in_the_catalog_with_their_choices(self):
        resolver, _ = resolve({})
        fields = {item["path"]: item for mode in resolver.catalog()["free_mode"]["idle_noise_modes"]
                  for item in mode["fields"]}
        self.assertEqual(fields[PREFIX + "concurrency_policy"]["choices"],
                         ["exclusive_fifo", "overlap_disjoint_regions"])
        self.assertEqual(fields[PREFIX + "drive_crosstalk_direction"]["choices"],
                         ["both", "victim_to_attacker", "attacker_to_victim"])
        self.assertEqual(fields[PREFIX + "drive_crosstalk_strength"]["maximum"], 1.0)

    def test_invalid_values_are_rejected(self):
        for settings in ({**OVERLAP, PREFIX + "drive_crosstalk_strength": 1.5},
                         {**OVERLAP, PREFIX + "drive_crosstalk_direction": "sideways"},
                         {**OVERLAP, PREFIX + "concurrency_policy": "parallel"}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                resolve(settings)

    def _transfers(self, settings):
        _, environment = resolve(settings)
        parsed = _parse_frozen_inputs(environment.establish(), LIMITS)
        concurrent = concurrent_regions_for(parsed)
        return concurrent, parsed

    def test_exclusive_declares_no_concurrent_regions(self):
        concurrent, _ = self._transfers(DEDICATED)
        self.assertIsNone(concurrent)

    def test_overlap_declares_a_transfer_per_direction_on_the_edge_joining_the_regions(self):
        concurrent, parsed = self._transfers(OVERLAP)
        victim = set(parsed["region"].physical_qubit_ids)
        attacker = set(parsed["alternate_region"].physical_qubit_ids)
        pairs = {(item.source, item.target) for item in concurrent.transfers}
        self.assertEqual(pairs, {(5, 6), (6, 5)})            # the default regions touch on one coupling edge
        self.assertTrue(all(item.source in victim | attacker and item.target in victim | attacker
                            for item in concurrent.transfers))
        self.assertTrue(all(item.coefficient == 0.02 for item in concurrent.transfers))

    def test_direction_and_strength_select_the_transfers(self):
        victim_only, parsed = self._transfers({**OVERLAP, PREFIX + "drive_crosstalk_direction": "victim_to_attacker"})
        self.assertEqual({(item.source, item.target) for item in victim_only.transfers}, {(5, 6)})
        attacker_only, _ = self._transfers({**OVERLAP, PREFIX + "drive_crosstalk_direction": "attacker_to_victim"})
        self.assertEqual({(item.source, item.target) for item in attacker_only.transfers}, {(6, 5)})
        none, _ = self._transfers({**OVERLAP, PREFIX + "drive_crosstalk_strength": 0.0})
        self.assertEqual(none.transfers, ())

    def test_the_product_assembles_an_executor_that_runs_regions_concurrently(self):
        factory = ProductEnvironmentFactory(LIMITS)
        _, environment = resolve(OVERLAP)
        product = factory.assemble(environment, WebOwnerContext())
        try:
            executor = product.controller._world._executor
            self.assertIsNotNone(executor._concurrent)
            self.assertEqual(len(executor._lanes), 2)
        finally:
            product.close()


if __name__ == "__main__":
    unittest.main()
