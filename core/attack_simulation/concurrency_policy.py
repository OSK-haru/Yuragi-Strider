"""The frozen choice of how jobs of different tenants share the QPU in time, and the leaks it implies.

``exclusive_fifo`` runs one job at a time (the original behaviour). ``overlap_disjoint_regions`` lets
jobs on disjoint tenant regions run at the same time; the only coupling between them is the drive
crosstalk declared here. The product declares one strength for every physically adjacent pair of
qubits across the two regions (the coupling map's gate edges); its detuning is the sample value of
``crosstalk_samples`` (D80). Both are declared assumptions, not calibrated values.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import math

from core.experimental_isolation_physics.coherent_crosstalk import DriveCrosstalkTransfer

EXCLUSIVE_FIFO = "exclusive_fifo"
OVERLAP_DISJOINT_REGIONS = "overlap_disjoint_regions"
CONCURRENCY_POLICIES = (EXCLUSIVE_FIFO, OVERLAP_DISJOINT_REGIONS)

BOTH = "both"
VICTIM_TO_ATTACKER = "victim_to_attacker"
ATTACKER_TO_VICTIM = "attacker_to_victim"
CROSSTALK_DIRECTIONS = (BOTH, VICTIM_TO_ATTACKER, ATTACKER_TO_VICTIM)

# Compatibility for the current product scenario builder; new scenario code
# should persist a strength on each CrosstalkDeclaration.
DEFAULT_CROSSTALK_STRENGTH = 0.02

@dataclass(frozen=True)
class CrosstalkDeclaration:
    """One scenario-declared directional transfer between named resource regions."""

    src_region: str
    dst_region: str
    strength: float
    detuning_rad_per_us: float

    def __post_init__(self) -> None:
        if (not isinstance(self.src_region, str) or not self.src_region.strip()
                or not isinstance(self.dst_region, str) or not self.dst_region.strip()):
            raise ValueError("crosstalk region IDs must be nonempty")
        object.__setattr__(self, "src_region", self.src_region.strip())
        object.__setattr__(self, "dst_region", self.dst_region.strip())
        if self.src_region == self.dst_region:
            raise ValueError("crosstalk source and destination regions must differ")
        if (type(self.strength) not in (int, float) or not math.isfinite(float(self.strength))
                or not 0.0 <= self.strength <= 1.0
                or type(self.detuning_rad_per_us) not in (int, float)
                or not math.isfinite(float(self.detuning_rad_per_us))):
            raise ValueError("invalid crosstalk declaration")
        object.__setattr__(self, "strength", float(self.strength))
        object.__setattr__(self, "detuning_rad_per_us", float(self.detuning_rad_per_us))


def cross_region_transfers(
    gate_edges: Iterable[Sequence[int]],
    regions: Mapping[str, Sequence[int]] | Sequence[int],
    declarations: Sequence[CrosstalkDeclaration] | Sequence[int],
    strength: float | None = None,
    direction: str | None = None,
) -> tuple[DriveCrosstalkTransfer, ...]:
    """Expand declared region-to-region transfers over physical coupling edges."""
    # Scenario adapter for the existing two-region product contract. New callers
    # pass the named region map and declaration list above.
    if not isinstance(regions, Mapping):
        if strength is None or direction not in CROSSTALK_DIRECTIONS:
            raise ValueError("legacy pair input requires strength and a known direction")
        region_map = {"victim": regions, "attacker": declarations}
        pairs = (("victim", "attacker"), ("attacker", "victim"))
        if direction == VICTIM_TO_ATTACKER:
            pairs = pairs[:1]
        elif direction == ATTACKER_TO_VICTIM:
            pairs = pairs[1:]
        from core.attack_simulation.crosstalk_samples import SAMPLE_DETUNING_RAD_PER_US
        declared = tuple(CrosstalkDeclaration(src, dst, float(strength),
                                               SAMPLE_DETUNING_RAD_PER_US)
                         for src, dst in pairs)
        return cross_region_transfers(gate_edges, region_map, declared)
    if strength is not None or direction is not None:
        raise ValueError("strength and direction belong in declaration entries")
    if len({item for values in regions.values() for item in values}) != sum(map(len, regions.values())):
        raise ValueError("declared regions must be physically disjoint")
    transfers: list[DriveCrosstalkTransfer] = []
    edges = tuple(sorted(tuple(sorted(edge)) for edge in gate_edges))
    for declaration in declarations:
        if declaration.strength == 0.0:
            continue
        if declaration.src_region not in regions or declaration.dst_region not in regions:
            raise ValueError("crosstalk declaration references an unknown region")
        source_region, target_region = set(regions[declaration.src_region]), set(regions[declaration.dst_region])
        for left, right in edges:
            if left in source_region and right in target_region:
                source, target = left, right
            elif right in source_region and left in target_region:
                source, target = right, left
            else:
                continue
            transfers.append(DriveCrosstalkTransfer(
                source, target, declaration.strength, 0.0, declaration.detuning_rad_per_us,
            ))
    return tuple(transfers)
