"""Sample values for the drive-crosstalk inputs that the calibration data does not give.

The Program only knows each pulse's rotation angle and duration, and a leak onto a neighbour also depends on
the neighbour's frequency and on how the drive is spread over time. Neither is in the calibration snapshot,
so a preset has to declare them. These are rough, plausible values for a preset to start from. They are
samples, not measurements of any device; the only place that says so is the official documentation
(``formalweb/website/docs/limitations/attack-simulator.md``), and nothing else claims a basis for them.

* ``SAMPLE_PULSE_SHAPE``: a truncated Gaussian, sigma = one quarter of the duration, cut at two sigma on each
  side. This is the usual shape of a calibrated single-qubit pulse on a superconducting device.
* ``SAMPLE_DETUNING_RAD_PER_US``: 2 pi x 100 MHz between the drive on one qubit and a neighbour's transition.
  Neighbouring transmons are usually designed to differ by tens to hundreds of MHz.
"""

from __future__ import annotations

import math

from core.attack_simulation.pulse_shape import GAUSSIAN_SHAPE

SAMPLE_PULSE_SHAPE = GAUSSIAN_SHAPE
SAMPLE_DETUNING_RAD_PER_US = 2.0 * math.pi * 100.0       # 1 MHz = 1 cycle per microsecond
