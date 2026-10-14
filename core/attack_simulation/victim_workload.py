"""Finite actor-neutral workload schema and deterministic, pure advancement.

No sleeping, callbacks, host clock, truth access, QPU writes or mode branches.
The trusted coordinator owns the immutable state and must publish candidates
and causal barriers BEFORE calling the authorized client. Result availability
is an explicit world input from the service/transport owner, never host worker
completion. This is not a live runner or an atomic QPU/frontier transaction.
"""

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
from typing import Protocol

from core.attack_simulation.event_commit_boundary import EventPosition
from core.attack_simulation.job_service import JobResultView, JobStatusView
from core.attack_simulation.jobs import JobStatus, TERMINAL_JOB_STATUSES
from core.attack_simulation.program_request import (
    CircuitProgram, PROGRAM_INITIALIZATIONS, ProgramSubmitRequest,
)


def _identifier(value: str) -> None:
    if (type(value) is not str or not 1 <= len(value) <= 128
            or not value.isascii() or any(c.isspace() for c in value)):
        raise ValueError("a bounded nonempty ASCII identifier is required")


def _duration(value: float) -> None:
    EventPosition("duration", value, 0)


class Outcome(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class DeclaredDurations:
    """Explicit µs intervals; zero is a declaration, not an omitted default.

    classical_us precedes this operation, outbound_us ends at common ingress,
    inbound_us starts at service result availability for this operation.
    Actual browser-to-server transport is outside these simulated intervals.
    """
    classical_us: float
    outbound_us: float
    inbound_us: float

    def __post_init__(self) -> None:
        for value in (self.classical_us, self.outbound_us, self.inbound_us):
            _duration(value)


@dataclass(frozen=True)
class RegisteredProgram:
    program_id: str
    program: CircuitProgram

    def __post_init__(self) -> None:
        _identifier(self.program_id)
        if type(self.program) is not CircuitProgram:
            raise TypeError("only registered immutable Programs are supported")
        # Even a directly constructed CircuitProgram must satisfy ingestion bounds.
        document = self.program.to_circuit_dict()
        # The canonical core representation uses "0"/"1", whereas the editor
        # ingestion contract accepts integer preparation labels. Normalize only
        # that representation difference, retaining the common validator.
        document["initial_states"] = [int(s) if s in ("0", "1") else s
                                      for s in document.get("initial_states", [])]
        validated = CircuitProgram.from_editor_json(json.dumps(document, allow_nan=False))
        object.__setattr__(self, "program", validated)


@dataclass(frozen=True)
class CountAtLeast:
    """The only v1 counts predicate: a bounded own-result count comparison."""
    bitstring: str
    minimum: int

    def __post_init__(self) -> None:
        if (type(self.bitstring) is not str or not 1 <= len(self.bitstring) <= 32
                or set(self.bitstring) - {"0", "1"}
                or type(self.minimum) is not int or not 0 <= self.minimum <= 2**31 - 1):
            raise ValueError("invalid counts predicate")


@dataclass(frozen=True)
class Branch:
    outcome: Outcome
    next_step: str
    predicate: CountAtLeast | None = None

    def __post_init__(self) -> None:
        _identifier(self.next_step)
        if type(self.outcome) is not Outcome:
            raise TypeError("branch outcome must be typed")
        if self.predicate is not None and (type(self.predicate) is not CountAtLeast
                                         or self.outcome is not Outcome.SUCCEEDED):
            raise ValueError("counts branches require a successful own result")


@dataclass(frozen=True)
class WorkloadStep:
    """max_visits bounds this step per independent start's causal chain."""
    step_id: str
    program_ref: str
    device_id: str
    shots: int
    durations: DeclaredDurations
    max_visits: int
    branches: tuple[Branch, ...] = ()
    operation_id: str = "program.submit"
    initialization: str = "prepare"

    def __post_init__(self) -> None:
        for value in (self.step_id, self.program_ref, self.device_id):
            _identifier(value)
        if self.operation_id != "program.submit":
            raise ValueError("v1 supports the common program.submit operation only")
        if type(self.initialization) is not str or self.initialization not in PROGRAM_INITIALIZATIONS:
            raise ValueError("initialization must use the common prepare/inherit contract")
        if type(self.shots) is not int or not 1 <= self.shots <= 2**31 - 1:
            raise ValueError("shots must be bounded and positive")
        if type(self.max_visits) is not int or not 1 <= self.max_visits <= 4096:
            raise ValueError("every step, including cycles, needs a finite visit bound")
        if type(self.durations) is not DeclaredDurations:
            raise TypeError("explicit declared durations are required")
        if type(self.branches) is not tuple or len(self.branches) > 16:
            raise ValueError("branches must be a bounded immutable tuple")
        fallbacks: set[Outcome] = set()
        for branch in self.branches:
            if type(branch) is not Branch or branch.outcome in fallbacks:
                raise ValueError("fallback must be the last branch for its outcome")
            if branch.predicate is None:
                fallbacks.add(branch.outcome)


@dataclass(frozen=True)
class TimedStart:
    step_id: str
    at_us: float

    def __post_init__(self) -> None:
        _identifier(self.step_id)
        _duration(self.at_us)


@dataclass(frozen=True)
class WorkloadPlan:
    plan_id: str
    actor_id: str
    clock_domain: str
    programs: tuple[RegisteredProgram, ...]
    steps: tuple[WorkloadStep, ...]
    starts: tuple[TimedStart, ...]
    max_events: int
    max_pending: int
    version: int = 1

    def __post_init__(self) -> None:
        _identifier(self.plan_id)
        _identifier(self.actor_id)
        EventPosition(self.clock_domain, 0, 0)
        if type(self.version) is not int or self.version != 1:
            raise ValueError("unsupported workload version")
        for values, kind, limit in ((self.programs, RegisteredProgram, 64),
                                    (self.steps, WorkloadStep, 256),
                                    (self.starts, TimedStart, 256)):
            if (type(values) is not tuple or not 1 <= len(values) <= limit
                    or any(type(value) is not kind for value in values)):
                raise ValueError("plan collections must be bounded immutable tuples")
        if (type(self.max_events) is not int or not 1 <= self.max_events <= 4096
                or type(self.max_pending) is not int
                or not 1 <= self.max_pending <= self.max_events):
            raise ValueError("explicit bounded event and pending limits are required")
        programs = {p.program_id for p in self.programs}
        steps = {s.step_id for s in self.steps}
        if len(programs) != len(self.programs) or len(steps) != len(self.steps):
            raise ValueError("duplicate Program or step identifier")
        if any(s.program_ref not in programs or any(b.next_step not in steps for b in s.branches)
               for s in self.steps) or any(s.step_id not in steps for s in self.starts):
            raise ValueError("unknown Program or step reference")
        by_id = {s.step_id: s for s in self.steps}
        chain_bound = 0
        for start in self.starts:
            reachable: set[str] = set()
            pending = [start.step_id]
            while pending:  # At most 256 nodes; cycles use the visited set.
                current = pending.pop()
                if current not in reachable:
                    reachable.add(current)
                    pending.extend(b.next_step for b in by_id[current].branches)
            chain_bound += sum(by_id[s].max_visits for s in reachable)
        if max(sum(s.max_visits for s in self.steps), chain_bound) > self.max_events:
            raise ValueError("declared visit bounds exceed event capacity")
        if len(self.starts) > self.max_pending:
            raise WorkloadCapacityError("initial independent group exceeds pending capacity")


class WorkloadCapacityError(RuntimeError):
    """Host bookkeeping capacity, never an attack or simulated service failure."""


class Phase(str, Enum):
    SCHEDULED = "scheduled"
    DISPATCHING = "dispatching"
    WAITING = "waiting"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class WorkloadEvent:
    event_id: int
    step_id: str
    arrival: EventPosition
    phase: Phase = Phase.SCHEDULED
    job_id: str | None = None
    outcome: Outcome | None = None
    result_arrival: EventPosition | None = None


@dataclass(frozen=True)
class CausalBarrier:
    """Lower bound on a possible next arrival, with the producer explicitly named.

    This is NOT a WorldIngressTicket: an unresolved result must permit its own
    producer's physical commit. A dependency-aware coordinator must constrain
    sealing against these barriers and atomically replace them with successors.
    Simply dropping them into the current ingress frontier can deadlock.
    """
    producer_event_id: int
    earliest_next_arrival: EventPosition


@dataclass(frozen=True)
class WorkloadState:
    """Trusted coordinator-owned value, not a participant restoration payload.

    Publish ingress_reservations() and barriers() together under the owner's
    lock. The common client's completed admission records replace reservations;
    only advance_result() replaces causal barriers with known future candidates.
    """
    plan: WorkloadPlan
    run_id: str
    events: tuple[WorkloadEvent, ...]
    closed: bool = False

    @property
    def finished(self) -> bool:
        return all(e.phase is Phase.TERMINAL for e in self.events)

    def candidates(self) -> tuple[WorkloadEvent, ...]:
        return tuple(sorted((e for e in self.events if e.phase is Phase.SCHEDULED),
                            key=lambda e: e.arrival.key))

    def ingress_reservations(self) -> tuple[WorkloadEvent, ...]:
        """Known arrivals including a dispatch whose common admission is unresolved."""
        return tuple(sorted((e for e in self.events if e.phase in
                             (Phase.SCHEDULED, Phase.DISPATCHING)),
                            key=lambda e: e.arrival.key))

    def barriers(self) -> tuple[CausalBarrier, ...]:
        barriers = []
        for event in self.events:
            if event.phase not in (Phase.DISPATCHING, Phase.WAITING):
                continue
            step = _step(self.plan, event.step_id)
            successors = [_step(self.plan, b.next_step) for b in step.branches
                          if _visits(self, b.next_step, event.event_id)
                          < _step(self.plan, b.next_step).max_visits]
            if successors:
                earliest = min(event.arrival.time_us + step.durations.inbound_us
                               + s.durations.classical_us + s.durations.outbound_us
                               for s in successors)
                barriers.append(CausalBarrier(event.event_id, EventPosition(
                    self.plan.clock_domain, earliest,
                    event.arrival.order + 1 if earliest == event.arrival.time_us else 0)))
        return tuple(barriers)


def _step(plan: WorkloadPlan, step_id: str) -> WorkloadStep:
    return next(s for s in plan.steps if s.step_id == step_id)


def _visits(state: WorkloadState, step_id: str, producer_id: int) -> int:
    # Each independent start has its own finite chain. Neither its repeat bound
    # nor its tie order may be consumed by another worker finishing first.
    width = len(state.plan.starts)
    root = (producer_id - 1) % width
    return sum(e.step_id == step_id and (e.event_id - 1) % width == root for e in state.events)


def _event(state: WorkloadState, event_id: int) -> WorkloadEvent:
    if state.closed:
        raise RuntimeError("workload is closed; restart/recovery is not implicit")
    if type(event_id) is not int:
        raise ValueError("unknown workload event")
    for event in state.events:
        if event.event_id == event_id:
            return event
    raise ValueError("unknown workload event")


def _replace_event(state: WorkloadState, updated: WorkloadEvent) -> WorkloadState:
    return replace(state, events=tuple(updated if e.event_id == updated.event_id else e
                                       for e in state.events))


def start_workload(plan: WorkloadPlan, *, run_id: str) -> WorkloadState:
    """Explicit start after trusted assembly, shared by free/challenge sessions."""
    if type(plan) is not WorkloadPlan:
        raise TypeError("a frozen validated workload plan is required")
    _identifier(run_id)
    events = []
    for index, start in enumerate(plan.starts, 1):
        step = _step(plan, start.step_id)
        arrival = start.at_us + step.durations.classical_us + step.durations.outbound_us
        events.append(WorkloadEvent(index, step.step_id,
                                    EventPosition(plan.clock_domain, arrival, index)))
    return WorkloadState(plan, run_id, tuple(events))


def due_events(state: WorkloadState, through: EventPosition) -> tuple[WorkloadEvent, ...]:
    if state.closed:
        raise RuntimeError("workload is closed")
    if type(through) is not EventPosition or through.clock_domain != state.plan.clock_domain:
        raise ValueError("explicit matching world position is required")
    return tuple(e for e in state.candidates() if e.arrival.key <= through.key)


def begin_dispatch(state: WorkloadState, event_id: int, *,
                   through: EventPosition) -> WorkloadState:
    """Publish returned state/barriers BEFORE external submission, under owner lock."""
    event = _event(state, event_id)
    if event.phase is not Phase.SCHEDULED:
        raise ValueError("operation is already dispatched")
    if event not in due_events(state, through):
        raise ValueError("operation is not due at the explicit world position")
    return _replace_event(state, replace(event, phase=Phase.DISPATCHING))


class AuthorizedWorkloadClient(Protocol):
    """Trusted assembler binds identity; methods MUST use common auth/budget paths.

    submit_declared_program registers the simulated arrival at a separate trusted
    boundary. It must NOT reuse a browser's host-receipt timestamp or expose
    declared backdating to participants. No existing live adapter implements it.
    """
    @property
    def principal_id(self) -> str: ...

    def submit_declared_program(self, request: ProgramSubmitRequest, *,
                                arrival: EventPosition) -> JobStatusView: ...

    def get_result(self, job_id: str) -> JobResultView: ...


@dataclass(frozen=True)
class Submission:
    job_id: str | None
    denied: bool = False

    def __post_init__(self) -> None:
        if type(self.denied) is not bool or (self.job_id is None) != self.denied:
            raise ValueError("submission must identify a job or an explicit denial")
        if self.job_id is not None:
            _identifier(self.job_id)


def _client(state: WorkloadState, client: AuthorizedWorkloadClient) -> None:
    if client.principal_id != state.plan.actor_id:
        raise PermissionError("workload client belongs to another actor")


def dispatch_operation(state: WorkloadState, event_id: int,
                       client: AuthorizedWorkloadClient) -> Submission:
    """One authorized call; no retries, eval, arbitrary callback or hidden service."""
    event = _event(state, event_id)
    if event.phase is not Phase.DISPATCHING:
        raise ValueError("publish dispatch reservation before calling the client")
    step = _step(state.plan, event.step_id)
    program = next(p.program for p in state.plan.programs if p.program_id == step.program_ref)
    key = hashlib.sha256(repr((state.run_id, state.plan.plan_id, event_id)).encode()).hexdigest()
    request = ProgramSubmitRequest(program, step.device_id, step.shots, key,
                                   initialization=step.initialization)
    try:
        _client(state, client)
        view = client.submit_declared_program(request, arrival=event.arrival)
    except PermissionError:
        return Submission(None, denied=True)
    if type(view) is not JobStatusView or type(view.status) is not JobStatus:
        raise TypeError("client must return the common projected job status")
    return Submission(view.job_id)


def bind_submission(state: WorkloadState, event_id: int, submission: Submission) -> WorkloadState:
    event = _event(state, event_id)
    if event.phase is not Phase.DISPATCHING or type(submission) is not Submission:
        raise ValueError("expected a reserved operation and typed submission")
    if submission.job_id is not None and any(e.job_id == submission.job_id for e in state.events):
        raise ValueError("one common job cannot represent multiple operations")
    return _replace_event(state, replace(event, phase=Phase.WAITING,
                                        job_id=submission.job_id,
                                        outcome=Outcome.DENIED if submission.denied else None))


@dataclass(frozen=True)
class OwnProjectedResult:
    job_id: str
    outcome: Outcome
    shots: int | None = None
    counts: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.job_id)
        if type(self.outcome) is not Outcome:
            raise ValueError("job outcome must be a terminal projected result")
        if type(self.counts) is not tuple or len(self.counts) > 256:
            raise ValueError("projected counts must be bounded and immutable")
        if self.shots is None:
            if self.counts:
                raise ValueError("counts require declared shots")
            return
        if type(self.shots) is not int or not 0 <= self.shots <= 2**31 - 1:
            raise ValueError("invalid projected shots")
        seen = set()
        for pair in self.counts:
            if type(pair) is not tuple or len(pair) != 2:
                raise ValueError("invalid projected count pair")
            bits, count = pair
            CountAtLeast(bits, count)
            if bits in seen:
                raise ValueError("duplicate projected count key")
            seen.add(bits)
        if sum(count for _, count in self.counts) != self.shots:
            raise ValueError("projected counts do not sum to shots")


def read_own_result(state: WorkloadState, event_id: int,
                    client: AuthorizedWorkloadClient) -> OwnProjectedResult:
    event = _event(state, event_id)
    if event.phase is not Phase.WAITING or event.job_id is None:
        raise ValueError("no own job is awaiting a result")
    _client(state, client)
    view = client.get_result(event.job_id)
    if type(view) is not JobResultView or view.job_id != event.job_id:
        raise PermissionError("result is not the requested own projected job")
    if type(view.status) is not JobStatus:
        raise TypeError("job status must be the common typed projection")
    if view.status not in TERMINAL_JOB_STATUSES or view.status is JobStatus.INTERNAL_ERROR:
        raise RuntimeError("result is pending or the infrastructure requires recovery")
    outcome = {JobStatus.SUCCEEDED: Outcome.SUCCEEDED, JobStatus.REJECTED: Outcome.DENIED,
               JobStatus.FAILED: Outcome.FAILED, JobStatus.CANCELLED: Outcome.CANCELLED}[view.status]
    if view.result is None:
        return OwnProjectedResult(view.job_id, outcome)
    if type(view.result) is not dict or set(view.result) != {"shots", "counts"}:
        raise ValueError("only common projected shots/counts are allowed")
    counts = view.result["counts"]
    if type(counts) is not dict or len(counts) > 256:
        raise ValueError("projected counts exceed v1 capacity")
    return OwnProjectedResult(view.job_id, outcome, view.result["shots"], tuple(counts.items()))


def advance_result(state: WorkloadState, event_id: int, *,
                   available: EventPosition,
                   result: OwnProjectedResult | None = None) -> WorkloadState:
    """Replace dependency with successor using declared availability + durations.

    available is service release after physical commit, supplied by its trusted
    owner, BEFORE inbound transport. It is not a host completion timestamp.
    Denials still require an explicit availability; no transport value is guessed.
    """
    event = _event(state, event_id)
    if event.phase is not Phase.WAITING:
        raise ValueError("operation is not awaiting completion")
    if (type(available) is not EventPosition or available.clock_domain != state.plan.clock_domain
            or available.key < event.arrival.key):
        raise ValueError("result availability must follow this operation in the same world")
    if event.outcome is Outcome.DENIED:
        if result is not None:
            raise ValueError("a denied submission has no job result")
        outcome, counts = Outcome.DENIED, {}
    else:
        if type(result) is not OwnProjectedResult or result.job_id != event.job_id:
            raise PermissionError("only this operation's projected result can advance it")
        outcome, counts = result.outcome, dict(result.counts)
    step = _step(state.plan, event.step_id)
    arrival = EventPosition(state.plan.clock_domain,
                            available.time_us + step.durations.inbound_us, available.order)
    next_state = _replace_event(state, replace(event, phase=Phase.TERMINAL,
                                               outcome=outcome, result_arrival=arrival))
    branch = next((b for b in step.branches if b.outcome is outcome and
                   (b.predicate is None or (result is not None and result.shots is not None
                    and counts.get(b.predicate.bitstring, 0) >= b.predicate.minimum))), None)
    if branch is None:
        return next_state  # Explicit finite termination, including unhandled failure.
    successor = _step(state.plan, branch.next_step)
    if _visits(state, successor.step_id, event.event_id) >= successor.max_visits:
        return next_state  # Exhausted declared repeat bound, never an infinite cycle.
    if (len(state.events) >= state.plan.max_events or
            sum(e.phase is not Phase.TERMINAL for e in next_state.events) >= state.plan.max_pending):
        raise WorkloadCapacityError("workload capacity reached; preserve unresolved state")
    # Reserve deterministic IDs by root and chain depth, not callback order.
    event_id = event.event_id + len(state.plan.starts)
    time_us = arrival.time_us + successor.durations.classical_us + successor.durations.outbound_us
    # Injective local tie order for zero-delay successors sharing a release.
    # Integration still owns merging local orders with other ingress sources.
    radix = state.plan.max_events * len(state.plan.starts) + 1
    order = (arrival.order + 1) * radix + event_id if time_us == arrival.time_us else event_id
    new_event = WorkloadEvent(event_id, successor.step_id,
                             EventPosition(state.plan.clock_domain, time_us, order))
    return replace(next_state, events=tuple(sorted(next_state.events + (new_event,),
                                                  key=lambda e: e.event_id)))


def close_workload(state: WorkloadState) -> WorkloadState:
    """Fence progression while retaining unresolved evidence; no automatic reset."""
    return replace(state, closed=True)
