"""Window overlap and drive-crosstalk kicks as pure functions of declared windows."""

import cmath
import math
import unittest

from core.attack_simulation.concurrent_windows import (
    DriveSchedule, ExecutionWindow, drive_crosstalk_kicks, kicks_into, overlap_groups, overlap_span,
    shares_qubits, windows_overlap,
)
from core.attack_simulation.event_commit_boundary import EventPosition
from core.attack_simulation.shot_activity import ActivityInterval, ShotActivity
from core.experimental_isolation_physics.coherent_crosstalk import DriveCrosstalkTransfer

CLOCK = "world"


def shot(*intervals, duration=10.0):
    return ShotActivity(duration, tuple(intervals))


def window(job, tenant, qubits, start, finish, activity, shots=1, delay=0.0, offset=0.0):
    return ExecutionWindow(job, tenant, tuple(qubits), EventPosition(CLOCK, start, 0), EventPosition(CLOCK, finish, 0),
                           DriveSchedule(activity, shots, delay, offset))


def sx(qubit, start, end, phase=0.0):
    return ActivityInterval(qubit, "drive", start, end, angle_rad=math.pi / 2, phase_rad=phase)


class OverlapTests(unittest.TestCase):
    def setUp(self):
        self.empty = shot()

    def test_touching_windows_do_not_overlap(self):
        a = window("a", "t1", (0,), 0, 10, self.empty)
        b = window("b", "t2", (5,), 10, 20, self.empty)
        self.assertFalse(windows_overlap(a, b))
        self.assertFalse(windows_overlap(b, a))
        self.assertIsNone(overlap_span(a, b))

    def test_overlap_span_is_the_shared_interval(self):
        a = window("a", "t1", (0,), 0, 10, self.empty)
        b = window("b", "t2", (5,), 4, 20, self.empty)
        self.assertEqual(overlap_span(a, b), (4.0, 10.0))
        self.assertEqual(overlap_span(b, a), (4.0, 10.0))

    def test_zero_length_in_time_never_overlaps(self):
        a = ExecutionWindow("a", "t1", (0,), EventPosition(CLOCK, 5, 0), EventPosition(CLOCK, 5, 1),
                            DriveSchedule(shot(duration=0.0), 1))
        b = window("b", "t2", (5,), 0, 10, self.empty)
        self.assertFalse(windows_overlap(a, b))

    def test_clock_domains_must_match(self):
        a = window("a", "t1", (0,), 0, 10, self.empty)
        other = ExecutionWindow("b", "t2", (5,), EventPosition("other", 0, 0), EventPosition("other", 10, 0),
                                DriveSchedule(self.empty, 1))
        with self.assertRaises(ValueError):
            windows_overlap(a, other)

    def test_shares_qubits(self):
        a = window("a", "t1", (0, 1), 0, 10, self.empty)
        self.assertTrue(shares_qubits(a, window("b", "t2", (1, 2), 0, 10, self.empty)))
        self.assertFalse(shares_qubits(a, window("c", "t2", (2, 3), 0, 10, self.empty)))

    def test_groups_chain_through_a_middle_window(self):
        a = window("a", "t1", (0,), 0, 10, self.empty)
        b = window("b", "t2", (1,), 8, 20, self.empty)
        c = window("c", "t1", (2,), 18, 30, self.empty)
        d = window("d", "t2", (3,), 30, 40, self.empty)     # touches c only
        e = window("e", "t1", (4,), 100, 110, self.empty)
        self.assertEqual(overlap_groups([d, c, e, b, a]), ((4, 3, 1), (0,), (2,)))

    def test_groups_of_nothing(self):
        self.assertEqual(overlap_groups([]), ())


class ScheduleTests(unittest.TestCase):
    def test_shots_repeat_with_offset_and_delay(self):
        schedule = DriveSchedule(shot(sx(0, 1, 3)), shots=3, repetition_delay_us=2.0, offset_us=5.0)
        self.assertEqual([(i.start_us, i.end_us) for i in schedule.intervals()], [(6, 8), (18, 20), (30, 32)])
        self.assertEqual(schedule.duration_us, 5 + 30 + 4)

    def test_schedule_must_fit_the_window(self):
        with self.assertRaises(ValueError):
            window("a", "t1", (0,), 0, 20, shot(), shots=3)
        window("a", "t1", (0,), 0, 30, shot(), shots=3)

    def test_schedule_qubit_must_exist(self):
        with self.assertRaises(ValueError):
            window("a", "t1", (7,), 0, 10, shot(sx(1, 0, 1)))


class KickTests(unittest.TestCase):
    def setUp(self):
        # The attacker's index 0 is physical qubit 10; the victim's index 1 is physical qubit 21.
        self.victim = window("v", "victim", (20, 21), 0, 100, shot(sx(1, 0, 2, 0.3), duration=2.0), shots=1)
        self.transfer = DriveCrosstalkTransfer(10, 21, 0.0, 0.1)

    def attacker(self, start, finish, *intervals, **kw):
        return window("a", "attacker", (10, 11), start, finish, shot(*intervals, duration=finish - start), **kw)

    def test_kick_angle_and_axis_follow_the_transfer(self):
        attacker = self.attacker(0, 50, sx(0, 4, 8, 0.25))
        (kick,) = drive_crosstalk_kicks(attacker, self.victim, [self.transfer])
        self.assertAlmostEqual(kick.angle_rad, 0.1 * math.pi / 2)
        self.assertAlmostEqual(kick.phase_rad, math.pi / 2 + 0.25)
        self.assertEqual((kick.qubit, kick.time_us, kick.source_job_id, kick.source_qubit), (21, 4.0, "a", 10))

    def test_drive_is_placed_at_the_window_start_and_every_shot(self):
        attacker = window("a", "attacker", (10, 11), 20, 50, shot(sx(0, 1, 2), duration=5.0), shots=2, delay=1.0)
        kicks = drive_crosstalk_kicks(attacker, self.victim, [self.transfer])
        self.assertEqual([k.time_us for k in kicks], [21.0, 27.0])

    def test_no_overlap_no_kick(self):
        attacker = self.attacker(100, 120, sx(0, 0, 4))
        self.assertEqual(drive_crosstalk_kicks(attacker, self.victim, [self.transfer]), ())

    def test_drive_cut_by_the_window_edge_counts_for_its_inside_part(self):
        attacker = self.attacker(95, 110, sx(0, 0, 10))     # runs 95..105, victim ends at 100
        (kick,) = drive_crosstalk_kicks(attacker, self.victim, [self.transfer])
        self.assertAlmostEqual(kick.angle_rad, 0.1 * math.pi / 2 * 0.5)
        self.assertEqual(kick.time_us, 95.0)
        early = window("a", "attacker", (10, 11), 0, 30, shot(sx(0, 0, 10), duration=30.0))
        late_victim = window("v", "victim", (20, 21), 5, 100, shot(duration=95.0))
        (kick,) = drive_crosstalk_kicks(early, late_victim, [self.transfer])
        self.assertAlmostEqual(kick.angle_rad, 0.1 * math.pi / 2 * 0.5)
        self.assertEqual(kick.time_us, 5.0)

    def test_only_declared_nonzero_transfers_act(self):
        attacker = self.attacker(0, 50, sx(0, 0, 4))
        others = [DriveCrosstalkTransfer(10, 20, 0.0, 0.0),      # zero coefficient
                  DriveCrosstalkTransfer(11, 21, 0.1, 0.0),      # that source never drives
                  DriveCrosstalkTransfer(21, 10, 0.1, 0.0),      # the other direction
                  DriveCrosstalkTransfer(10, 99, 0.1, 0.0)]      # target not in the victim
        self.assertEqual(drive_crosstalk_kicks(attacker, self.victim, others), ())

    def test_virtual_z_and_other_activity_leak_nothing(self):
        attacker = self.attacker(0, 50, ActivityInterval(0, "frame", 1, 1, angle_rad=1.0),
                                 ActivityInterval(0, "idle", 2, 9), ActivityInterval(0, "measure", 9, 10),
                                 ActivityInterval(0, "two_qubit", 10, 20), ActivityInterval(0, "reset", 20, 21))
        self.assertEqual(drive_crosstalk_kicks(attacker, self.victim, [self.transfer]), ())

    def test_both_directions_are_separate_declarations(self):
        victim = window("v", "victim", (20, 21), 0, 100, shot(sx(1, 30, 32), duration=100.0))
        attacker = self.attacker(0, 50, sx(0, 4, 8))
        back = DriveCrosstalkTransfer(21, 10, 0.05, 0.0)
        forward = drive_crosstalk_kicks(attacker, victim, [self.transfer, back])
        reverse = drive_crosstalk_kicks(victim, attacker, [self.transfer, back])
        self.assertEqual([(k.qubit, k.time_us) for k in forward], [(21, 4.0)])
        (kick,) = reverse
        self.assertEqual((kick.qubit, kick.time_us, kick.source_job_id), (10, 30.0, "v"))
        self.assertAlmostEqual(kick.angle_rad, 0.05 * math.pi / 2)
        self.assertAlmostEqual(kick.phase_rad, 0.0)

    def test_shared_qubits_are_rejected(self):
        clash = window("c", "other", (21, 22), 0, 50, shot(sx(0, 0, 4)))
        with self.assertRaises(ValueError):
            drive_crosstalk_kicks(clash, self.victim, [self.transfer])

    def test_kicks_into_merges_sources_in_time_order(self):
        early = window("e", "t2", (10, 11), 0, 50, shot(sx(0, 10, 12), duration=50.0))
        late = window("l", "t3", (30, 31), 0, 50, shot(sx(0, 3, 5), duration=50.0))
        transfers = [self.transfer, DriveCrosstalkTransfer(30, 20, 0.2, 0.0)]
        kicks = kicks_into(self.victim, [early, self.victim, late], transfers)
        self.assertEqual([(k.source_job_id, k.time_us) for k in kicks], [("l", 3.0), ("e", 10.0)])

    def test_deterministic(self):
        attacker = self.attacker(0, 50, sx(0, 4, 8), shots=1)
        first = drive_crosstalk_kicks(attacker, self.victim, [self.transfer])
        self.assertEqual(first, drive_crosstalk_kicks(attacker, self.victim, [self.transfer]))
        # No drive phase: the kick is the transfer coefficient times the source rotation.
        self.assertTrue(cmath.isclose(cmath.rect(first[0].angle_rad, first[0].phase_rad),
                                      self.transfer.coefficient * (math.pi / 2), abs_tol=1e-12))


if __name__ == "__main__":
    unittest.main()
