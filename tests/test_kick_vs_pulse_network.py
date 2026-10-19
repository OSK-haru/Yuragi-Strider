"""The kick model of drive crosstalk against the pulse-level coupled-transmon network.

Bounds are set from the measured errors (see ``scripts/experiment_kick_vs_pulse_network.py`` and the
design document), with room, so a regression shows up while the accuracy statement stays honest.
"""

import math
import unittest

import numpy as np

from core.attack_simulation.pulse_shape import GAUSSIAN_SHAPE, SQUARE_SHAPE
from scripts.experiment_kick_vs_pulse_network import Region, infidelities, leak_transfer, pulse_reference, rz, sx, x

IDLE_VICTIM = (Region(21, 0.0, ((rz(0.9), sx()), 0.30, (rz(-0.4), sx(), rz(1.3)))),)
BUSY_VICTIM = (Region(21, 0.0, ((rz(0.9), sx()), (sx(),), (rz(-0.4), sx(), rz(1.3)))),)
ATTACKER_ONCE = (Region(10, 0.10, ((x(),),)),)


def effect(qubits, transfers, shape=SQUARE_SHAPE):
    """How much the leak changes the victim: the error of the model that applies no kicks."""
    return infidelities(qubits, transfers, shape, with_kicks=False)[1]


class ConventionTests(unittest.TestCase):
    def test_without_leak_the_two_pictures_agree(self):
        qubits = ((Region(10, 0.0, ((rz(0.7), sx(), rz(0.3)),)),), (Region(21, 0.0, ((rz(0.9), sx(), rz(-0.4)),)),))
        for error in infidelities(qubits, ()):
            self.assertLess(abs(error), 1e-10)

    def test_the_reference_does_not_depend_on_its_time_step(self):
        qubits, transfers = (ATTACKER_ONCE, IDLE_VICTIM), leak_transfer(0.3, detuning=100.0)
        coarse, fine = pulse_reference(qubits, transfers, SQUARE_SHAPE, 0.004), pulse_reference(
            qubits, transfers, SQUARE_SHAPE, 0.001)
        self.assertLess(1.0 - abs(np.vdot(coarse, fine)) ** 2, 1e-9)


class IdleTargetTests(unittest.TestCase):
    """With the target idle and the leak resonant, the leak is one rotation about a fixed axis."""

    def test_the_kick_matches_the_network_for_any_strength_phase_and_shape(self):
        for shape in (SQUARE_SHAPE, GAUSSIAN_SHAPE):
            for c in (0.01, 0.1, 0.3):
                for phase in (0.0, math.pi / 2.0, 2.4):
                    with self.subTest(shape=shape.kind, c=c, phase=phase):
                        for error in infidelities((ATTACKER_ONCE, IDLE_VICTIM), leak_transfer(c, phase=phase), shape):
                            self.assertLess(abs(error), 1e-8)      # the Gaussian's area is a 6-node quadrature

    def test_the_leak_is_visible_and_the_frame_shift_is_needed(self):
        qubits, transfers = (ATTACKER_ONCE, IDLE_VICTIM), leak_transfer(0.1)
        _, shifted = infidelities(qubits, transfers)
        _, unshifted = infidelities(qubits, transfers, frame_aware=False)
        self.assertGreater(effect(qubits, transfers), 1e-2)       # the leak changes the state this much
        self.assertLess(abs(shifted), 1e-10)
        self.assertGreater(unshifted, 0.5 * effect(qubits, transfers))   # on the wrong axis, the effect is lost


class DetunedLeakTests(unittest.TestCase):
    """A detuned leak turns its own axis; its second-order effect is carried by the kick."""

    def test_error_is_far_below_the_effect_and_falls_with_the_fourth_power_of_the_strength(self):
        qubits = (ATTACKER_ONCE, IDLE_VICTIM)
        for detuning in (30.0, 100.0, 300.0):
            for shape in (SQUARE_SHAPE, GAUSSIAN_SHAPE):
                with self.subTest(detuning=detuning, shape=shape.kind):
                    transfers = leak_transfer(0.1, detuning=detuning)
                    _, error = infidelities(qubits, transfers, shape)
                    self.assertLess(error, 1e-6)                    # measured up to 3e-7
                    self.assertLess(error, 0.01 * effect(qubits, transfers, shape) + 1e-8)
        _, strong = infidelities(qubits, leak_transfer(0.3, detuning=100.0))
        _, weak = infidelities(qubits, leak_transfer(0.1, detuning=100.0))
        self.assertGreater(strong / weak, 30.0)                      # (0.3 / 0.1) ** 4 = 81 in the limit


class ConcurrentPulseTests(unittest.TestCase):
    """A target pulsing during the leak: the kick is taken in the target gate's interaction picture."""

    def test_error_is_fourth_order_in_the_strength_and_beats_a_plain_kick(self):
        for c in (0.03, 0.1, 0.3):
            with self.subTest(c=c):
                qubits = ((Region(10, 0.05, ((x(),),)),), BUSY_VICTIM)
                transfers = leak_transfer(c)
                _, error = infidelities(qubits, transfers)
                _, plain = infidelities(qubits, transfers, frame_aware=False)
                self.assertLess(error, 3e-2 * c ** 4)                # measured 1.4e-2 c^4
                self.assertGreater(plain, 100.0 * error)

    def test_every_alignment_of_two_pulses(self):
        for offset in (0.0, 0.009, 0.018, 0.027):
            with self.subTest(offset=offset):
                qubits = ((Region(10, 0.036 + offset, ((x(),),)),), BUSY_VICTIM)
                self.assertLess(infidelities(qubits, leak_transfer(0.1))[1], 3e-5)     # measured up to 8e-6

    def test_detuned_gaussian_pulses_against_a_pulsing_target(self):
        qubits = ((Region(10, 0.05, ((x(),),)),), BUSY_VICTIM)
        self.assertLess(infidelities(qubits, leak_transfer(0.1, detuning=100.0), GAUSSIAN_SHAPE)[1], 1e-5)


class BothDirectionsTests(unittest.TestCase):
    def test_both_regions_receive_a_leak_in_the_same_time(self):
        attacker = (Region(10, 0.0, ((sx(),), (x(),), (sx(),))),)
        for c in (0.03, 0.1):
            with self.subTest(c=c):
                for error in infidelities((attacker, BUSY_VICTIM), leak_transfer(c, back=c)):
                    self.assertLess(error, 1e-6)                    # measured up to 1.3e-7

    def test_a_leak_before_a_regions_first_window_is_not_in_the_kick_model(self):
        """Recorded limit: with no earlier job there is no gap to leak into; the caller must supply it."""
        attacker = (Region(10, 0.05, ((sx(),), (x(),))),)           # the victim's first pulse runs before 0.05
        error_attacker, _ = infidelities((attacker, BUSY_VICTIM), leak_transfer(0.1, back=0.1))
        self.assertGreater(error_attacker, 3e-3)                    # measured 1.2e-2


class GapTests(unittest.TestCase):
    def test_a_leak_between_two_jobs_acts_on_the_state_the_next_job_inherits(self):
        victim = (Region(21, 0.0, ((sx(),), (sx(),))), Region(21, 0.40, ((rz(0.6), sx(), rz(-0.2)),)))
        attacker = (Region(10, 0.20, ((x(),),)),)
        for c in (0.1, 0.3):
            with self.subTest(c=c):
                transfers = leak_transfer(c)
                self.assertLess(abs(infidelities((attacker, victim), transfers)[1]), 1e-10)
                self.assertGreater(effect((attacker, victim), transfers), 1e-2 * (c / 0.1) ** 2)


if __name__ == "__main__":
    unittest.main()
