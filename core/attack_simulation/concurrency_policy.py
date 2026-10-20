"""The frozen choice of how jobs of different tenants share the QPU in time, and the leaks it implies.

``exclusive_fifo`` runs one job at a time (the original behaviour). ``overlap_disjoint_regions`` lets
jobs on disjoint tenant regions run at the same time; the only coupling between them is the drive
crosstalk declared here. The product declares one strength for every physically adjacent pair of
qubits across the two regions (the coupling map's gate edges); its detuning is the sample value of
``crosstalk_samples`` (D80). Both are declared assumptions, not calibrated values.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from core.attack_simulation.crosstalk_samples import SAMPLE_DETUNING_RAD_PER_US
from core.experimental_isolation_physics.coherent_crosstalk import DriveCrosstalkTransfer

EXCLUSIVE_FIFO = "exclusive_fifo"
OVERLAP_DISJOINT_REGIONS = "overlap_disjoint_regions"
CONCURRENCY_POLICIES = (EXCLUSIVE_FIFO, OVERLAP_DISJOINT_REGIONS)

BOTH = "both"
VICTIM_TO_ATTACKER = "victim_to_attacker"
ATTACKER_TO_VICTIM = "attacker_to_victim"
CROSSTALK_DIRECTIONS = (BOTH, VICTIM_TO_ATTACKER, ATTACKER_TO_VICTIM)

DEFAULT_CROSSTALK_STRENGTH = 0.02


def cross_region_transfers(gate_edges: Iterable[Sequence[int]], victim_region: Sequence[int],
                           attacker_region: Sequence[int], strength: float,
                           direction: str) -> tuple[DriveCrosstalkTransfer, ...]:
    """One transfer per direction for each coupling edge that joins the two regions."""
    if direction not in CROSSTALK_DIRECTIONS:
        raise ValueError("unknown drive-crosstalk direction")
    if strength == 0.0:
        return ()
    victim, attacker = set(victim_region), set(attacker_region)
    if victim & attacker:
        raise ValueError("crosstalk is defined between disjoint regions")
    transfers: list[DriveCrosstalkTransfer] = []
    for left, right in sorted(tuple(sorted(edge)) for edge in gate_edges):
        pairs = ((left, right), (right, left))
        for source, target in pairs:
            if source in victim and target in attacker and direction in (BOTH, VICTIM_TO_ATTACKER):
                transfers.append(DriveCrosstalkTransfer(source, target, strength, 0.0, SAMPLE_DETUNING_RAD_PER_US))
            elif source in attacker and target in victim and direction in (BOTH, ATTACKER_TO_VICTIM):
                transfers.append(DriveCrosstalkTransfer(source, target, strength, 0.0, SAMPLE_DETUNING_RAD_PER_US))
    return tuple(transfers)
