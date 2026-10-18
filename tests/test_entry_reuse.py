"""Reusing the computation of a nearly identical entry state keeps the job within a certified error."""

import unittest
from unittest.mock import patch

import numpy as np

from core.attack_simulation.jobs import JobStatus
from core.attack_simulation.shared_qpu_runtime import SharedQpuRuntimeConfig, _find_reusable
from core.experimental_isolation_physics import ImperfectResetSpec, PhenomenologicalResetModel
from core.gates import initial_density_matrix
from tests.test_local_program_runtime import program_json
from tests.test_program_initialization import build, request

GATES = [[{"type": "H", "targets": [0]}], [{"type": "CNOT", "controls": [0], "targets": [1]}],
         [{"type": "RX", "targets": [2], "params": {"theta_rad": 0.7}}]]


def run(tolerance, *, success=0.99, reuse=True, shots=20):
    model = PhenomenologicalResetModel(ImperfectResetSpec(success), observed_one_given_zero=0.05,
                                       observed_zero_given_one=0.08)
    store, runtime, service = build(model=model, ids=(3, 7, 11), initial=initial_density_matrix(["1", "0", "1"]))
    object.__setattr__(runtime.config, "entry_reuse_tolerance", tolerance)
    service._sampling_salt = b"fixed-test-salt"  # the random salt would give each service its own draws
    text = program_json(3, GATES, measurements=[(0, 0), (1, 1)], classical_bits=2)
    job = service.submit_program("a", request(text=text, mode="prepare", shots=shots))
    if reuse:
        record = runtime.run_next()
    else:
        with patch("core.attack_simulation.shared_qpu_runtime._find_reusable", return_value=None):
            record = runtime.run_next()
    assert record.status is JobStatus.SUCCEEDED
    return (service.get_result("a", job.job_id).result["counts"], np.asarray(store.read().density_matrix),
            store.read().device_time_us, runtime.last_entry_reuse)


def state(weights):
    return np.diag(np.array(weights, dtype=complex))


class EntryReuseTests(unittest.TestCase):
    def test_zero_tolerance_is_exact(self):
        counts, final, elapsed, _ = run(0.0)
        base_counts, base_final, base_elapsed, _ = run(0.0, reuse=False)
        self.assertEqual(counts, base_counts)
        self.assertEqual(elapsed, base_elapsed)
        np.testing.assert_array_equal(final, base_final)

    def test_perfect_reset_reuses_identical_states_exactly(self):
        counts, final, _, (computed, reused) = run(0.0, success=1.0)
        base_counts, base_final, _, _ = run(0.0, success=1.0, reuse=False)
        self.assertGreater(reused, 0)
        self.assertEqual(computed + reused, 20)
        self.assertEqual(counts, base_counts)
        np.testing.assert_array_equal(final, base_final)

    def test_a_tolerance_reuses_more_and_keeps_the_counts_within_the_bound(self):
        tolerance = 1e-4
        counts, final, _, (computed, reused) = run(tolerance)
        base_counts, base_final, _, (base_computed, _) = run(0.0)
        self.assertLess(computed, base_computed)
        self.assertGreater(reused, 0)
        # Same random draws: the counts can differ only when a draw falls inside the perturbation.
        moved = sum(abs(counts.get(key, 0) - base_counts.get(key, 0)) for key in set(counts) | set(base_counts)) / 2
        self.assertLessEqual(moved, 20 * tolerance * 20 + 2)

    def test_the_reuse_rule_accepts_only_what_the_bound_certifies(self):
        entry, result = state([0.5, 0.5, 0, 0]), ("result",)
        cache = [(False, entry, result)]
        near = state([0.5 + 1e-6, 0.5 - 1e-6, 0, 0])
        far = state([0.6, 0.4, 0, 0])
        self.assertIs(_find_reusable(cache, False, entry.copy(), 0.0), result)      # identical
        self.assertIsNone(_find_reusable(cache, False, near, 0.0))                   # exact mode
        self.assertIs(_find_reusable(cache, False, near, 1e-5), result)             # trace distance 1e-6
        self.assertIsNone(_find_reusable(cache, False, near, 1e-7))
        self.assertIsNone(_find_reusable(cache, False, far, 1e-3))
        self.assertIsNone(_find_reusable(cache, True, entry.copy(), 1e-3))          # a repeat shot differs

    def test_the_tolerance_is_validated_and_frozen_in_the_manifest(self):
        model = PhenomenologicalResetModel(ImperfectResetSpec(1.0))
        base = dict(reset_model_id=model.model_id, reset_model_configuration_id=model.configuration_id)
        self.assertEqual(SharedQpuRuntimeConfig(**base).to_manifest_config()["entry_reuse_tolerance"], 0.0)
        for bad in (-1e-6, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                SharedQpuRuntimeConfig(**base, entry_reuse_tolerance=bad)


if __name__ == "__main__":
    unittest.main()
