"""Finite trusted traces: staged QPU computation and atomic owner publication.

No host clock, live ingress inference, profile defaults, or recovery is supplied.
The caller declares exhaustive ingress and exact FIFO windows. This adapter
supports the existing common register Program, not concurrent physical jobs.
It can retain and lazily evolve multiple tenant regions when the frozen runtime
selects dedicated-region placement.
"""

from dataclasses import dataclass
import math
from threading import Lock, RLock

import numpy as np

from core.attack_simulation.circuit_program import CircuitExecutionOperation
from core.attack_simulation.declared_ingress import IngressReservation, _producer
from core.attack_simulation.event_commit_boundary import EventOrderError, EventPosition
from core.attack_simulation.interleaved_execution import interleaved_work
from core.attack_simulation.jobs import JobStatus
from core.attack_simulation.qpu_state import QpuStateStore
from core.attack_simulation.shared_qpu_runtime import SharedQpuRuntime
from core.attack_simulation.world_event_frontier import DeclaredWorldFrontier
from core.experimental_isolation_physics.idle_evolution import IndependentIdleModel
from core.register_operations import freeze_register


class DeclaredExecutionCapacityError(RuntimeError):
    """Host evidence bound, not a QPU error or service quota outcome."""


class DeclaredObservationNotReadyError(RuntimeError):
    """Catch-up has not confirmed the effects required by a timed observation."""


class JobNotAtPositionError(LookupError):
    """The declared job has not arrived at this trusted observation position."""


class QueuedCancellationRejectedError(ValueError):
    """Common queued-only rejection, distinct from invalid supervisor evidence."""


@dataclass(frozen=True)
class JobExecutionWindow:
    arrival: EventPosition
    start: EventPosition
    finish: EventPosition
    release: EventPosition

    def __post_init__(self):
        positions = (self.arrival, self.start, self.finish, self.release)
        if any(type(p) is not EventPosition for p in positions):
            raise TypeError("explicit declared positions are required")
        if len({p.clock_domain for p in positions}) != 1:
            raise ValueError("window clock domains differ")
        if (self.start.key < self.arrival.key or self.finish.key <= self.start.key
                or self.release.key < self.finish.key):
            raise EventOrderError("arrival/start/finish/release must be causally ordered")


@dataclass(frozen=True)
class PreparedExecution:
    job_id: str
    window: JobExecutionWindow
    state_batch: object
    job_success: object
    next_event_sequence: int


@dataclass(frozen=True)
class DeclaredCancellationTrace:
    """Supervisor evidence, not a public timestamp or participant capability."""
    job_id: str
    principal_id: str
    request_id: str
    window: JobExecutionWindow
    arrival: EventPosition
    application: EventPosition
    release: EventPosition
    ingress_reservation: IngressReservation | None = None


class DeclaredQpuExecutor:
    """Single-region FIFO adapter with one shared authority transaction gate.

    Begin publishes RUNNING + start frontier before expensive calculation.
    Cancellation therefore follows service state, not the host worker's speed.
    Finish publishes all validated physical events + success + finish frontier
    together. Calculation failure leaves RUNNING for explicit infrastructure
    recovery; it does not manufacture a physical INTERNAL_ERROR outcome.
    """

    def __init__(self, *, runtime: SharedQpuRuntime, clock_domain: str,
                 max_jobs: int, max_events_per_job: int, max_unresolved: int,
                 idle_model: IndependentIdleModel | None):
        if type(runtime) is not SharedQpuRuntime:
            raise TypeError("the common runtime is required")
        for limit in (max_jobs, max_events_per_job, max_unresolved):
            if type(limit) is not int or limit < 1:
                raise ValueError("explicit positive evidence limits are required")
        if idle_model is not None and type(idle_model) is not IndependentIdleModel:
            raise TypeError("an explicit supported idle model or no idle capability is required")
        snapshot = runtime.state_store.read()
        if (runtime._declared_executor is not None or snapshot.revision != 0 or snapshot.device_time_us != 0
                or runtime.scheduler._jobs or snapshot.last_event_id is not None
                or snapshot.last_job_id is not None or runtime.state_store._transaction_lock_bound
                or runtime.scheduler._transaction_lock_bound or runtime.state_store._prepared_batches):
            raise ValueError("declared execution requires fresh runtime owners")
        if idle_model is not None and len(idle_model.qubits) != len(snapshot.physical_qubit_ids):
            raise ValueError("idle model must cover the retained register in axis order")
        frontier = DeclaredWorldFrontier(clock_domain=clock_domain, max_unresolved=max_unresolved)
        gate = RLock()
        # Assembly is trusted and unpublished: no concurrent use while binding.
        runtime.state_store.bind_transaction_lock(gate)
        runtime.scheduler.bind_transaction_lock(gate)
        frontier.bind_transaction_lock(gate)
        self.runtime, self.frontier = runtime, frontier
        self._gate, self._compute_gate = gate, Lock()
        self._domain = clock_domain
        self._max_jobs, self._max_events = max_jobs, max_events_per_job
        self._idle_model = idle_model
        self._windows = {}
        self._releases = {}
        self._active = None
        self._prepared = None
        self._finished = set()
        self._physical_end = 0.0
        self._closed = False
        self._control_owner = None
        self._receipt_namespace: str | None = None
        self._cancellations = {}
        self._control_requests = {}
        runtime._declared_executor = self
        runtime._declared_event_limit = max_events_per_job

    def claim_control_coordinator(self, owner: object, *, ingress_producer_id: str | None = None) -> None:
        """Select timed control before admission; closed-source behavior stays opt-in."""
        if ingress_producer_id is not None:
            _producer(ingress_producer_id)
        with self._gate:
            self._open()
            if (owner is None or self._control_owner is not None or self._windows
                    or self._releases or self.runtime.scheduler._jobs
                    or self.frontier.snapshot().target is not None):
                raise ValueError("control coordination requires unpublished fresh owners")
            self._control_owner = owner
            self._receipt_namespace = ingress_producer_id

    def bind_receipt_namespace(self, producer_id: str, *, owner: object) -> None:
        """Bind the driver's exclusive receipt source once before admission."""
        _producer(producer_id)
        with self._gate:
            self._open()
            if owner is None or self._control_owner is not owner:
                raise PermissionError("receipt namespace requires its exact control coordinator")
            if (self._receipt_namespace is not None or self._windows or self._releases
                    or self.runtime.scheduler._jobs or self.frontier.snapshot().target is not None):
                raise ValueError("bind receipt namespace once before world admission")
            self._receipt_namespace = producer_id

    def apply_cancellation(self, *, owner: object, principal_id: str, request_id: str,
                           job_id: str, arrival: EventPosition, application: EventPosition,
                           release: EventPosition, book, replacements: tuple = (),
                           ingress_reservation: IngressReservation | None = None) -> DeclaredCancellationTrace:
        """Atomic queued cancellation, timed trace and successor-bound replacement.

        Authorization belongs to the common service. The caller must coordinate
        exhaustive source/event order, and supply the shared book. This does not
        infer ingress completeness or advance the physical frontier from control
        time. No computation or arbitrary participant callback runs in this gate.
        An exact granted or admitted reservation in the driver's exclusive
        bound receipt namespace proves an already ordered arrival. It exempts only
        historical arrival checks, not confirmed physics or successor checks.
        The driver must order pending applications before START/physical seals;
        an admission cutoff alone does not confirm their physical effects.
        """
        from core.attack_simulation.declared_ingress import DeclaredIngressBook
        if (type(principal_id) is not str or not principal_id.strip()
                or type(request_id) is not str or not 1 <= len(request_id) <= 128
                or not request_id.isascii() or any(c.isspace() for c in request_id)):
            raise ValueError("a bounded control identity is required")
        if (type(book) is not DeclaredIngressBook or not book._transaction_lock_bound
                or book._lock is not self._gate or book._clock_domain != self._domain):
            raise ValueError("control and ingress must share the physical transaction gate")
        for position in (arrival, application, release):
            if type(position) is not EventPosition or position.clock_domain != self._domain:
                raise ValueError("control positions require this world domain")
        if not arrival.key < application.key < release.key:
            raise EventOrderError("control arrival/application/release must advance causally")
        with self._gate:
            self._open()
            book._open()
            if owner is None or self._control_owner is not owner:
                raise PermissionError("timed control requires its exact coordinator")
            if ingress_reservation is not None:
                book._exact(ingress_reservation)
                if (self._receipt_namespace is None
                        or ingress_reservation.producer_id != self._receipt_namespace
                        or ingress_reservation.producer_id in book._owners
                        or ingress_reservation.position != arrival):
                    raise ValueError("control proof requires the driver's exact live arrival")
                decision = book.decision(ingress_reservation)
                if decision is not None:
                    if decision != "admitted":
                        raise ValueError("control proof contradicts the retained admission decision")
                elif book._granted is not ingress_reservation:
                    raise EventOrderError("control proof requires an ordered live admission grant")
            key = principal_id, request_id
            replay = self._control_requests.get(key)
            if replay is not None:
                if (replay.job_id, replay.arrival, replay.application, replay.release) != (
                        job_id, arrival, application, release):
                    raise ValueError("control request identity cannot change on replay")
                if replay.ingress_reservation is not ingress_reservation:
                    raise ValueError("control ingress proof cannot change on replay")
                return replay
            window = self._windows.get(job_id)
            if window is None and job_id in self._cancellations:
                window = self._cancellations[job_id].window
            if window is None:
                raise DeclaredObservationNotReadyError("cancellation needs registered admission evidence")
            if arrival.key < window.arrival.key:
                raise JobNotAtPositionError("job has not arrived at this control request")
            frontier = self.frontier.snapshot()
            ingress = book.snapshot()
            if ingress_reservation is None:
                for boundary in (ingress.confirmed_cutoff, frontier.sealed_through):
                    if boundary is not None and arrival.key <= boundary.key:
                        raise EventOrderError("control arrival crosses an established ingress boundary")
                for reservation in (ingress.granted, ingress.last_retired):
                    if reservation is not None and arrival.key <= reservation.position.key:
                        raise EventOrderError("control cannot backinsert before ordered admission")
            if frontier.target is None or application.key > frontier.target.key:
                raise DeclaredObservationNotReadyError("control application is not due")
            if application.key >= window.start.key:
                raise QueuedCancellationRejectedError("queued_only cancellation must precede declared START")
            if frontier.confirmed is not None and application.key <= frontier.confirmed.key:
                raise EventOrderError("control cannot backinsert behind confirmed physics")
            job = self.runtime.scheduler.get(job_id)
            if job.status is not JobStatus.QUEUED:
                raise QueuedCancellationRejectedError("queued_only cancellation requires a queued job")
            if type(replacements) is not tuple or len(replacements) > book._max_pending:
                raise ValueError("control dependency replacements must be a bounded immutable tuple")
            dependencies = {key for key, binding in book._result_inputs.items() if binding[0] == job_id}
            replaced = set()
            for replacement in replacements:
                book._replacement_token(replacement)
                if replacement.expected.key in replaced:
                    raise ValueError("duplicate control dependency replacement")
                replaced.add(replacement.expected.key)
                if book._barriers.get(replacement.expected.key) is not replacement.expected:
                    raise ValueError("control dependency changed during preparation")
                book._validate_replacement_position(replacement.replacement)
                expected_next = book.cancellation_successor_bound(replacement.expected, job_id, release)
                if replacement.replacement.earliest_next_arrival != expected_next:
                    raise ValueError("control successor differs from its declared workload durations")
                if (frontier.sealed_through is not None
                        and replacement.replacement.earliest_next_arrival.key <= frontier.sealed_through.key):
                    raise EventOrderError("control successor crosses an existing physical seal")
            if dependencies != replaced:
                raise ValueError("replace every bound result dependency with the control outcome")
            trace = DeclaredCancellationTrace(job_id, principal_id, request_id, window,
                                              arrival, application, release, ingress_reservation)
            previous_release = self._releases[job_id]
            cancelled = False
            try:
                self.runtime.scheduler._cancel_declared(job_id)
                cancelled = True
                for replacement in replacements:
                    book.commit_barrier_replacement(replacement)
                # Remove only the unexecuted window, never state/events/START.
                del self._windows[job_id]
                self._releases[job_id] = release
                self._cancellations[job_id] = trace
                self._control_requests[key] = trace
            except BaseException:
                for replacement in reversed(replacements):
                    book._rollback_barrier_replacement(replacement)
                self._windows[job_id] = window
                self._releases[job_id] = previous_release
                self._cancellations.pop(job_id, None)
                self._control_requests.pop(key, None)
                if cancelled:
                    self.runtime.scheduler._rollback_declared_cancel(job_id)
                raise
            self.runtime.scheduler._rollback_cancel = None  # Published suffix cannot be compensated later.
            return trace

    def _open(self):
        if self._closed:
            raise RuntimeError("declared physical executor is closed")

    def register_job(self, job_id: str, window: JobExecutionWindow) -> None:
        """Trusted join to an admitted common job; never participant backdating."""
        if type(window) is not JobExecutionWindow or window.start.clock_domain != self._domain:
            raise ValueError("a window in this world is required")
        with self._gate:
            self._open()
            job = self.runtime.scheduler.get(job_id)
            if job_id in self._windows:
                if self._windows[job_id] != window:
                    raise ValueError("declared job window cannot change on replay")
                return
            if job.status is not JobStatus.QUEUED or type(job.command.operation) is not CircuitExecutionOperation:
                raise ValueError("declare only a queued common Program")
            confirmed = self.frontier.snapshot().confirmed
            if confirmed is not None and window.start.key <= confirmed.key:
                raise EventOrderError("job would backinsert behind confirmed physics")
            if len(self._releases) >= self._max_jobs:
                raise DeclaredExecutionCapacityError("declared job history is full")
            self._windows[job_id] = window
            self._releases[job_id] = window.release

    def replan_queued_job(self, job_id: str, window: JobExecutionWindow, *, owner: object) -> None:
        """Trusted FIFO replanning after control changes preceding queued work.

        Initial registration and public replay immutability remain unchanged.
        The driver computes common timing; this only replaces a known queued
        window and its release together. Arrival and confirmed physics cannot
        move; an ingress completeness seal is not a physical START commitment.
        """
        if type(window) is not JobExecutionWindow or window.start.clock_domain != self._domain:
            raise ValueError("a window in this world is required")
        with self._gate:
            self._open()
            if owner is None or self._control_owner is not owner:
                raise PermissionError("queued replanning requires its exact control coordinator")
            previous = self._windows.get(job_id)
            if previous is None:
                raise ValueError("queued replanning requires an existing execution window")
            if window.arrival != previous.arrival:
                raise ValueError("queued replanning cannot change the registered arrival")
            job = self.runtime.scheduler.get(job_id)
            if (job.status is not JobStatus.QUEUED or job_id in self._finished
                    or job_id in self._cancellations
                    or (self._active is not None and self._active[0].job_id == job_id)):
                raise ValueError("replan only a job that has not started or terminated")
            confirmed = self.frontier.snapshot().confirmed
            if confirmed is not None and window.start.key <= confirmed.key:
                raise EventOrderError("queued replanning cannot backinsert behind confirmed physics")
            self._windows[job_id] = window
            self._releases[job_id] = window.release

    def register_cancelled_release(self, job_id: str, release: EventPosition) -> None:
        """Trusted cancellation reply time, without inventing physical execution.

        A job cancelled before physical-window registration still has a terminal
        common result. Its declared control-response release needs no QPU window.
        """
        if type(release) is not EventPosition or release.clock_domain != self._domain:
            raise ValueError("a cancellation release in this world is required")
        with self._gate:
            self._open()
            if self._control_owner is not None:
                raise PermissionError("timed control requires its application trace, not a release-only override")
            if self.runtime.scheduler.get(job_id).status is not JobStatus.CANCELLED:
                raise ValueError("cancellation release requires a cancelled common job")
            if job_id in self._releases:
                if self._releases[job_id] != release:
                    raise ValueError("declared release cannot change on replay")
                return
            if len(self._releases) >= self._max_jobs:
                raise DeclaredExecutionCapacityError("declared job history is full")
            self._releases[job_id] = release

    def _duration(self, job, snapshot):
        op, config = job.command.operation, self.runtime.config
        dedicated = config.placement_policy.value == "tenant_dedicated_region"
        if op.interleave is not None:
            if config.placement_policy.value != "guaranteed_same_physical_qubit":
                raise ValueError("interleaved runs require the shared same-region placement")
            return interleaved_work(
                op, job.command.tenant_id, snapshot.owner_tenant_id,
                config.reset_duration_us, config.program_measurement_duration_us,
            ).duration_us
        target = (self.runtime.tenant_regions[job.command.tenant_id] if dedicated else None)
        reset_duration, measurement_duration = (
            self.runtime.regional_service_durations[target]
            if dedicated else
            (config.reset_duration_us, config.program_measurement_duration_us)
        )
        duration = sum(i.duration_us if i.kind == "unitary" else reset_duration
                       if i.kind in {"initialize", "reset"} else 0.0 for i in op.instructions)
        duration += measurement_duration * len({
            i.time_group for i in op.instructions if i.kind == "measure"})
        duration *= op.shots
        duration += config.repetition_delay_us * (op.shots - 1)
        if snapshot.owner_tenant_id is not None and snapshot.owner_tenant_id != job.command.tenant_id:
            if not dedicated:
                duration += config.reset_duration_us
        if config.placement_policy.value == "isolated_physical_resource":
            raise ValueError("declared adapter does not support resource reassignment")
        if dedicated and snapshot.physical_qubit_ids != target:
            duration += config.isolated_assignment_duration_us
        return duration

    def _active_idle_model(self):
        if self.runtime.tenant_regions:
            return self.runtime.regional_idle_models[self.runtime.state_store.read().physical_qubit_ids]
        return self._idle_model

    def _detached_runtime(self, scratch):
        detached = SharedQpuRuntime(
            state_store=scratch, reset_models=self.runtime.reset_models,
            config=self.runtime.config,
            tenant_regions=self.runtime.tenant_regions if self.runtime.tenant_regions else None,
            regional_reset_models=self.runtime.regional_reset_models if self.runtime.tenant_regions else None,
            regional_idle_models=self.runtime.regional_idle_models if self.runtime.tenant_regions else None,
            regional_service_durations=self.runtime.regional_service_durations if self.runtime.tenant_regions else None,
            idle_model=self.runtime.idle_model,
        )
        detached._job_authorization = self.runtime.job_authorization
        return detached

    def _begin(self):
        with self._gate:
            self._open()
            if self._active is not None:
                return self._active
            queued = self.runtime.scheduler.queued_job_ids()
            if not queued:
                return None
            job = self.runtime.scheduler.get(queued[0])
            window = self._windows.get(job.job_id)
            if window is None:
                raise ValueError("FIFO head has no declared physical window")
            snapshot = self.runtime.state_store.read()
            if window.start.time_us < self._physical_end:
                raise EventOrderError("single-region physical windows overlap")
            gap = window.start.time_us - self._physical_end
            if gap and self._active_idle_model() is None:
                raise ValueError("positive declared wait requires an explicit complete idle model")
            self.runtime._authorize(job.command)
            self.runtime._validate_job_target(job.command)
            duration = self._duration(job, snapshot)
            # Absolute float coordinates lose precision when subtracted after
            # seconds/minutes of world time. The duration belongs to compiled
            # physics; the rounded endpoint is metadata, not a replacement dt.
            expected_finish = window.start.time_us + duration
            if (not math.isfinite(expected_finish) or window.finish.time_us != expected_finish
                    or (duration > 0 and expected_finish == window.start.time_us)):
                raise ValueError("declared physical duration differs from compiled service events")
            op = job.command.operation
            event_count = (interleaved_work(
                op, job.command.tenant_id, snapshot.owner_tenant_id,
                self.runtime.config.reset_duration_us, self.runtime.config.program_measurement_duration_us,
            ).event_count + int(gap > 0)) if op.interleave is not None else (
                op.shots + int(gap > 0) + int(
                    snapshot.owner_tenant_id is not None and snapshot.owner_tenant_id != job.command.tenant_id
                    and not self.runtime.tenant_regions))
            if (self.runtime.tenant_regions
                    and snapshot.physical_qubit_ids != self.runtime.tenant_regions[job.command.tenant_id]):
                event_count += 1
            if event_count > self._max_events:
                raise DeclaredExecutionCapacityError("physical event batch exceeds host bound")
            def begin():
                try:
                    running = self.runtime.scheduler._start_declared(job.job_id)
                    if idle_batch is not None:
                        self.runtime.state_store.commit_prepared(idle_batch)
                except BaseException:
                    self.runtime.scheduler._rollback_declared_start(job.job_id)
                    if idle_batch is not None:
                        self.runtime.state_store._rollback_prepared_commit(idle_batch)
                    raise
                self.runtime._next_event_sequence = self.runtime.state_store.read().revision + 1
                self._physical_end = window.start.time_us
                self._active = (running, window, self.runtime.state_store.read(), 0.0)
                return self._active
            self.frontier._validate_confirmation(window.start)
        # Idle physics must already be authoritative when START is confirmed.
        # Compute and validate outside the shared authority gate, then recheck
        # queue/state/closure before publishing idle + RUNNING + start together.
        idle_batch = None
        if gap:
            scratch = self.runtime.state_store.clone_detached()
            detached = self._detached_runtime(scratch)
            state = self._active_idle_model().evolve(np.asarray(snapshot.density_matrix), elapsed_us=gap)
            detached._commit_event(job=job, event_kind="declared_idle", next_state=freeze_register(state),
                duration_us=gap, next_owner_tenant_id=snapshot.owner_tenant_id)
            idle_batch = self.runtime.state_store.prepare_batch(snapshot, scratch.read(), scratch.events(),
                                                              max_events=self._max_events,
                                                              final_inactive_region=scratch.inactive_region())
        with self._gate:
            self._open()
            if (self._active is not None or self.runtime.state_store.read() is not snapshot
                    or self.runtime.scheduler.queued_job_ids()[:1] != (job.job_id,)
                    or self._windows.get(job.job_id) is not window
                    or self.runtime.scheduler.get(job.job_id).status is not JobStatus.QUEUED):
                raise RuntimeError("physical start changed during idle preparation")
            if idle_batch is not None:
                self.runtime.state_store.validate_prepared(idle_batch)
            return self.frontier.commit_with(window.start, begin)

    def prepare_next(self) -> PreparedExecution | None:
        """Numerical work is detached; failure preserves authoritative start."""
        if not self._compute_gate.acquire(blocking=False):
            raise RuntimeError("a numerical preparation is already active")
        try:
            active = self._begin()
            if active is None:
                return None
            with self._gate:
                if self._prepared is not None:
                    return self._prepared
                job, window, snapshot, gap = active
                self.frontier._validate_confirmation(window.finish)
            scratch = self.runtime.state_store.clone_detached()
            detached = self._detached_runtime(scratch)
            result = detached._execute(job)
            expected_time = snapshot.device_time_us + self._duration(job, snapshot)
            if not math.isclose(scratch.read().device_time_us, expected_time, rel_tol=1e-12, abs_tol=1e-10):
                raise ValueError("computed event duration differs from the declared physical finish")
            batch = self.runtime.state_store.prepare_batch(snapshot, scratch.read(), scratch.events(),
                                                          max_events=self._max_events,
                                                          final_inactive_region=scratch.inactive_region())
            success = self.runtime.scheduler.prepare_success(job.job_id, result)
            prepared = PreparedExecution(job.job_id, window, batch, success, detached._next_event_sequence)
            with self._gate:
                self._open()
                if self._active is not active:
                    raise RuntimeError("physical start binding changed during computation")
                self._prepared = prepared
            return prepared
        finally:
            self._compute_gate.release()

    def begin_next(self):
        """Confirm START separately, without requiring FINISH to be due.

        The service state must become RUNNING at its declared start even while
        nominal time has not reached finish. Host speed cannot extend the queued
        cancellation interval. The same compute gate serializes idle preparation.
        """
        if not self._compute_gate.acquire(blocking=False):
            raise RuntimeError("a numerical preparation is already active")
        try:
            active = self._begin()
            return None if active is None else active[0]
        finally:
            self._compute_gate.release()

    def commit(self, prepared: PreparedExecution):
        """Short shared-lock commit; no numeric kernel or host timestamp."""
        with self._gate:
            self._open()
            if prepared is not self._prepared or prepared is None:
                raise ValueError("prepared execution is foreign, copied or retired")
            self.runtime.state_store.validate_prepared(prepared.state_batch)
            self.runtime.scheduler.validate_prepared(prepared.job_success)
            def publish():
                try:
                    self.runtime.state_store.commit_prepared(prepared.state_batch)
                    result = self.runtime.scheduler.commit_prepared_success(prepared.job_success)
                except BaseException:
                    # No reader can observe these candidate writes while the
                    # shared gate is held. Undo only this unpublished suffix,
                    # never an earlier confirmed START or externally seen event.
                    self.runtime.scheduler._rollback_prepared_success(prepared.job_success)
                    self.runtime.state_store._rollback_prepared_commit(prepared.state_batch)
                    raise
                self.runtime._next_event_sequence = prepared.next_event_sequence
                self._physical_end = prepared.window.finish.time_us
                self._finished.add(prepared.job_id)
                self._active = self._prepared = None
                return result
            return self.frontier.commit_with(prepared.window.finish, publish)

    def result_is_available(self, job_id: str) -> bool:
        with self._gate:
            if self._closed:
                return False
            release, target = self._releases.get(job_id), self.frontier.snapshot().target
            status = self.runtime.scheduler.get(job_id).status
            if (self._control_owner is not None and status is JobStatus.CANCELLED
                    and job_id not in self._cancellations):
                return False
            ready = job_id in self._finished or status is JobStatus.CANCELLED
            return bool(ready and release is not None and target is not None and target.key >= release.key)

    def observation_at(self, job_id: str, position: EventPosition) -> tuple[JobStatus, bool]:
        """Trusted receipt-time projection, independent of host callback delay.

        Return status and result availability at an explicit world position,
        not at the latest nominal target. This does not sample a host clock,
        advance physics, grant access, or provide a participant backdate API.
        The service must authorize before calling and project its public view.
        Registered windows describe only the current submit-only trace. A
        cancellation/failure without its application-time evidence is rejected,
        rather than inventing its historical status or reply time.
        """
        if type(position) is not EventPosition or position.clock_domain != self._domain:
            raise ValueError("observation requires a position in this world")
        with self._gate:
            self._open()
            frontier = self.frontier.snapshot()
            if frontier.target is None or position.key > frontier.target.key:
                raise ValueError("observation exceeds the observed nominal target")
            job = self.runtime.scheduler.get(job_id)
            cancellation = self._cancellations.get(job_id)
            window = cancellation.window if cancellation is not None else self._windows.get(job_id)
            if window is None:
                raise DeclaredObservationNotReadyError("job has no registered timed observation trace")
            if position.key < window.arrival.key:
                raise JobNotAtPositionError("job has not arrived at this position")
            if cancellation is not None:
                if job.status is not JobStatus.CANCELLED:
                    raise DeclaredObservationNotReadyError("cancelled job changed outside its timed trace")
                if position.key < cancellation.application.key:
                    return JobStatus.QUEUED, False
                return JobStatus.CANCELLED, position.key >= cancellation.release.key
            if job.status not in {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.SUCCEEDED}:
                raise DeclaredObservationNotReadyError("control transition has no application-time evidence")
            if job.status is JobStatus.SUCCEEDED and job_id not in self._finished:
                raise DeclaredObservationNotReadyError("success was not confirmed by this executor")
            if position.key < window.start.key:
                return JobStatus.QUEUED, False
            confirmed = frontier.confirmed
            started = job_id in self._finished or (
                self._active is not None and self._active[0].job_id == job_id)
            if not started or confirmed is None or confirmed.key < window.start.key:
                raise DeclaredObservationNotReadyError("START is not confirmed for this observation")
            if position.key < window.finish.key:
                return JobStatus.RUNNING, False
            if (confirmed.key < window.finish.key or job_id not in self._finished
                    or job.status is not JobStatus.SUCCEEDED):
                raise DeclaredObservationNotReadyError("FINISH is not confirmed for this observation")
            return JobStatus.SUCCEEDED, position.key >= window.release.key

    def forget_job(self, job_id: str, *, owner: object) -> None:
        """Trusted retention: drop a released terminal job from every per-job map.

        The coordinator proves no pending request or workload dependency still
        names the job. The latest physical job stays as the next job's premise.
        """
        with self._gate:
            self._open()
            if owner is None or self._control_owner is not owner:
                raise PermissionError("retention requires its exact control coordinator")
            if not self.result_is_available(job_id):
                raise ValueError("only a released terminal job can be discarded")
            if ((self._active is not None and self._active[0].job_id == job_id)
                    or (self._prepared is not None and self._prepared.job_id == job_id)
                    or self.runtime.state_store.read().last_job_id == job_id):
                raise ValueError("the latest physical job is retained")
            self.runtime.scheduler._forget_terminal(job_id)
            self._windows.pop(job_id, None)
            self._releases.pop(job_id, None)
            self._finished.discard(job_id)
            if self._cancellations.pop(job_id, None) is not None:
                for key in [k for k, t in self._control_requests.items() if t.job_id == job_id]:
                    del self._control_requests[key]
            # Diagnostic records last: a refusal here leaves job maps consistent.
            self.runtime.state_store._forget_job_events(job_id)

    def release_position(self, job_id: str) -> EventPosition:
        """Trusted evidence, not a public timestamp field."""
        with self._gate:
            self._open()
            if not self.result_is_available(job_id):
                raise RuntimeError("declared result release is not reached")
            return self._releases[job_id]

    def close(self):
        """Fence commits/publication, retaining unresolved execution evidence."""
        with self._gate:
            self._closed = True
            self.frontier.close()
