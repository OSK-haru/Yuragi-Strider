"""Regional lanes for concurrent sharing: one independent state store per disjoint region.

Stage 4 of the concurrent-sharing design. In ``overlap_disjoint_regions`` each tenant's region keeps
its own store (own revision, own device clock, own event chain), so two regions advance side by side.
Their only coupling is the drive leak one window puts on another, and that depends on declared
activity alone (never on results), so a job can be computed as soon as ingress is sealed through its
own finish. This module holds the lane bookkeeping and the kick assembly; the executor drives it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from core.attack_simulation.circuit_program import CircuitExecutionOperation
from core.attack_simulation.concurrent_windows import (
    DriveSchedule, ExecutionWindow, gap_before, gap_leaks, leaks_into,
)
from core.attack_simulation.event_commit_boundary import EventPosition
from core.attack_simulation.kick_application import EMPTY_PLAN, KickPlan, place_kicks
from core.attack_simulation.pulse_shape import SQUARE_SHAPE, PulseShape
from core.attack_simulation.qpu_state import InactiveRegionState, QpuStateSnapshot, QpuStateStore
from core.attack_simulation.shot_activity import shot_activity
from core.experimental_isolation_physics.coherent_crosstalk import DriveCrosstalkTransfer


@dataclass(frozen=True)
class ConcurrentRegions:
    """Frozen choice of ``scheduling.concurrency_policy = overlap_disjoint_regions``."""

    transfers: tuple[DriveCrosstalkTransfer, ...] = ()
    pulse_shape: PulseShape = SQUARE_SHAPE

    def __post_init__(self) -> None:
        transfers = tuple(self.transfers)
        if any(type(item) is not DriveCrosstalkTransfer for item in transfers):
            raise TypeError("transfers must contain DriveCrosstalkTransfer values")
        object.__setattr__(self, "transfers", transfers)
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
    second = replace(first, physical_qubit_ids=parked.physical_qubit_ids,
                     density_matrix=parked.density_matrix, owner_tenant_id=parked.owner_tenant_id)
    placeholder_for_second = InactiveRegionState(first.physical_qubit_ids, first.density_matrix,
                                                 first.owner_tenant_id, 0.0)
    stores = {}
    for snapshot, other in ((first, parked), (second, placeholder_for_second)):
        stores[snapshot.physical_qubit_ids] = QpuStateStore(
            snapshot, region_binding=bindings[snapshot.physical_qubit_ids],
            alternate_region_binding=bindings[other.physical_qubit_ids],
            inactive_region=InactiveRegionState(other.physical_qubit_ids, other.density_matrix,
                                                other.owner_tenant_id, 0.0))
    return stores


def execution_window(job_id: str, tenant_id: str, operation: CircuitExecutionOperation,
                     layout_qubit_ids: Sequence[int], region: Sequence[int], start: EventPosition,
                     finish: EventPosition, *, reset_duration_us: float, measurement_duration_us: float,
                     repetition_delay_us: float, pulse_shape: PulseShape,
                     schedule: DriveSchedule | None = None) -> ExecutionWindow:
    """The job's window with the whole region as receivers (program qubits first, then the rest).

    The region's qubits outside the program receive leaks too; ``place_kicks`` treats them as qubits
    without a program of their own.
    """
    if schedule is None:
        schedule = DriveSchedule(
            shot_activity(operation, reset_duration_us=reset_duration_us,
                          measurement_duration_us=measurement_duration_us, pulse_shape=pulse_shape),
            operation.shots, repetition_delay_us)
    layout = tuple(layout_qubit_ids)
    qubits = layout + tuple(q for q in region if q not in layout)
    return ExecutionWindow(job_id, tenant_id, qubits, start, finish, schedule)


def kicks_for(target: ExecutionWindow, previous: ExecutionWindow | None,
              others: Sequence[ExecutionWindow], config: ConcurrentRegions,
              operation: CircuitExecutionOperation, layout_qubit_ids: Sequence[int],
              reset_duration_us: float) -> KickPlan:
    """Every leak onto ``target`` (its own window plus the gap since ``previous``) as placed kicks.

    ``others`` are the windows of the other regions; the caller must hold all of them that can touch
    ``[previous.finish, target.finish)`` (sealed ingress guarantees that none can still arrive).
    """
    if not config.transfers or not others:
        return EMPTY_PLAN
    leaks = leaks_into(target, others, config.transfers)
    leaks += gap_leaks(others, target.physical_qubits, *gap_before(target, previous), config.transfers)
    if not leaks:
        return EMPTY_PLAN
    return place_kicks(leaks, operation, target.schedule, window_start_us=target.start.time_us,
                       reset_duration_us=reset_duration_us, layout_qubit_ids=layout_qubit_ids)


def lane_key_of_tenant(tenant_regions: Mapping[str, tuple[int, ...]], tenant_id: str) -> tuple[int, ...]:
    return tenant_regions[tenant_id]
