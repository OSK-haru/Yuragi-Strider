"""Placing leaks from other jobs into a job's shots as kicks, and applying them in the runtime."""

import math
import unittest
from dataclasses import replace
from unittest.mock import patch

import numpy as np

from attack_client.circuit import Circuit
from core.attack_simulation.concurrent_windows import DriveLeak, DriveSchedule
from core.attack_simulation.jobs import JobStatus
from core.attack_simulation.kick_application import EMPTY_PLAN, KickPlan, PlacedKick, kick_unitary, place_kicks
from core.attack_simulation.pulse_shape import SQUARE_SHAPE
from core.attack_simulation.shot_activity import frame_before, instruction_starts, shot_activity
from core.experimental_isolation_physics import ImperfectResetSpec, PhenomenologicalResetModel
from core.experimental_isolation_physics.idle_evolution import IdleQubitSpec, IndependentIdleModel
from tests.test_local_program_runtime import program_json
from tests.test_program_initialization import build, request
from tests.test_shot_activity import IDS as IDS_PRODUCT, compile_circuit

IDS = (3, 7, 11)
RESET_US = 1.0                                    # the runtime config's service reset duration is not read here
GATES = [[{"type": "H", "targets": [0]}], [{"type": "X", "targets": [0]}], [{"type": "H", "targets": [0]}]]


def leak(qubit, start, end, *, coefficient=1.0, angle=math.pi / 2, phase=0.0, detuning=0.0, drive=None,
         job="other"):
    """A leak of a drive that runs exactly over ``drive`` (default: the leak interval itself)."""
    drive_start, drive_end = drive or (start, end)
    return DriveLeak(qubit, start, end, job, 99, complex(coefficient), detuning, drive_start, drive_end, angle,
                     phase, SQUARE_SHAPE)


def vector(angle, axis=0.0):
    return (angle * math.cos(axis), angle * math.sin(axis), 0.0)


def placed(position, qubit, rotation):
    return PlacedKick(position, qubit, rotation)


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


def at_end(qubit, rotation):
    """Every shot gets this kick just before sampling."""
    def plan(operation):
        n = len(operation.instructions)
        return KickPlan(tuple((placed(n, qubit, rotation),) for _ in range(operation.shots)))
    return plan


class KickUnitaryTests(unittest.TestCase):
    def test_axis_and_angle(self):
        np.testing.assert_allclose(kick_unitary(placed(0, 3, vector(math.pi))), [[0, -1j], [-1j, 0]], atol=1e-12)
        np.testing.assert_allclose(kick_unitary(placed(0, 3, vector(math.pi, math.pi / 2))), [[0, -1], [1, 0]],
                                   atol=1e-12)
        np.testing.assert_allclose(kick_unitary(placed(0, 3, (0.0, 0.0, math.pi))), [[-1j, 0], [0, 1j]], atol=1e-12)
        np.testing.assert_allclose(kick_unitary(placed(0, 3, (0.0, 0.0, 0.0))), np.eye(2), atol=1e-12)
        u = kick_unitary(placed(0, 3, (0.3, -0.4, 0.5)))
        np.testing.assert_allclose(u @ u.conj().T, np.eye(2), atol=1e-12)


class RuntimeKickTests(unittest.TestCase):
    def test_no_kicks_is_the_unkicked_run(self):
        counts, _, _, _ = run(lambda op: EMPTY_PLAN)
        self.assertEqual(counts, {"0": 4, "1": 0})

    def test_a_pi_kick_on_the_measured_qubit_flips_every_shot(self):
        counts, _, _, _ = run(at_end(3, vector(math.pi)))
        self.assertEqual(counts, {"0": 0, "1": 4})

    def test_a_kick_on_an_unmeasured_qubit_leaves_the_counts_and_moves_that_qubit(self):
        counts, final, _, _ = run(at_end(7, vector(math.pi)))
        self.assertEqual(counts, {"0": 4, "1": 0})
        self.assertAlmostEqual(final[2, 2].real, 1.0)       # |010>: qubit slot 1 flipped, the others at 0

    def test_a_kick_changes_the_gate_outcome_by_where_it_sits(self):
        # H X H = Z: the qubit stays |0>. Before the last H it is |+>, which an X kick leaves alone but a
        # Y kick sends to |->, so the last H ends in |1>.
        before_last_h = lambda op: KickPlan(tuple(
            (placed(2, 3, vector(math.pi, math.pi / 2)),) for _ in range(op.shots)))
        counts, _, _, _ = run(before_last_h, gates=GATES)
        self.assertEqual(counts, {"0": 0, "1": 4})
        counts, _, _, _ = run(at_end(3, vector(math.pi)), gates=GATES)
        self.assertEqual(counts, {"0": 0, "1": 4})
        counts, _, _, _ = run(lambda op: EMPTY_PLAN, gates=GATES)
        self.assertEqual(counts, {"0": 4, "1": 0})

    def test_trailing_kicks_act_on_the_state_the_last_shot_leaves(self):
        trailing = lambda op: KickPlan(((),) * op.shots, (placed(len(op.instructions), 11, vector(math.pi)),))
        counts, final, _, _ = run(trailing)
        self.assertEqual(counts, {"0": 4, "1": 0})
        self.assertAlmostEqual(final[1, 1].real, 1.0)       # |001>

    def test_shots_with_kicks_do_not_reuse_or_feed_the_entry_cache(self):
        _, _, (computed, reused), _ = run(lambda op: EMPTY_PLAN, shots=6)
        self.assertGreater(reused, 0)
        only_second = lambda op: KickPlan(((), (placed(len(op.instructions), 3, vector(math.pi)),), (), (), (), ()))
        counts, _, (computed, reused), _ = run(only_second, shots=6)
        self.assertEqual(counts, {"0": 5, "1": 1})
        self.assertEqual(computed + reused, 6)
        _, _, (_, reused_plain), _ = run(lambda op: EMPTY_PLAN, shots=6)
        self.assertLess(reused, reused_plain)

    def test_wait_kick_is_followed_by_the_remaining_relaxation(self):
        _, runtime, service = build(ids=IDS)
        runtime.config = replace(runtime.config, repetition_delay_us=10.0)
        runtime.idle_model = IndependentIdleModel(
            (IdleQubitSpec(1.0, math.inf, 0.0),
             IdleQubitSpec(math.inf, math.inf, 0.0),
             IdleQubitSpec(math.inf, math.inf, 0.0)), 3,
        )
        text = program_json(3, [], measurements=[(0, 0)], classical_bits=1)
        job = service.submit_program("a", request(text=text, mode="inherit"))
        record = runtime.scheduler.get(job.job_id)
        kick = placed(0, IDS[0], vector(math.pi))
        kick = PlacedKick(kick.position, kick.qubit, kick.rotation, wait_offset_us=1.0)
        state, _, _ = runtime._run_before_measurement(
            record.command.operation, (0,), np.asarray(runtime.state_store.read().density_matrix, dtype=complex),
            1, (kick,),
        )
        excited = sum(state[index, index].real for index in range(8) if index & 4)
        self.assertAlmostEqual(excited, math.exp(-9.0), places=12)


class PlacementTests(unittest.TestCase):
    """Positions and sizes, for a qubit outside the program (no frame, no own evolution)."""

    @classmethod
    def setUpClass(cls):
        *_, cls.operation = run(lambda op: EMPTY_PLAN, gates=GATES, shots=2)
        cls.starts, _ = instruction_starts(cls.operation, reset_duration_us=RESET_US)
        cls.shot = shot_activity(cls.operation, reset_duration_us=RESET_US, measurement_duration_us=1.0)
        cls.schedule = DriveSchedule(cls.shot, 3, repetition_delay_us=5.0, offset_us=2.0)

    def place(self, *leaks, window_start=10.0):
        return place_kicks(leaks, self.operation, self.schedule, window_start_us=window_start,
                           reset_duration_us=RESET_US, layout_qubit_ids=())

    def shot_time(self, shot_index, local, window_start=10.0):
        return window_start + self.schedule.offset_us + shot_index * self.schedule.period_us + local

    def short(self, time_us, qubit=3):
        return leak(qubit, time_us, time_us + 1e-4)

    def test_a_leak_goes_after_the_instructions_that_started_before_it(self):
        starts = [s for s in self.starts if s is not None]
        later = next(i for i, s in enumerate(starts) if s > starts[0])
        just_before_later = self.place(self.short(self.shot_time(1, starts[later] - 1e-3)))
        self.assertEqual([p.position for p in just_before_later.per_shot[1]], [later])
        at_later = self.place(self.short(self.shot_time(1, starts[later])))
        self.assertEqual([p.position for p in at_later.per_shot[1]], [later + 1])

    def test_the_rotation_is_coefficient_times_the_drive_angle_over_the_fraction_inside(self):
        plan = self.place(leak(3, self.shot_time(0, 0.0), self.shot_time(0, 0.5), coefficient=0.2j,
                               angle=math.pi, phase=0.25, drive=(self.shot_time(0, 0.0), self.shot_time(0, 2.0))))
        (kick,) = plan.per_shot[0]
        np.testing.assert_allclose(kick.rotation, vector(0.2 * math.pi / 4.0, math.pi / 2 + 0.25), atol=1e-12)
        self.assertEqual((kick.qubit, kick.sources), (3, ("other",)))

    def test_before_the_first_shot_goes_to_its_start(self):
        plan = self.place(leak(3, self.shot_time(0, -1.5), self.shot_time(0, -1.0)))   # in the handover offset
        self.assertEqual([p.position for p in plan.per_shot[0]], [0])
        self.assertEqual([len(s) for s in plan.per_shot], [1, 0, 0])

    def test_a_gap_before_the_window_goes_to_the_start_of_the_first_shot(self):
        gap = leak(3, 1.0, 4.0, angle=0.6)                      # the window starts at 10
        plan = self.place(gap)
        (kick,) = plan.per_shot[0]
        self.assertEqual(kick.position, 0)
        np.testing.assert_allclose(kick.rotation, vector(0.6 * (1.0)), atol=1e-12)    # rate 0.6 / 3 us over the 3 us
        straddling = leak(3, 8.0, 14.0, angle=0.6)
        plan = self.place(straddling)
        self.assertEqual(plan.per_shot[0][0].position, 0)
        self.assertGreater(plan.per_shot[0][0].rotation[0], 0.0)

    def test_the_wait_between_shots_goes_to_the_next_shots_start(self):
        plan = self.place(self.short(self.shot_time(0, self.shot.duration_us + 1.0)))
        self.assertEqual([len(s) for s in plan.per_shot], [0, 1, 0])
        self.assertEqual(plan.per_shot[1][0].position, 0)
        self.assertAlmostEqual(plan.per_shot[1][0].wait_offset_us, 1.0001)

    def test_separate_same_source_leaks_keep_their_order_at_one_instruction_position(self):
        first = leak(3, self.shot_time(0, 0.2), self.shot_time(0, 0.2001), angle=0.8, phase=0.0,
                     job="same")
        second = leak(3, self.shot_time(0, 0.5), self.shot_time(0, 0.5001), angle=0.8,
                      phase=math.pi / 2, job="same")
        (early, late) = self.place(first, second).per_shot[0]
        self.assertEqual(early.position, late.position)
        self.assertEqual(early.sources, ("same",))
        self.assertEqual(late.sources, ("same",))
        self.assertLess(early.rotation[1], 1e-12)
        self.assertGreater(late.rotation[1], 0.79)

    def test_during_the_final_measurements_goes_just_before_sampling(self):
        plan = self.place(self.short(self.shot_time(2, self.shot.duration_us - 0.5)))
        self.assertEqual([p.position for p in plan.per_shot[2]], [self.starts.index(None)])

    def test_after_the_last_shot_is_trailing(self):
        plan = self.place(self.short(self.shot_time(2, self.shot.duration_us + 0.1)))
        self.assertEqual(len(plan.trailing), 1)
        self.assertEqual([len(s) for s in plan.per_shot], [0, 0, 0])

    def test_a_leak_across_shots_is_split_between_them(self):
        spanning = leak(3, self.shot_time(0, self.shot.duration_us - 1.0), self.shot_time(1, 1.0), angle=2.0,
                        drive=(self.shot_time(0, self.shot.duration_us - 1.0), self.shot_time(1, 1.0)))
        plan = self.place(spanning)
        # shot 0's tail; the wait and shot 1's head (a separate instruction position)
        self.assertEqual([len(s) for s in plan.per_shot], [1, 2, 0])
        total = sum(k.rotation[0] for shot in plan.per_shot for k in shot)
        self.assertAlmostEqual(total, 2.0)                               # the whole drive angle, however it is split

    def test_kicks_keep_time_order_inside_a_shot_and_sources_add(self):
        duration = self.shot.duration_us
        late = leak(3, self.shot_time(0, 0.6 * duration), self.shot_time(0, 0.6 * duration + 1e-3), job="b")
        early = leak(7, self.shot_time(0, 0.2 * duration), self.shot_time(0, 0.2 * duration + 1e-3), job="a")
        again = leak(3, self.shot_time(0, 0.6 * duration), self.shot_time(0, 0.6 * duration + 1e-3), job="c",
                     phase=math.pi / 2)
        plan = self.place(late, early, again)
        kicks = plan.per_shot[0]
        self.assertEqual([k.qubit for k in kicks], [7, 3])
        positions = [k.position for k in kicks]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(kicks[1].sources, ("b", "c"))
        np.testing.assert_allclose(kicks[1].rotation[:2], np.array(vector(1e-3 * math.pi / 2 / 1e-3 * 1.0, 0.0)[:2])
                                   + np.array(vector(math.pi / 2, math.pi / 2)[:2]), atol=1e-6)

    def test_a_detuned_leak_has_the_phase_of_each_shots_own_time_and_a_resonant_one_repeats(self):
        for detuning, same in ((0.0, True), (1.7, False)):
            with self.subTest(detuning=detuning):
                plan = self.place(*[leak(3, self.shot_time(k, 1.0), self.shot_time(k, 1.2), detuning=detuning)
                                    for k in range(3)])
                first, second = plan.per_shot[0][0].rotation, plan.per_shot[1][0].rotation
                self.assertEqual(np.allclose(first, second), same)


class FrameConversionTests(unittest.TestCase):
    """With a program on the target qubit, the axis follows its virtual-Z frame and its own pulses."""

    @classmethod
    def setUpClass(cls):
        cls.operation = compile_circuit(Circuit(3).rz(0, 0.5).h(0).rz(0, 0.25).x(1).measure_all())
        cls.ids = IDS_PRODUCT
        cls.shot = shot_activity(cls.operation, reset_duration_us=1.0, measurement_duration_us=1.0)
        cls.schedule = DriveSchedule(cls.shot, 1)
        cls.starts, _ = instruction_starts(cls.operation, reset_duration_us=1.0)

    def place(self, *leaks):
        return place_kicks(leaks, self.operation, self.schedule, window_start_us=0.0, reset_duration_us=1.0,
                           layout_qubit_ids=self.ids)

    def short(self, qubit_id, time_us, phase=0.3):
        return leak(qubit_id, time_us, time_us + 1e-3, phase=phase, angle=1e-3)

    def test_frame_before_matches_the_activity_record(self):
        frames = [i for i in self.shot.of_qubit(0) if i.kind == "frame"]
        self.assertGreater(len(frames), 0)
        total = sum(i.angle_rad for i in frames)
        self.assertAlmostEqual(frame_before(self.operation, len(self.operation.instructions), 0), total)
        self.assertEqual(frame_before(self.operation, 0, 0), 0.0)
        self.assertEqual(frame_before(self.operation, len(self.operation.instructions), 5), 0.0)

    def test_the_axis_adds_the_frame_accumulated_before_the_leak(self):
        total = frame_before(self.operation, len(self.operation.instructions), 0)
        (after_all,) = self.place(self.short(self.ids[0], self.shot.duration_us - 0.5)).per_shot[0]
        rx, ry, _ = after_all.rotation
        self.assertAlmostEqual(math.remainder(math.atan2(ry, rx) - (0.3 + total), 2 * math.pi), 0.0, places=6)
        (at_start,) = self.place(self.short(self.ids[0], 0.0)).per_shot[0]     # the reset has started, no gate has
        rx, ry, _ = at_start.rotation
        self.assertAlmostEqual(math.atan2(ry, rx), 0.3, places=6)

    def test_the_frame_is_per_qubit_and_absent_outside_the_program(self):
        end = self.shot.duration_us - 0.5
        for qubit in (self.ids[2], 999):                 # a qubit with no virtual Z, and one outside the program
            (kick,) = self.place(self.short(qubit, end)).per_shot[0]
            self.assertAlmostEqual(math.atan2(kick.rotation[1], kick.rotation[0]), 0.3, places=6)

    def test_the_frame_of_an_uncalibrated_gate_is_unknown(self):
        *_, uncalibrated = run(lambda op: EMPTY_PLAN, gates=GATES, shots=1)
        with self.assertRaises(ValueError):
            frame_before(uncalibrated, len(uncalibrated.instructions), 0)

    def test_a_leak_during_the_targets_own_pulse_goes_before_the_gate_in_its_interaction_picture(self):
        """A leak about Y during a pi pulse about X comes out as a Z rotation of 2/pi of its size."""
        operation = compile_circuit(Circuit(3).x(0).measure_all())
        shot = shot_activity(operation, reset_duration_us=1.0, measurement_duration_us=1.0)
        (pulse,) = [i for i in shot.of_qubit(0) if i.kind == "drive"]
        gate_position = next(i for i, item in enumerate(operation.instructions) if item.natives)
        size = 0.01
        plan = place_kicks([leak(self.ids[0], pulse.start_us, pulse.end_us, angle=size, phase=math.pi / 2)],
                           operation, DriveSchedule(shot, 1), window_start_us=0.0, reset_duration_us=1.0,
                           layout_qubit_ids=self.ids)
        (kick,) = plan.per_shot[0]
        self.assertEqual(kick.position, gate_position)
        np.testing.assert_allclose(kick.rotation, (0.0, 0.0, -2.0 * size / math.pi), atol=3e-4)

    def test_a_leak_about_the_pulse_axis_is_not_changed_by_the_pulse(self):
        operation = compile_circuit(Circuit(3).x(0).measure_all())
        shot = shot_activity(operation, reset_duration_us=1.0, measurement_duration_us=1.0)
        (pulse,) = [i for i in shot.of_qubit(0) if i.kind == "drive"]
        plan = place_kicks([leak(self.ids[0], pulse.start_us, pulse.end_us, angle=0.2, phase=0.0)],
                           operation, DriveSchedule(shot, 1), window_start_us=0.0, reset_duration_us=1.0,
                           layout_qubit_ids=self.ids)
        (kick,) = plan.per_shot[0]
        np.testing.assert_allclose(kick.rotation, (0.2, 0.0, 0.0), atol=1e-12)


class SecondOrderTests(unittest.TestCase):
    """A leak whose axis turns is more than its net rotation; the kick must carry the rest."""

    def test_a_detuned_leak_matches_the_exact_ordered_product(self):
        operation = compile_circuit(Circuit(3).measure_all())
        shot = shot_activity(operation, reset_duration_us=1.0, measurement_duration_us=1.0)
        detuning, angle, begin, end = 7.0, 0.4, 0.1, 0.9
        piece = leak(IDS_PRODUCT[0], begin, end, angle=angle, detuning=detuning, phase=0.4)
        (kick,) = place_kicks([piece], operation, DriveSchedule(shot, 1), window_start_us=0.0,
                              reset_duration_us=1.0, layout_qubit_ids=()).per_shot[0]
        exact = np.eye(2, dtype=complex)
        edges = np.linspace(begin, end, 4001)
        for low, high in zip(edges[:-1], edges[1:]):
            rate = piece.rate(np.array([0.5 * (low + high)]))[0]
            exact = kick_unitary(PlacedKick(0, 3, (rate.real * (high - low), rate.imag * (high - low), 0.0))) @ exact
        fidelity = abs(np.trace(exact.conj().T @ kick_unitary(kick))) ** 2 / 4.0
        first_order = np.array([piece.rate(np.linspace(begin, end, 20001)).sum() * (end - begin) / 20001])
        self.assertLess(1.0 - fidelity, 1e-6)             # the error left is third order, ~angle^6
        plain_net = kick_unitary(PlacedKick(0, 3, (first_order.real[0], first_order.imag[0], 0.0)))
        plain_fidelity = abs(np.trace(exact.conj().T @ plain_net)) ** 2 / 4.0
        self.assertGreater(1.0 - plain_fidelity, 50.0 * (1.0 - fidelity))   # the net rotation alone is much worse


if __name__ == "__main__":
    unittest.main()
