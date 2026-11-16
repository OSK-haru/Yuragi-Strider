"""Regional lanes for concurrent sharing: one independent state store per disjoint region.

Stage 4 of the concurrent-sharing design. In ``overlap_disjoint_regions`` each tenant's region keeps
its own store (own revision, own device clock, own event chain), so two regions advance side by side.
Their only coupling is the drive leak one window puts on another, and that depends on declared
activity alone (never on results), so a job can be computed as soon as ingress is sealed through its
own finish. This module holds the lane bookkeeping and the kick assembly; the executor drives it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
import math

from core.attack_simulation.circuit_program import CircuitExecutionOperation
from core.attack_simulation.coupled_regions import (
    CoupledTransmonSettings, ResidualZZSettings, UncoupledSettings,
)
from core.attack_simulation.concurrent_windows import (
    DriveSchedule, ExecutionWindow, gap_before, gap_leaks, leaks_into,
)
from core.attack_simulation.event_commit_boundary import EventPosition
from core.attack_simulation.kick_application import EMPTY_PLAN, KickPlan, place_kicks
from core.attack_simulation.pulse_shape import SQUARE_SHAPE, PulseShape
from core.attack_simulation.qpu_state import InactiveRegionState, QpuStateSnapshot, QpuStateStore
from core.attack_simulation.shot_activity import shot_activity
from core.experimental_isolation_physics.coherent_crosstalk import DriveCrosstalkTransfer
from core.experimental_isolation_physics.readout_crosstalk import ReadoutCrosstalk


@dataclass(frozen=True)
class ConcurrentRegions:
    """Frozen choice of ``scheduling.concurrency_policy = overlap_disjoint_regions``."""

    transfers: tuple[DriveCrosstalkTransfer, ...] = ()
    pulse_shape: PulseShape = SQUARE_SHAPE
    # Cross-region residual ZZ; when present the two regions share one joint world (D85).
    residual_zz: ResidualZZSettings | None = None
    # The same coupling declared by device values instead (frequency, anharmonicity, exchange J; D91).
    transmons: CoupledTransmonSettings | None = None
    # What reading one qubit out does to a neighbour and learns from it (measured values; D91). It depends on
    # the neighbour's state, so the regions share one joint world with it as well.
    readout_crosstalk: tuple[ReadoutCrosstalk, ...] = ()

    @property
    def coupling(self) -> ResidualZZSettings | CoupledTransmonSettings | None:
        """The coupling Hamiltonian between the two regions, if one is declared."""
        return self.residual_zz if self.residual_zz is not None else self.transmons

    @property
    def kick_transfers(self) -> tuple[DriveCrosstalkTransfer, ...]:
        """The transfers whose leaks become kicks.

        All of them, except those between two qubits of one cluster that the reference solver advances at pulse
        level: the cluster's own propagator carries those leaks (D91).
        """
        if self.transmons is None or self.transmons.solver != "reference":
            return self.transfers
        group: dict[int, int] = {qubit: qubit for qubit, _ in self.transmons.transmons}

        def root(qubit: int) -> int:
            while group[qubit] != qubit:
                qubit = group[qubit]
            return qubit

        for left, right, _ in self.transmons.exchange:
            group[root(left)] = root(right)
        return tuple(item for item in self.transfers
                     if not (item.source in group and item.target in group
                             and root(item.source) == root(item.target)))

    @property
    def world_settings(self) -> ResidualZZSettings | CoupledTransmonSettings | UncoupledSettings | None:
        """How the regions' joint world is advanced, or None when nothing joins their states."""
        if self.coupling is not None:
            return self.coupling
        return UncoupledSettings() if self.readout_crosstalk else None

    def __post_init__(self) -> None:
        crosstalk = tuple(self.readout_crosstalk)
        if any(type(item) is not ReadoutCrosstalk for item in crosstalk):
            raise TypeError("readout_crosstalk must contain ReadoutCrosstalk values")
        if len({(item.reader, item.neighbour) for item in crosstalk}) != len(crosstalk):
            raise ValueError("a readout-crosstalk pair is declared twice")
        object.__setattr__(self, "readout_crosstalk", crosstalk)
        if self.residual_zz is not None and type(self.residual_zz) is not ResidualZZSettings:
            raise TypeError("residual_zz must be ResidualZZSettings")
        if self.transmons is not None and type(self.transmons) is not CoupledTransmonSettings:
            raise TypeError("transmons must be CoupledTransmonSettings")
        if self.residual_zz is not None and self.transmons is not None:
            raise ValueError("declare the coupling once: measured ZZ or device values, not both")
        transfers = tuple(self.transfers)
        if any(type(item) is not DriveCrosstalkTransfer for item in transfers):
            raise TypeError("transfers must contain DriveCrosstalkTransfer values")
        object.__setattr__(self, "transfers", transfers)
        if self.transmons is not None:
            # A leak's detuning is the receiver's frequency minus the source's. Where the device values
            # give both, a transfer may not state a different one.
            frequency = {qubit: spec.frequency_ghz for qubit, spec in self.transmons.transmons}
            for item in transfers:
                if item.source in frequency and item.target in frequency:
                    expected = 2.0 * math.pi * 1e3 * (frequency[item.target] - frequency[item.source])
                    if abs(item.detuning_rad_per_us - expected) > 1e-6 * max(1.0, abs(expected)):
                        raise ValueError("a transfer's detuning must be the declared frequency of its target "
                                         "minus that of its source")
        if type(self.pulse_shape) is not PulseShape:
            raise TypeError("a PulseShape is required")


@dataclass
class RegionLane:
    """One region's independent authority and its running job."""

    region: tuple[int, ...]
    store: QpuStateStore
    end_us: float = 0.0
    active: tuple | None = None
    prepared: object | None = None
    last_window: ExecutionWindow | None = None
    last_finish: EventPosition | None = None


def build_lane_stores(base: QpuStateStore) -> dict[tuple[int, ...], QpuStateStore]:
    """Split a fresh two-region store into one single-region-active store per region.

    Each lane store keeps the other region only as a placeholder: a lane never runs another tenant's
    job, so it never switches regions and the placeholder is never evolved or read.
    """
    first, parked = base.read(), base.inactive_region()
    if parked is None or first.revision != 0 or first.device_time_us != 0.0 or parked.updated_at_us != 0.0:
        raise ValueError("concurrent regions need a fresh two-region store")
    bindings = {binding.physical_qubit_ids: binding for binding in base.regional_bindings}
    second = replace(first, physical_qubit_ids=parked.physical_qubit_ids, local_dimension=parked.local_dimension,
                     density_matrix=parked.density_matrix, owner_tenant_id=parked.owner_tenant_id)
    placeholder_for_second = InactiveRegionState(first.physical_qubit_ids, first.density_matrix,
                                                 first.owner_tenant_id, 0.0, first.local_dimension)
    stores = {}
    for snapshot, other in ((first, parked), (second, placeholder_for_second)):
        stores[snapshot.physical_qubit_ids] = QpuStateStore(
            snapshot, region_binding=bindings[snapshot.physical_qubit_ids],
            alternate_region_binding=bindings[other.physical_qubit_ids],
            inactive_region=InactiveRegionState(other.physical_qubit_ids, other.density_matrix,
                                                other.owner_tenant_id, 0.0, other.local_dimension))
    return stores


def execution_window(job_id: str, tenant_id: str, operation: CircuitExecutionOperation,
                     layout_qubit_ids: Sequence[int], region: Sequence[int], start: EventPosition,
                     finish: EventPosition, *, reset_duration_us: float, measurement_duration_us: float,
                     repetition_delay_us: float, pulse_shape: PulseShape, handover: bool = False,
                     schedule: DriveSchedule | None = None) -> ExecutionWindow:
    """The job's window with the whole region as receivers (program qubits first, then the rest).

    The region's qubits outside the program receive leaks too; ``place_kicks`` treats them as qubits
    without a program of their own. ``handover`` is the service reset before the first shot when the
    region changes tenant (one reset duration); it drives nothing and delays the shots.
    """
    if schedule is None or schedule.handover != handover:
        schedule = DriveSchedule(
            shot_activity(operation, reset_duration_us=reset_duration_us,
                          measurement_duration_us=measurement_duration_us, pulse_shape=pulse_shape),
            operation.shots, repetition_delay_us, reset_duration_us if handover else 0.0, handover)
    layout = tuple(layout_qubit_ids)
    qubits = layout + tuple(q for q in region if q not in layout)
    return ExecutionWindow(job_id, tenant_id, qubits, start, finish, schedule)


def kicks_for(target: ExecutionWindow, previous: ExecutionWindow | None,
              others: Sequence[ExecutionWindow], config: ConcurrentRegions,
              operation: CircuitExecutionOperation, layout_qubit_ids: Sequence[int],
              reset_duration_us: float, measurement_duration_us: float = 0.0) -> KickPlan:
    """Every leak onto ``target`` (its own window plus the gap since ``previous``) as placed kicks.

    ``others`` are the windows of the other regions; the caller must hold all of them that can touch
    ``[previous.finish, target.finish)`` (sealed ingress guarantees that none can still arrive).
    """
    transfers = config.kick_transfers
    if not transfers or not others:
        return EMPTY_PLAN
    leaks = leaks_into(target, others, transfers)
    leaks += gap_leaks(others, target.physical_qubits, *gap_before(target, previous), transfers)
    if not leaks:
        return EMPTY_PLAN
    return place_kicks(leaks, operation, target.schedule, window_start_us=target.start.time_us,
                       reset_duration_us=reset_duration_us, layout_qubit_ids=layout_qubit_ids,
                       measurement_duration_us=measurement_duration_us)
