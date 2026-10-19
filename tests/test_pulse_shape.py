"""Envelope shapes as area-normalised profiles over the unit interval."""

import math
import unittest

import numpy as np

from core.attack_simulation.pulse_shape import GAUSSIAN_SHAPE, SQUARE_SHAPE, PulseShape
from core.pulse_envelopes import GaussianPulseEnvelope


class PulseShapeTests(unittest.TestCase):
    def test_every_shape_has_unit_area_and_is_zero_outside(self):
        u = np.linspace(0.0, 1.0, 200001)
        for shape in (SQUARE_SHAPE, GAUSSIAN_SHAPE, PulseShape("gaussian", 3.0)):
            with self.subTest(shape=shape):
                self.assertAlmostEqual(float(np.trapezoid(shape.density(u), u)), 1.0, places=6)
                self.assertEqual(shape.density(np.array([-0.1, 1.1])).tolist(), [0.0, 0.0])

    def test_the_cumulative_is_the_running_area(self):
        u = np.linspace(0.0, 1.0, 20001)
        for shape in (SQUARE_SHAPE, GAUSSIAN_SHAPE):
            with self.subTest(shape=shape):
                running = np.concatenate([[0.0], np.cumsum(0.5 * (shape.density(u)[1:] + shape.density(u)[:-1])
                                                          * np.diff(u))])
                np.testing.assert_allclose(shape.cumulative(u), running, atol=1e-6)
                self.assertEqual(shape.cumulative(np.array([-1.0, 2.0])).tolist(), [0.0, 1.0])

    def test_the_gaussian_matches_the_pulse_model_envelope(self):
        duration, angle = 0.036, math.pi / 2
        envelope = GaussianPulseEnvelope.from_target_rotation_angle(angle, duration / 4.0, 2.0)
        self.assertAlmostEqual(envelope.duration_us, duration)
        u = np.array([0.0, 0.2, 0.5, 0.8, 1.0])
        np.testing.assert_allclose(GAUSSIAN_SHAPE.density(u) * angle / duration,
                                   [envelope.amplitude_rad_per_us(value * duration) for value in u], rtol=1e-12)
        np.testing.assert_allclose(GAUSSIAN_SHAPE.cumulative(u) * angle,
                                   [envelope.integrated_area_rad(value * duration) for value in u], atol=1e-12)

    def test_unknown_shapes_and_bad_widths_are_rejected(self):
        with self.assertRaises(ValueError):
            PulseShape("triangle")
        with self.assertRaises(ValueError):
            PulseShape("gaussian", 0.0)
        with self.assertRaises(ValueError):
            PulseShape("gaussian", float("nan"))


if __name__ == "__main__":
    unittest.main()
