"""Pure single-region FIFO timing, using explicit simulated service durations.

The caller supplies FIFO jobs and the preceding physical finish/tenant. Arrival
order values are opaque ingress evidence, not slots in a fixed phase scheme.
This sidecar neither owns a queue nor establishes ingress completeness, commits
physics, or publishes results. Bounds come from the validated compiled Program;
one call plans one job and retains no history. No provider coefficients or host
elapsed time are inferred here.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from core.attack_simulation.circuit_program import CircuitExecutionOperation
from core.attack_simulation.declared_execution import JobExecutionWindow
from core.attack_simulation.event_commit_boundary import EventPosition
from core.attack_simulation.jobs import JobRecord
from core.attack_simulation.interleaved_execution import interleaved_work
from core.attack_simulation.placement import PlacementPolicy
from core.attack_simulation.shared_qpu_runtime import SharedQpuRuntimeConfig


def _duration(value: float, name: str) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be finite and nonnegative")
    try:
        converted = float(value)
    except OverflowError:
        raise ValueError(f"{name} must be finite and nonnegative") from None
    if not math.isfinite(converted) or converted < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return converted


def compiled_job_duration(
    job: JobRecord,
    runtime_config: SharedQpuRuntimeConfig,
    previous_tenant_id: str | None,
) -> float:
    """Return physical microseconds, matching DeclaredQpuExecutor._duration.

    Initialize/reset instructions use the supplied service reset duration;
    unitary durations are already compiled (including parallel-column timing).
    Final measurements charge once per distinct time_group, then the whole
    Program repeats per shot. Tenant handover adds one service reset outside
    those shots, even for inherit Programs or reset_count=0. Register width and
    physical IDs do not multiply these durations. Authorization, layout checks,
    and state ownership remain the executor's responsibility.
    """
    if type(job) is not JobRecord:
        raise TypeError("an immutable common job record is required")
    if type(runtime_config) is not SharedQpuRuntimeConfig:
        raise TypeError("an explicit common runtime configuration is required")
    if previous_tenant_id is not None and (
        type(previous_tenant_id) is not str or not previous_tenant_id.strip()
    ):
        raise ValueError("previous tenant must be a nonempty identifier or None")
    operation = job.command.operation
    if type(operation) is not CircuitExecutionOperation:
        raise TypeError("timing requires a compiled common Program")
    if runtime_config.placement_policy is PlacementPolicy.ISOLATED_PHYSICAL_RESOURCE:
        raise ValueError("declared timing does not support resource reassignment")
    if operation.interleave is not None:
        if runtime_config.placement_policy is not PlacementPolicy.GUARANTEED_SAME_PHYSICAL_QUBIT:
            raise ValueError("interleaved timing requires the shared same-region placement")
        return _duration(interleaved_work(
            operation, job.command.tenant_id, previous_tenant_id,
            runtime_config.reset_duration_us, runtime_config.program_measurement_duration_us,
        ).duration_us, "compiled job duration")
    duration = sum(
        instruction.duration_us if instruction.kind == "unitary"
        else runtime_config.reset_duration_us
        if instruction.kind in {"initialize", "reset"} else 0.0
        for instruction in operation.instructions
    )
    duration += runtime_config.program_measurement_duration_us * len({
        instruction.time_group for instruction in operation.instructions
        if instruction.kind == "measure"
    })
    duration *= operation.shots
    duration += runtime_config.repetition_delay_us * (operation.shots - 1)
    if previous_tenant_id is not None and previous_tenant_id != job.command.tenant_id:
        duration += runtime_config.reset_duration_us
    return _duration(duration, "compiled job duration")


def _after(position: EventPosition, delay_us: float) -> EventPosition:
    time_us = _duration(position.time_us + delay_us, "planned event time")
    if delay_us > 0 and time_us == position.time_us:
        raise ValueError("positive duration is not representable at this world time")
    return EventPosition(
        position.clock_domain, time_us,
        position.order + 1 if time_us == position.time_us else 0,
    )


@dataclass(frozen=True)
class DeclaredServiceTiming:
    """Explicit simulation assumptions; every field is required, including zero.

    processing_delay_us runs from arrival to service readiness, independently of
    FIFO contention. publication_delay_us runs from physical finish to possible
    result release. denial_delay_us is used for coordinator denial replies;
    it is not added to successful physical jobs. These are declared assumptions,
    not real-provider defaults or measurements of compilation/worker latency.
    """

    clock_domain: str
    processing_delay_us: float
    publication_delay_us: float
    denial_delay_us: float

    def __post_init__(self) -> None:
        EventPosition(self.clock_domain, 0, 0)
        for name in ("processing_delay_us", "publication_delay_us", "denial_delay_us"):
            object.__setattr__(self, name, _duration(getattr(self, name), name))

    def plan_job_window(
        self,
        job: JobRecord,
        runtime_config: SharedQpuRuntimeConfig,
        *,
        previous_finish: EventPosition | None,
        previous_tenant_id: str | None,
        arrival: EventPosition,
    ) -> JobExecutionWindow:
        """Plan one supplied FIFO successor without reading or mutating owners.

        START is after arrival + processing and strictly after previous FINISH's
        full key, including ties with arbitrary ingress orders. FINISH and
        release advance causally even with zero durations; tie order does not
        add physical microseconds. Publication never occupies the QPU. The
        caller owns FIFO selection, cancellation, initial tenant evidence, and
        finite queue/history limits; this function cannot infer those inputs.
        """
        for position in (arrival,) + ((previous_finish,) if previous_finish is not None else ()):
            if type(position) is not EventPosition:
                raise TypeError("explicit immutable EventPosition values are required")
            if position.clock_domain != self.clock_domain:
                raise ValueError("service timing and event clock domains differ")
        duration = compiled_job_duration(job, runtime_config, previous_tenant_id)
        start = _after(arrival, self.processing_delay_us)
        if previous_finish is not None and start.key <= previous_finish.key:
            start = _after(previous_finish, 0.0)
        finish = _after(start, duration)
        release = _after(finish, self.publication_delay_us)
        return JobExecutionWindow(arrival, start, finish, release)

    def denial_release(self, arrival: EventPosition) -> EventPosition:
        """Plan a denial reply after arrival, without inventing a physical job.

        Only denial_delay_us applies. Even zero delay advances the causal key;
        this reply reserves no QPU time or tenant ownership.
        """
        if type(arrival) is not EventPosition:
            raise TypeError("an explicit immutable EventPosition is required")
        if arrival.clock_domain != self.clock_domain:
            raise ValueError("service timing and event clock domains differ")
        return _after(arrival, self.denial_delay_us)
