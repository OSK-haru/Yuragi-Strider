"""The kick model of drive crosstalk against the pulse-level coupled-transmon network.

Bounds are set from the measured errors (see ``scripts/experiment_kick_vs_pulse_network.py`` and the
design document), with room, so a regression shows up while the accuracy statement stays honest.
"""

import math
import unittest

from core.experimental_isolation_physics import DriveCrosstalkTransfer
from scripts.experiment_kick_vs_pulse_network import Region, infidelities, rz, sx, x

ATTACKER, VICTIM = 10, 21


def leak(c, phase=math.pi / 2.0, back=0.0):
    forward = (DriveCrosstalkTransfer(ATTACKER, VICTIM, c * math.cos(phase), c * math.sin(phase)),)
    return forward + ((DriveCrosstalkTransfer(VICTIM, ATTACKER, back, 0.0),) if back else ())


IDLE_VICTIM = Region(VICTIM, 0.0, ((rz(0.9), sx()), 0.30, (rz(-0.4), sx(), rz(1.3))))
BUSY_VICTIM = Region(VICTIM, 0.0, ((rz(0.9), sx()), (sx(),), (rz(-0.4), sx(), rz(1.3))))


class ConventionTests(unittest.TestCase):
    def test_without_leak_the_two_pictures_agree(self):
        regions = (Region(ATTACKER, 0.0, ((rz(0.7), sx(), rz(0.3)),)), Region(VICTIM, 0.0, ((rz(0.9), sx(), rz(-0.4)),)))
        for error in infidelities(regions, ()):
            self.assertLess(abs(error), 1e-12)


class IdleTargetTests(unittest.TestCase):
    """With the target idle, a leak is one rotation about a fixed axis, which the kick reproduces exactly."""

    def test_the_kick_matches_the_network_for_any_strength_and_leak_phase(self):
        attacker = Region(ATTACKER, 0.10, ((x(),),))
        for c in (0.01, 0.1, 0.3):
            for phase in (0.0, math.pi / 2.0, 2.4):
                with self.subTest(c=c, phase=phase):
                    for error in infidelities((attacker, IDLE_VICTIM), leak(c, phase)):
                        self.assertLess(abs(error), 1e-12)

    def test_the_leak_is_visible_and_the_frame_shift_is_needed(self):
        attacker = Region(ATTACKER, 0.10, ((x(),),))
        regions, transfers = (attacker, IDLE_VICTIM), leak(0.1)
        _, no_kicks = infidelities(regions, transfers, with_kicks=False)
        _, no_shift = infidelities(regions, transfers, frame_shift=False)
        _, shifted = infidelities(regions, transfers)
        self.assertGreater(no_kicks, 1e-2)            # the leak changes the state this much
        self.assertLess(abs(shifted), 1e-12)
        self.assertGreater(no_shift, 0.5 * no_kicks)  # without the frame the kick is on the wrong axis


class ConcurrentPulseTests(unittest.TestCase):
    """A target pulsing during the leak: a kick at the pulse's start is an approximation."""

    def test_error_scales_with_the_square_of_the_leak_and_stays_small(self):
        for c in (0.01, 0.03, 0.1, 0.3):
            with self.subTest(c=c):
                regions = (Region(ATTACKER, 0.05, ((x(),),)), BUSY_VICTIM)
                _, error = infidelities(regions, leak(c))
                self.assertLess(error, 0.1 * c * c)

    def test_worst_alignment_of_two_pulses(self):
        errors = []
        for offset in (0.0, 0.009, 0.018, 0.027):
            regions = (Region(ATTACKER, 0.036 + offset, ((x(),),)), BUSY_VICTIM)
            errors.append(infidelities(regions, leak(0.1))[1])
        self.assertLess(max(errors), 0.25e-2)          # measured up to 1.7e-3, aligned
        self.assertEqual(errors.index(max(errors)), 0)

    def test_a_target_pi_pulse_during_the_leak_is_badly_approximated(self):
        """Recorded limitation: a pi pulse turns the leak axis during the pulse, which one kick cannot."""
        regions = (Region(ATTACKER, 0.05, ((sx(),), (x(),))), BUSY_VICTIM)
        error_attacker, _ = infidelities(regions, leak(0.1, back=0.1))
        self.assertGreater(error_attacker, 5e-3)       # measured 1.6e-2
        self.assertLess(error_attacker, 2.0 * 0.1 ** 2)


if __name__ == "__main__":
    unittest.main()
