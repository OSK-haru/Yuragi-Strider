"""What reading out one qubit does to a neighbour, and what the result picks up from it (D91).

Readout resonators share a feedline and overlap in spectrum, so the tone that reads qubit ``reader`` also
puts a small, state-dependent field into the resonator of ``neighbour`` (Heinsoo et al., Phys. Rev. Applied
10, 034040 (2018), Sec. V). Two things follow, and both are measured rather than derived here:

* The neighbour is weakly measured. A Ramsey experiment on it with the reader's pulse in between loses
  contrast as ``exp(-Gamma tau)``; ``dephasing_per_us`` is that average rate ``Gamma`` over the readout
  pulse (the paper's ``Gamma_ij``; its phase-error probability is ``(1 - exp(-Gamma tau)) / 2``). The same
  photons shift the neighbour's frequency while they are there (ac Stark shift), which the same
  experiment shows as a displaced fringe; ``frequency_shift_rad_per_us`` is that shift. The cited paper
  reports only the contrast.
* The reader's assigned result depends on the neighbour's state. ``excited_assignment_shift`` is the change
  in the probability of assigning the reader to 1 when the neighbour is in |1> instead of |0>, given for the
  reader truly in |0> and truly in |1>. The paper's cross-fidelity ``F_ij = P(e_i | pi_j) - P(e_i | 0_j)``
  is this shift averaged over the reader's two states. The reader's own assignment errors are those
  measured with the neighbour in |0>.

A result that depends on the neighbour's state is information about the neighbour, so drawing it changes the
neighbour's state; the runtime applies both (``CoupledRegionWorld``). With several neighbours the shifts
add, which is all that pairwise measurements determine.
"""

from __future__ import annotations

import cmath
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class ReadoutCrosstalk:
    reader: int                                     # the physical qubit being read out
    neighbour: int                                  # the physical qubit that is affected
    dephasing_per_us: float = 0.0                   # Gamma: the neighbour's coherence decays as exp(-Gamma t)
    frequency_shift_rad_per_us: float = 0.0         # the neighbour's |1> gains this energy during the readout
    excited_assignment_shift: tuple[float, float] = (0.0, 0.0)

    def __post_init__(self) -> None:
        if (type(self.reader) is not int or type(self.neighbour) is not int or self.reader < 0
                or self.neighbour < 0 or self.reader == self.neighbour):
            raise ValueError("readout crosstalk joins two distinct physical qubits")
        for name in ("dephasing_per_us", "frequency_shift_rad_per_us"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
            object.__setattr__(self, name, float(value))
        if self.dephasing_per_us < 0.0:
            raise ValueError("dephasing_per_us must be nonnegative")
        shift = tuple(self.excited_assignment_shift)
        if (len(shift) != 2 or any(type(value) not in (int, float) or not math.isfinite(value)
                                   or abs(value) > 1.0 for value in shift)):
            raise ValueError("excited_assignment_shift is two probability changes, for the reader in |0> and |1>")
        object.__setattr__(self, "excited_assignment_shift", tuple(float(value) for value in shift))

    @property
    def disturbs(self) -> bool:
        """The readout changes the neighbour's state while it lasts."""
        return self.dephasing_per_us != 0.0 or self.frequency_shift_rad_per_us != 0.0

    @property
    def reads(self) -> bool:
        """The reader's result depends on the neighbour's state."""
        return self.excited_assignment_shift != (0.0, 0.0)

    def coherence_factor(self, duration_us: float) -> complex:
        """What ``duration_us`` of the reader's readout multiplies the neighbour's ``<1| rho |0>`` by."""
        return cmath.exp(-(self.dephasing_per_us + 1j * self.frequency_shift_rad_per_us) * duration_us)
