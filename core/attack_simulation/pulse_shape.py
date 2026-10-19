"""Envelope shapes of a drive pulse, as area-normalised profiles over the unit interval.

The compiled Program only knows a pulse's rotation angle and duration. How the drive is spread inside
that duration matters once a leak is detuned (the leak sees the envelope's spectrum) or lands on a pulse
the receiver is itself running (the leak is carried along by the receiver's rotation, step by step). A
shape is declared, not measured: the calibration data has durations and errors only.

``square`` is constant. ``gaussian`` is the truncated Gaussian of ``core.pulse_envelopes`` centred in the
pulse, cut at ``truncation_sigma`` standard deviations on each side. Both are normalised so the area
under ``density`` over ``[0, 1]`` is 1, i.e. the rate at time t is ``angle / duration * density(t / duration)``.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np

_erf = np.vectorize(math.erf, otypes=[float])
SHAPE_KINDS = ("square", "gaussian")


@dataclass(frozen=True)
class PulseShape:
    kind: str = "square"
    truncation_sigma: float = 2.0

    def __post_init__(self) -> None:
        if self.kind not in SHAPE_KINDS:
            raise ValueError("unknown pulse shape")
        if not math.isfinite(self.truncation_sigma) or self.truncation_sigma <= 0.0:
            raise ValueError("truncation_sigma must be finite and positive")

    @property
    def _sigma(self) -> float:
        """The Gaussian's standard deviation as a fraction of the pulse duration."""
        return 0.5 / self.truncation_sigma

    def density(self, u: np.ndarray) -> np.ndarray:
        """Area-1 profile on ``[0, 1]``, zero outside."""
        u = np.asarray(u, dtype=float)
        inside = (u >= 0.0) & (u <= 1.0)
        if self.kind == "square":
            return np.where(inside, 1.0, 0.0)
        sigma = self._sigma
        norm = sigma * math.sqrt(2.0 * math.pi) * math.erf(self.truncation_sigma / math.sqrt(2.0))
        return np.where(inside, np.exp(-0.5 * ((u - 0.5) / sigma) ** 2) / norm, 0.0)

    def cumulative(self, u: np.ndarray) -> np.ndarray:
        """Fraction of the area before ``u``, clipped to ``[0, 1]``."""
        u = np.clip(np.asarray(u, dtype=float), 0.0, 1.0)
        if self.kind == "square":
            return u
        scale = self._sigma * math.sqrt(2.0)
        low, high = _erf(-0.5 / scale), _erf(0.5 / scale)
        return (_erf((u - 0.5) / scale) - low) / (high - low)


SQUARE_SHAPE = PulseShape()
GAUSSIAN_SHAPE = PulseShape("gaussian", 2.0)
