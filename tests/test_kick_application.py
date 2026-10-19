"""Placing externally given kicks into a job's shots and applying them in the runtime."""

import math
import unittest
from unittest.mock import patch

import numpy as np

from core.attack_simulation.concurrent_windows import DriveSchedule, UnitaryKick
from core.attack_simulation.jobs import JobStatus
from core.attack_simulation.kick_application import EMPTY_PLAN, KickPlan, PlacedKick, kick_unitary, place_kicks
from core.attack_simulation.shot_activity import instruction_starts, shot_activity
from core.experimental_isolation_physics import ImperfectResetSpec, PhenomenologicalResetModel
from tests.test_local_program_runtime import program_json
from tests.test_program_initialization import build, request

IDS = (3, 7, 11)
RESET_US = 1.0                                    # the runtime config's service reset duration is not read here
GATES = [[{"type": "H", "targets": [0]}], [{"type": "X", "targets": [0]}], [{"type": "H", "targets": [0]}]]


def kick(qubit, angle, time_us=0.0, phase=0.0):
    return UnitaryKick(qubit, time_us, angle, phase, "other", 99)


def run(plan_for, *, gates=(), shots=4, measure=(0, 0)):
    """Run one job with ``plan_for(operation)`` as its kicks; returns counts, final state, reuse and the operation."""
    model = PhenomenologicalResetModel(ImperfectResetSpec(1.0))
    store, runtime, service = build(model=model, ids=IDS)
    service._sampling_salt = b"fixed-test-salt"
    text = program_json(3, list(gates), measurements=[measure], classical_bits=1)
    job = service.submit_program("a", request(text=text, mode="prepare", shots=shots))
    seen = {}
    original = runtime._execute_circuit

    def with_kicks(job_, operation, kicks=EMPTY_PLAN):
        seen["operation"] = operation
        return original(job_, operation, plan_for(operation))

    with patch.object(runtime, "_execute_circuit", side_effect=with_kicks):
        record = runtime.run_next()
    assert record.status is JobStatus.SUCCEEDED
    return (service.get_result("a", job.job_id).result["counts"], np.asarray(store.read().density_matrix),
            runtime.last_entry_reuse, seen["operation"])


def at_end(*kicks):
    """Every shot gets these kicks just before sampling."""
    def plan(operation):
        n = len(operation.instructions)
        return KickPlan(tuple(tuple(PlacedKick(n, k) for k in kicks) for _ in range(operation.shots)))
    return plan


class KickUnitaryTests(unittest.TestCase):
    def test_axis_and_angle(self):
        np.testing.assert_allclose(kick_unitary(kick(0, math.pi)), [[0, -1j], [-1j, 0]], atol=1e-12)
        np.testing.assert_allclose(kick_unitary(kick(0, math.pi, phase=math.pi / 2)), [[0, -1], [1, 0]], atol=1e-12)
        np.testing.assert_allclose(kick_unitary(kick(0, 0.0)), np.eye(2), atol=1e-12)
        u = kick_unitary(kick(0, 0.7, phase=0.4))
        np.testing.assert_allclose(u @ u.conj().T, np.eye(2), atol=1e-12)


class RuntimeKickTests(unittest.TestCase):
    def test_no_kicks_is_the_unkicked_run(self):
        counts, _, _, _ = run(lambda op: EMPTY_PLAN)
        self.assertEqual(counts, {"0": 4, "1": 0})

    def test_a_pi_kick_on_the_measured_qubit_flips_every_shot(self):
        counts, _, _, _ = run(at_end(kick(3, math.pi)))
        self.assertEqual(counts, {"0": 0, "1": 4})

    def test_a_kick_on_an_unmeasured_qubit_leaves_the_counts_and_moves_that_qubit(self):
        counts, final, _, _ = run(at_end(kick(7, math.pi)))
        self.assertEqual(counts, {"0": 4, "1": 0})
        self.assertAlmostEqual(final[2, 2].real, 1.0)       # |010>: qubit slot 1 flipped, the others at 0

    def test_a_kick_changes_the_gate_outcome_by_where_it_sits(self):
        # H X H = Z: the qubit stays |0>. Before the last H it is |+>, which an X kick leaves alone but a
        # Y kick sends to |->, so the last H ends in |1>.
        before_last_h = lambda op: KickPlan(tuple(
            (PlacedKick(2, kick(3, math.pi, phase=math.pi / 2)),) for _ in range(op.shots)))
        counts, _, _, _ = run(before_last_h, gates=GATES)
        self.assertEqual(counts, {"0": 0, "1": 4})
        after_all = at_end(kick(3, math.pi))
        counts, _, _, _ = run(after_all, gates=GATES)
        self.assertEqual(counts, {"0": 0, "1": 4})
        counts, _, _, _ = run(lambda op: EMPTY_PLAN, gates=GATES)
        self.assertEqual(counts, {"0": 4, "1": 0})

    def test_trailing_kicks_act_on_the_state_the_last_shot_leaves(self):
        trailing = lambda op: KickPlan(((),) * op.shots, (kick(11, math.pi),))
        counts, final, _, _ = run(trailing)
        self.assertEqual(counts, {"0": 4, "1": 0})
        self.assertAlmostEqual(final[1, 1].real, 1.0)       # |001>

    def test_shots_with_kicks_do_not_reuse_or_feed_the_entry_cache(self):
        _, _, (computed, reused), _ = run(lambda op: EMPTY_PLAN, shots=6)
        self.assertGreater(reused, 0)
        only_second = lambda op: KickPlan(((), (PlacedKick(len(op.instructions), kick(3, math.pi)),), (), (), (), ()))
        counts, _, (computed, reused), _ = run(only_second, shots=6)
        self.assertEqual(counts, {"0": 5, "1": 1})
        self.assertEqual(computed + reused, 6)
        _, _, (_, reused_plain), _ = run(lambda op: EMPTY_PLAN, shots=6)
        self.assertLess(reused, reused_plain)


class PlacementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        *_, cls.operation = run(lambda op: EMPTY_PLAN, gates=GATES, shots=2)
        cls.starts, _ = instruction_starts(cls.operation, reset_duration_us=RESET_US)
        cls.shot = shot_activity(cls.operation, reset_duration_us=RESET_US, measurement_duration_us=1.0)
        cls.schedule = DriveSchedule(cls.shot, 3, repetition_delay_us=5.0, offset_us=2.0)

    def place(self, time_us, window_start=10.0):
        plan = place_kicks([kick(3, 0.1, window_start + time_us)], self.operation, self.schedule,
                           window_start_us=window_start, reset_duration_us=RESET_US)
        return plan

    def shot_time(self, shot_index, local):
        return self.schedule.offset_us + shot_index * self.schedule.period_us + local

    def test_a_kick_goes_after_the_instructions_that_started_before_it(self):
        starts = [s for s in self.starts if s is not None]
        later = next(i for i, s in enumerate(starts) if s > starts[0])
        just_before_later = self.place(self.shot_time(1, starts[later] - 1e-6))
        self.assertEqual([p.position for p in just_before_later.per_shot[1]], [later])
        at_later = self.place(self.shot_time(1, starts[later]))
        self.assertEqual([p.position for p in at_later.per_shot[1]], [later + 1])

    def test_before_the_first_shot_goes_to_its_start(self):
        plan = self.place(0.5)                                   # inside the 2 us handover offset
        self.assertEqual([p.position for p in plan.per_shot[0]], [0])
        self.assertEqual([len(s) for s in plan.per_shot], [1, 0, 0])

    def test_the_wait_between_shots_goes_to_the_next_shots_start(self):
        plan = self.place(self.shot_time(0, self.shot.duration_us + 1.0))
        self.assertEqual([len(s) for s in plan.per_shot], [0, 1, 0])
        self.assertEqual(plan.per_shot[1][0].position, 0)

    def test_during_the_final_measurements_goes_just_before_sampling(self):
        plan = self.place(self.shot_time(2, self.shot.duration_us - 0.5))
        self.assertEqual([p.position for p in plan.per_shot[2]], [self.starts.index(None)])

    def test_after_the_last_shot_is_trailing(self):
        plan = self.place(self.shot_time(2, self.shot.duration_us + 0.1))
        self.assertEqual(len(plan.trailing), 1)
        self.assertEqual([len(s) for s in plan.per_shot], [0, 0, 0])

    def test_kicks_keep_time_order_inside_a_shot(self):
        duration = self.shot.duration_us
        late = kick(3, 0.1, 10.0 + self.shot_time(0, 0.6 * duration))
        early = kick(7, 0.1, 10.0 + self.shot_time(0, 0.2 * duration))
        plan = place_kicks([late, early], self.operation, self.schedule, window_start_us=10.0, reset_duration_us=RESET_US)
        self.assertEqual([p.kick for p in plan.per_shot[0]], [early, late])
        positions = [p.position for p in plan.per_shot[0]]
        self.assertEqual(positions, sorted(positions))


if __name__ == "__main__":
    unittest.main()
