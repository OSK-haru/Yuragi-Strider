"""Bounded common live event driver; no HTTP mount or participant timestamps.

One shared ingress gate orders capture, frozen actor sources and physical
completeness. Compilation/numerics are outside that short gate. Pump backlog
does not become device time, victim suspension, reset or simulated failure.
"""

from collections.abc import Mapping
from dataclasses import dataclass
import math
from threading import Lock
from time import monotonic_ns

from api.attack_controls import DeclaredControlService
from api.attack_sessions import SessionBusyError, SessionCapacityError
from api.attack_workloads import DeclaredWorkloadRunner, WorkloadInfrastructureError
from api.attack_world import WorldPumpReport
from core.attack_simulation.declared_execution import DeclaredQpuExecutor, QueuedCancellationRejectedError
from core.attack_simulation.declared_service_timing import DeclaredServiceTiming, _after
from core.attack_simulation.event_commit_boundary import EventPosition, UnresolvedIngressError
from core.attack_simulation.job_service import (
    JobExpiredError, JobNotFoundError, JobResultNotReadyError, JobStatusView,
)
from core.attack_simulation.jobs import TERMINAL_JOB_STATUSES, JobStatus
from core.attack_simulation.live_ingress import LiveIngress
from core.attack_simulation.live_time_policy import (
    EXPLICIT_WAIT, LiveTimePolicy, parse_live_time_policy,
)
from core.attack_simulation.program_request import (
    STATE_INHERIT_OPERATION_ID, CircuitProgram, ProgramRequestError, ProgramSubmitRequest,
    interleave_operation_id,
)
from core.attack_simulation.request_receipts import RequestReceipt
from core.attack_simulation.victim_workload import Phase


@dataclass
class _LiveOperation:
    receipt: object
    reservation: object
    credential: str  # Virtual, process-local credential; never real Web auth.
    payload: object
    window: object = None
    arrived: bool = False
    applied: bool = False
    value: object = None
    error_code: str | None = None
    release: EventPosition | None = None


MAX_LIVE_CIRCUIT_JSON_BYTES = 256 * 1024


@dataclass(frozen=True)
class LiveProgramEnvelope:
    """Complete authenticated submit envelope, captured before Program parsing.

    Host parsing time must not precede the receipt sample, so only bounded
    shapes are checked here. The circuit document becomes a common Program at
    its declared arrival; invalid content is then a declared INVALID_PROGRAM.
    """
    circuit_json: str
    device_id: str
    shots: int
    idempotency_key: str
    initialization: str = "prepare"
    interleave: str | None = None

    def __post_init__(self):
        if (type(self.circuit_json) is not str
                or len(self.circuit_json.encode("utf-8")) > MAX_LIVE_CIRCUIT_JSON_BYTES):
            raise ValueError("circuit document exceeds the live envelope bound")
        for name in ("device_id", "idempotency_key", "initialization"):
            value = getattr(self, name)
            if type(value) is not str or not 1 <= len(value) <= 256:
                raise ValueError(f"{name} must be a bounded string")
        if type(self.shots) is not int:
            raise TypeError("shots must be an integer")
        if self.interleave is not None and (type(self.interleave) is not str
                                            or not 1 <= len(self.interleave) <= 256):
            raise ValueError("interleave must be a bounded string")

    def to_request(self) -> ProgramSubmitRequest:
        try:
            program = CircuitProgram.from_editor_json(self.circuit_json)
            return ProgramSubmitRequest(program, self.device_id, self.shots,
                                        self.idempotency_key, self.initialization, self.interleave)
        except ProgramRequestError:
            raise
        except (ValueError, TypeError, OverflowError, RecursionError):
            raise ProgramRequestError("invalid live program envelope") from None


def _public_reason(error: Exception) -> str:
    """Printable, bounded text of the unmet service condition (request-derived only)."""
    text = "".join(ch for ch in str(error) if ch.isprintable())[:200]
    return text or "invalid program"


@dataclass(frozen=True)
class LiveReply:
    """Common projected value/error only; no clock, receipt, actor or QPU trace."""
    value: object
    error_code: str | None


@dataclass(frozen=True)
class _CachedReadReply:
    receipt: object
    reply: LiveReply


class LiveReplyExpiredError(ValueError):
    """A completed reply fell outside the bounded exact-replay window."""


class LiveWorkloadWorld:
    """Common finite actor workloads plus bounded live common operations.

    Trusted adapters capture a complete authenticated envelope; receipt objects
    remain internal handles. Caller pumps explicitly, then retrieves only an
    authorized released reply. No background retry, arbitrary code, sockets or
    extra service/physics assumptions are introduced.
    """

    def __init__(self, *, runner: DeclaredWorkloadRunner, executor: DeclaredQpuExecutor,
                 producer_id: str, retained_jobs: int | None = None):
        if type(runner) is not DeclaredWorkloadRunner or type(executor) is not DeclaredQpuExecutor:
            raise TypeError("common runner and executor are required")
        if retained_jobs is not None and (type(retained_jobs) is not int or retained_jobs < 1):
            raise ValueError("retained_jobs must be a positive host limit")
        raw = runner._session.manifest.qpu_service_config.get("declared_service_timing")
        fields = {"processing_delay_us", "publication_delay_us", "denial_delay_us", "basis"}
        if (not isinstance(raw, Mapping) or set(raw) != fields
                or type(raw["basis"]) is not str or not 1 <= len(raw["basis"].strip()) <= 2048):
            raise ValueError("freeze explicit common service durations and basis")
        domain = runner._clock.settings.clock_domain
        timing = DeclaredServiceTiming(domain, **{k: raw[k] for k in fields - {"basis"}})
        time_policy = parse_live_time_policy(
            runner._session.manifest.experiment_config.get("live_time_policy"))
        clock_source = runner._clock._host_ns
        if time_policy.mode == EXPLICIT_WAIT and (
                not callable(getattr(clock_source, "advance_to", None))
                or type(getattr(clock_source, "value", None)) is not int):
            raise ValueError("explicit_wait requires a manually advanceable world clock")
        # Validate the live namespace BEFORE either coordinator is claimed.
        from core.attack_simulation.declared_ingress import _producer
        _producer(producer_id)
        book = runner._book
        if (producer_id in book._owners or any(t.producer_id == producer_id
                for t in book.snapshot().reservations)):
            raise ValueError("live namespace collides with a frozen workload")
        controls = DeclaredControlService(runner=runner, executor=executor, ingress_producer_id=producer_id)
        ingress = LiveIngress(controls.config.ingress, clock=runner._clock,
            book=book, gate=executor._gate, producer_id=producer_id)
        owner = object()
        runner.claim_world_coordinator(owner)
        self._runner, self._executor, self._controls = runner, executor, controls
        self._ingress, self._timing, self._time_policy, self._owner = ingress, timing, time_policy, owner
        self._pump_gate = Lock()
        self._operations = {}
        self._reply_cache = {}  # Bounded exact replay cache for completed operations.
        self._arrivals = {}  # Pruned with the job records; see _retire_job_history.
        # None keeps every job record for the world's lifetime.
        self._retained_jobs = retained_jobs
        self._closed = False

    def _open(self):
        self._runner._open()
        if self._closed or self._runner._session._closed:
            raise WorkloadInfrastructureError("live world has been discarded")

    def start(self):
        # Lifecycle control -> physical gate, never the inverse lock order.
        with self._runner._session._lock, self._executor._gate:
            self._open()
            return self._runner.start(_coordinator_owner=self._owner)

    def target(self):
        self._open()
        return self._ingress.sample()

    @property
    def live_time_policy(self) -> LiveTimePolicy:
        return self._time_policy

    def uses_clock_source(self, source) -> bool:
        return self._runner._clock._host_ns is source

    def host_clock_value_ns(self) -> int:
        source = self._runner._clock._host_ns
        value = getattr(source, "value", None)
        if type(value) is not int:
            raise RuntimeError("the world clock does not expose a controllable host position")
        return value

    def host_ns_for_world_time(self, world_time_us: float, *, after: bool) -> int:
        """Map a world stop position through the frozen host/world clock rate."""
        if type(world_time_us) not in {int, float}:
            raise ValueError("world time must be finite")
        if not math.isfinite(world_time_us) or world_time_us < 0 or type(after) is not bool:
            raise ValueError("world time mapping arguments are invalid")
        clock = self._runner._clock
        if clock._origin_ns is None:
            raise RuntimeError("the world clock has not started")
        origin, rate = clock._origin_ns, clock.settings.rate
        mapped = lambda offset: offset / 1000 * rate
        offset = max(0, math.floor(world_time_us * 1000 / rate))
        for _ in range(64):
            if offset > 0 and (mapped(offset - 1) > world_time_us if after
                               else mapped(offset - 1) >= world_time_us):
                offset -= 1
            elif mapped(offset) <= world_time_us if after else mapped(offset) < world_time_us:
                offset += 1
            else:
                return origin + offset
        raise ValueError("world position cannot be mapped to the host resolution")

    def advance_host_clock_to(self, host_time_ns: int) -> None:
        if self._time_policy.mode != EXPLICIT_WAIT:
            raise RuntimeError("host_clock worlds cannot be advanced by explicit wait")
        source = self._runner._clock._host_ns
        advance = getattr(source, "advance_to", None)
        if not callable(advance):
            raise RuntimeError("the explicit-wait clock is not controllable")
        advance(host_time_ns)

    def authorize_wait(self, credential, owner_context=None) -> str:
        session = self._runner._session
        session.authenticate(credential, _web_owner_context=owner_context)
        return session._authority.resolve(credential)

    def known_receipt_for(self, principal_id: str, receipt: RequestReceipt) -> bool:
        if type(receipt) is not RequestReceipt or receipt.principal_id != principal_id:
            return False
        with self._executor._gate:
            cached = self._reply_cache.get(receipt.sequence)
            operation = self._operations.get(receipt.sequence)
            return ((cached is not None and cached.receipt is receipt)
                    or (operation is not None and operation.receipt is receipt))

    def max_program_qubits(self, principal_id: str) -> int | None:
        """The principal's qubit limit from its ordinary device information.

        Static service configuration, like a real backend's published qubit
        count; reading it neither advances world time nor queues anything.
        """
        try:
            devices = self._runner._session._service.list_devices(principal_id)
        except PermissionError:
            return None
        return devices[0].max_qubits if devices else None

    def request_world_time_us(self, receipt: RequestReceipt) -> float:
        """Return the captured service-arrival world time for an exact receipt."""
        if type(receipt) is not RequestReceipt:
            raise ValueError("an exact request receipt is required")
        token = self._ingress.reservation(receipt)
        return token.position.time_us

    def set_compute_catch_up_paused(self, paused: bool) -> None:
        """Exclude only local numerical pump time from the host-clock mapping."""
        if type(paused) is not bool or self.live_time_policy.mode != "host_clock":
            raise ValueError("compute catch-up pause is only valid for host_clock")
        source = self._runner._clock._host_ns
        transition = getattr(source, "pause" if paused else "resume", None)
        if not callable(transition):
            raise RuntimeError("the selected host clock cannot pause numerical catch-up")
        transition()

    def pending_receipts_for(self, principal_id: str) -> tuple[RequestReceipt, ...]:
        with self._executor._gate:
            self._open()
            target = self._ingress.sample()
            pending = []
            for op in self._operations.values():
                if op.receipt.principal_id != principal_id:
                    continue
                cached = self._reply_cache.get(op.receipt.sequence)
                released = cached is not None and cached.receipt is op.receipt
                if not released and op.applied and op.release is not None:
                    released = op.release.key <= target.key
                if not released:
                    pending.append(op.receipt)
            return tuple(pending)

    def earliest_pending_event_time_us(self, horizon_us: float) -> float | None:
        """Return the earliest unprocessed event in [now, horizon], read-only."""
        if (type(horizon_us) not in {int, float} or not math.isfinite(horizon_us)
                or horizon_us < 0):
            raise ValueError("event horizon must be finite and nonnegative")
        waiting = self._waiting()  # Runner lock is never acquired under physical gate.
        with self._executor._gate:
            self._open()
            current = self._ingress.sample()
            if horizon_us < current.time_us:
                return None
            expanded = math.nextafter(horizon_us, math.inf)
            if not math.isfinite(expanded):
                expanded = horizon_us
            far = EventPosition(current.clock_domain, expanded, 0)
            actions = self._actions(far, waiting)
            times = [action[0][0] for action in actions if action[0][0] <= horizon_us]
            times.extend(op.release.time_us for op in self._operations.values()
                         if op.applied and op.release is not None
                         and current.time_us <= op.release.time_us <= horizon_us)
            times = [time_us for time_us in times if current.time_us <= time_us <= horizon_us]
            return min(times) if times else None

    def is_reply_released(self, receipt: RequestReceipt) -> bool:
        if type(receipt) is not RequestReceipt:
            return False
        with self._executor._gate:
            self._open()
            cached = self._reply_cache.get(receipt.sequence)
            if cached is not None and cached.receipt is receipt:
                return True
            operation = self._operations.get(receipt.sequence)
            if (operation is None or operation.receipt is not receipt
                    or not operation.applied or operation.release is None):
                return False
            return operation.release.key <= self._ingress.sample().key

    def _capture(self, credential, operation, payload, owner_context):
        session = self._runner._session
        # Scope precedes ingress; never wait for session control before capture.
        with session._scope_lock:
            session._require_web_owner(owner_context)
            session._check_workload_owner(self._runner._owner)
            principal = session._authority.resolve(credential)
            with self._executor._gate:
                self._open()
                limits = self._controls.config.ingress
                pending = sum(not op.applied for op in self._operations.values())
                if pending >= limits.max_pending or len(self._operations) >= limits.max_history:
                    raise SessionCapacityError("live operation evidence capacity reached")
                receipt = self._ingress.capture(principal, operation)
                token = self._ingress.reservation(receipt)
                self._operations[receipt.sequence] = _LiveOperation(receipt, token, credential, payload)
                return receipt

    def submit(self, credential, request, *, owner_context=None):
        if type(request) not in {ProgramSubmitRequest, LiveProgramEnvelope}:
            raise TypeError("a complete immutable common Program request or envelope is required")
        return self._capture(credential, "program.submit", request, owner_context)

    def _job_request(self, credential, operation, job_id, owner_context):
        if (type(job_id) is not str or not 1 <= len(job_id) <= 128 or not job_id.isascii()
                or any(c.isspace() for c in job_id)):
            raise ValueError("a bounded common job identifier is required")
        return self._capture(credential, operation, job_id, owner_context)

    def cancel(self, credential, job_id, *, owner_context=None):
        return self._job_request(credential, "job.cancel", job_id, owner_context)

    def status(self, credential, job_id, *, owner_context=None):
        return self._job_request(credential, "job.status.read", job_id, owner_context)

    def result(self, credential, job_id, *, owner_context=None):
        return self._job_request(credential, "job.result.read", job_id, owner_context)

    def victim_activity(self, credential, job_id, *, owner_context=None):
        """Abstract command; the service refuses it unless the scenario enabled it."""
        return self._job_request(credential, "job.victim_activity.read", job_id, owner_context)

    def reply(self, credential, receipt, *, owner_context=None):
        session = self._runner._session
        with session._scope_lock:
            session._require_web_owner(owner_context)
            session._check_workload_owner(self._runner._owner)
            principal = session._authority.resolve(credential)
            with self._executor._gate:
                self._open()
                if type(receipt) is not RequestReceipt:
                    raise ValueError("an exact request receipt is required")
                if receipt.principal_id != principal:
                    raise PermissionError("request is unavailable")
                cached = self._reply_cache.get(receipt.sequence)
                if cached is not None:
                    if cached.receipt is not receipt:
                        raise PermissionError("request is unavailable")
                    from copy import deepcopy
                    return LiveReply(deepcopy(cached.reply.value), cached.reply.error_code)
                op = self._operations.get(receipt.sequence)
                if op is None:
                    raise LiveReplyExpiredError("reply is outside the retained replay window")
                self._ingress.reservation(receipt)  # Exact object, including retained history.
                target = self._ingress.sample()
                if not op.applied or op.release is None or op.release.key > target.key:
                    raise JobResultNotReadyError("declared reply has not been published")
                # Copies leave cached result evidence immutable to the caller.
                from copy import deepcopy
                reply = LiveReply(deepcopy(op.value), op.error_code)
                self._ingress.release_reply(receipt)
                del self._operations[receipt.sequence]
                self._reply_cache[receipt.sequence] = _CachedReadReply(receipt, reply)
                limit = self._controls.config.ingress.max_history
                while len(self._reply_cache) > limit:
                    del self._reply_cache[next(iter(self._reply_cache))]
                return LiveReply(deepcopy(reply.value), reply.error_code)

    def _waiting(self):
        return [(producer, state, event) for producer, state in self._runner.states().items()
                for event in state.events if event.phase is Phase.WAITING]

    def _sample_target(self):
        with self._executor._gate:
            self._open()
            target = self._ingress.sample()
            prior = self._executor.frontier.snapshot().target
            order = max(target.order, prior.order if prior and prior.time_us == target.time_us else 0)
            through = EventPosition(target.clock_domain, target.time_us, order)
            self._executor.frontier.set_target(through)
            return through

    def _plan_queue(self):
        executor = self._executor
        waiting = self._waiting()  # Runner lock is never acquired under physical gate.
        with executor._gate:
            self._open()
            for _, _, event in waiting:
                if event.job_id:
                    self._arrivals.setdefault(event.job_id, event.arrival)
            previous = executor.frontier.snapshot().confirmed
            tenant = executor.runtime.state_store.read().owner_tenant_id
            if executor._active is not None:
                active, window, _, _ = executor._active
                previous, tenant = window.finish, active.command.tenant_id
            for job_id in executor.runtime.scheduler.queued_job_ids():
                job = executor.runtime.scheduler.get(job_id)
                if job_id not in self._arrivals:
                    raise WorkloadInfrastructureError("unrecorded source reached live queue")
                window = self._timing.plan_job_window(job, executor.runtime.config,
                    previous_finish=previous, previous_tenant_id=tenant, arrival=self._arrivals[job_id])
                if job_id not in executor._windows:
                    executor.register_job(job_id, window)
                elif executor._windows[job_id] != window:
                    executor.replan_queued_job(job_id, window, owner=self._controls._owner)
                previous, tenant = window.finish, job.command.tenant_id

    def _refine_dependencies(self, target):
        """Min(success, known cancellation, earliest FUTURE live cancellation).

        A submit-only success bound is invalid for a cancellable queued job.
        The shared gate's sampled horizon excludes earlier unknown receipts;
        current live cancellations remain in the minimum until resolved.
        """
        book, executor = self._runner._book, self._executor
        waiting = self._waiting()
        with executor._gate:
            self._open()
            for producer, state, event in waiting:
                barrier = book._barriers.get((producer, event.event_id))
                if barrier is None:
                    continue
                if event.job_id is None:
                    release = self._timing.denial_release(event.arrival)
                    step = next(s for s in state.plan.steps if s.step_id == event.step_id)
                    successors = [s for s in state.plan.steps if any(b.next_step == s.step_id for b in step.branches)]
                    times = [release.time_us + step.durations.inbound_us
                             + s.durations.classical_us + s.durations.outbound_us for s in successors]
                    time = min(times)
                    earliest = EventPosition(release.clock_domain, time,
                        release.order + 1 if time == release.time_us else 0)
                else:
                    job = executor.runtime.scheduler.get(event.job_id)
                    if job.status is JobStatus.CANCELLED:
                        release = executor._cancellations[event.job_id].release
                    else:
                        window = executor._windows[event.job_id]
                        release = window.release
                        if job.status is JobStatus.QUEUED:
                            candidates = [release, self._controls.config.control_timing.plan_cancel(target).release]
                            candidates.extend(self._controls.config.control_timing.plan_cancel(op.reservation.position).release
                                for op in self._operations.values() if op.receipt.operation_id == "job.cancel"
                                and op.payload == event.job_id and not op.applied
                                and op.reservation.position.key >= window.arrival.key)
                            release = min(candidates, key=lambda p: p.key)
                    earliest = book.cancellation_successor_bound(barrier, event.job_id, release)
                if earliest.key > barrier.earliest_next_arrival.key:
                    book.refine_barrier(producer, event.event_id, earliest)

    def _actions(self, target, waiting):
        book, executor = self._runner._book, self._executor
        snapshot = book.snapshot(target)
        actions = []
        token = snapshot.granted or snapshot.eligible
        if token is None and snapshot.reservations:
            candidate = snapshot.reservations[0]
            if not any(book._blocks(b, candidate) for b in snapshot.barriers):
                token = candidate  # Actual next_due reports capacity, not fake idle.
        if token is not None and token.position.time_us < target.time_us:
            actions.append((token.position.key, 1, token.producer_id, token.event_id, "arrival", token))
        for seq, op in self._operations.items():
            if op.arrived and not op.applied and op.window.application.time_us < target.time_us:
                actions.append((op.window.application.key, 1, "", seq, "control", op))
        for producer, _, event in waiting:
            if event.job_id is None:
                release = self._timing.denial_release(event.arrival)
            elif executor.runtime.scheduler.get(event.job_id).status in {JobStatus.SUCCEEDED, JobStatus.CANCELLED}:
                release = executor._releases[event.job_id]
            else:
                continue
            if release.time_us < target.time_us:
                actions.append((release.key, 0, producer, event.event_id, "workload_release", release))
        head = executor._active
        if head is None:
            queued = executor.runtime.scheduler.queued_job_ids()
            head = (executor.runtime.scheduler.get(queued[0]), executor._windows[queued[0]]) if queued else None
        if head is not None:
            job, window = head[:2]
            kind = "finish" if job.status is JobStatus.RUNNING else "start"
            position = window.finish if kind == "finish" else window.start
            # Every pending application also bars physics, after its arrival retires.
            controls = [op.window.application for op in self._operations.values()
                        if op.arrived and not op.applied]
            if (position.time_us < target.time_us
                    and not any(t.position.key <= position.key for t in snapshot.reservations)
                    and not any(b.earliest_next_arrival.key <= position.key for b in snapshot.barriers)
                    and not any(p.key < position.key for p in controls)):
                actions.append((position.key, 0, "", 0, kind, position))
        return sorted(actions, key=lambda a: a[:4])

    def _arrival(self, token, target):
        if token.producer_id != self._ingress._producer_id:
            result = self._runner.dispatch_next(_coordinator_owner=self._owner, _through=token.position)
            if result is None:
                raise WorkloadInfrastructureError("selected workload arrival disappeared")
            return
        # Grant has the same strict-past proof as physical completeness.
        granted = self._ingress.next_due(token.position)
        if granted is not token:
            raise WorkloadInfrastructureError("selected live arrival lost exact grant")
        op = self._operations[token.event_id]
        session = self._runner._session
        if op.receipt.operation_id == "program.submit":
            try:
                value = session.submit_live_program(self._runner._owner, op.credential,
                                                    self._program_request(op), op.receipt)
            except (PermissionError, ProgramRequestError, SessionCapacityError, JobExpiredError) as error:
                # Host capacity and a replay of a discarded job are returned as
                # released denials; raising here would stall the pump and leave
                # the submitter without any reply.
                code = ("OPERATION_DENIED" if isinstance(error, PermissionError)
                        else "HOST_CAPACITY_REACHED" if isinstance(error, SessionCapacityError)
                        else "JOB_EXPIRED" if isinstance(error, JobExpiredError)
                        else "INVALID_PROGRAM")
                # 満たされなかったサービス条件は、送信内容だけで決まる公開情報として返す。
                reason = _public_reason(error) if code == "INVALID_PROGRAM" else None
                def deny():
                    with self._executor._gate:
                        self._open()
                        self._ingress.retire(token, decision="denied")
                        op.error_code = code
                        if reason is not None:
                            op.value = {"reason": reason}
                        op.release = self._timing.denial_release(token.position)
                        op.arrived = op.applied = True
                session.commit_workload_transition(self._runner._owner, op.credential,
                    op.receipt.principal_id, deny)
                return
            def publish():
                with self._executor._gate:
                    self._ingress.retire(token, decision="admitted", job_id=value.job_id)
                    self._arrivals.setdefault(value.job_id, token.position)
                    op.value = value
                    op.release = _after(token.position, self._timing.processing_delay_us)
                    op.arrived = op.applied = True
            session.commit_workload_transition(self._runner._owner, op.credential,
                op.receipt.principal_id, publish)
        else:
            window = self._controls.config.control_timing.plan_control(token.position)
            def stage():
                with self._executor._gate:
                    self._ingress.retire(token, decision="admitted")
                    op.window, op.arrived = window, True
            session.commit_workload_transition(self._runner._owner, op.credential,
                op.receipt.principal_id, stage)

    def _program_request(self, op):
        """Parse a captured envelope at arrival, keeping grant-before-content order."""
        if type(op.payload) is not LiveProgramEnvelope:
            return op.payload
        # Decide permission before inspecting the circuit document. The common
        # preparation/admission path checks these grants again after parsing.
        service = self._runner._session._service
        principal = op.receipt.principal_id
        service._authorize(principal, "program.submit")
        if op.payload.initialization == "inherit":
            service._authorize(principal, STATE_INHERIT_OPERATION_ID)
        if op.payload.interleave is not None:
            service._authorize(principal, interleave_operation_id(op.payload.interleave))
        return op.payload.to_request()

    def _apply_control(self, op):
        session, executor = self._runner._session, self._executor
        def update():
            with executor._gate:
                principal = op.receipt.principal_id
                service = session._service
                error = None
                try:
                    if op.receipt.operation_id == "job.cancel":
                        trace = self._controls.cancel(op.credential, op.payload,
                            "live-" + str(op.receipt.sequence), arrival=op.window.arrival,
                            ingress_reservation=op.reservation)
                        value = JobStatusView(trace.job_id, JobStatus.CANCELLED)
                    elif op.receipt.operation_id == "job.status.read":
                        value = service._get_status_at(principal, op.payload, op.window.arrival)
                    elif op.receipt.operation_id == "job.victim_activity.read":
                        value = service._get_victim_activity_at(principal, op.payload, op.window.arrival)
                    else:
                        value = service._get_result_at(principal, op.payload, op.window.arrival)
                except JobExpiredError:
                    value, error = None, "JOB_EXPIRED"
                except JobNotFoundError:
                    value, error = None, "JOB_NOT_FOUND"
                except PermissionError:
                    session.workload_principal(self._runner._owner, op.credential)
                    value, error = None, "OPERATION_DENIED"
                except JobResultNotReadyError:
                    value, error = None, "RESULT_NOT_READY"
                except QueuedCancellationRejectedError:
                    value, error = None, "CANCELLATION_REJECTED"
                op.value, op.error_code, op.release = value, error, op.window.release
                op.applied = True
        session.commit_workload_transition(self._runner._owner, op.credential,
            op.receipt.principal_id, update)

    def _physical(self, kind, position, target):
        session, executor = self._runner._session, self._executor
        def seal():
            with executor._gate:
                if any(op.arrived and not op.applied and op.window.application.key < position.key
                       for op in self._operations.values()):
                    raise UnresolvedIngressError("physical commit crosses pending control")
                self._ingress.confirm_admission_cutoff(position)
                executor.frontier.seal_ingress(position)
        # The coordinator/session fence protects metadata even with no actor plan.
        self._runner.commit_coordinator_transition(
            seal, _coordinator_owner=self._owner,
        )
        if kind == "start":
            session.begin_declared_next(executor, _workload_owner=self._runner._owner)
        else:
            session.run_declared_next(executor, _workload_owner=self._runner._owner)

    def _retire_job_history(self):
        """Keep the newest retained_jobs terminal records; discard older ones.

        Only released terminal jobs that no pending request, workload result
        binding or the next physical job still names are discarded, together in
        every layer. Session idempotency keys stay, so replays cannot re-run.
        """
        if self._retained_jobs is None:
            return
        executor = self._executor
        runtime = executor.runtime
        with executor._gate:
            self._open()
            terminal = [job_id for job_id, job in runtime.scheduler._jobs.items()
                        if job.status in TERMINAL_JOB_STATUSES]
            excess = len(terminal) - self._retained_jobs
            if excess <= 0:
                return
            named = {op.payload for op in self._operations.values()
                     if not op.applied and type(op.payload) is str}
            named.update(job_id for job_id, _ in self._runner._book._result_inputs.values())
            named.update(runtime._scenario_workload_job_ids)
            for job_id in terminal[:excess]:
                if job_id in named or not executor.result_is_available(job_id):
                    continue
                try:
                    executor.forget_job(job_id, owner=self._controls._owner)
                except ValueError:
                    continue  # Latest physical job or an open journal: keep it.
                self._arrivals.pop(job_id, None)

    def pump(self, *, max_transitions):
        if type(max_transitions) is not int or not 1 <= max_transitions <= 16384:
            raise ValueError("pump needs a positive finite limit <= 16384")
        if not self._pump_gate.acquire(blocking=False):
            raise SessionBusyError("live world pump is already active")
        try:
            host_started_ns = monotonic_ns()
            self._open()
            count = 0
            while count < max_transitions:
                self._retire_job_history()
                target = self._sample_target()
                self._plan_queue()
                self._refine_dependencies(target)
                waiting = self._waiting()
                with self._executor._gate:
                    self._open()
                    actions = self._actions(target, waiting)
                if not actions:
                    break
                _, _, producer, event_id, kind, data = actions[0]
                if kind == "arrival":
                    self._arrival(data, target)
                elif kind == "control":
                    self._apply_control(data)
                elif kind == "workload_release":
                    self._runner.complete_result(producer, event_id, available=data,
                        _coordinator_owner=self._owner)
                else:
                    self._physical(kind, data, target)
                count += 1
            target = self._sample_target()
            # Finite actors may finish; a live world remains receptive until discard.
            settled = (all(s.finished for s in self._runner.states().values())
                and all(op.applied and op.release.key <= target.key for op in self._operations.values())
                and not self._executor.runtime.scheduler.queued_job_ids() and self._executor._active is None)
            confirmed = self._executor.frontier.snapshot().confirmed
            return WorldPumpReport(
                target, count, settled, count == max_transitions and not settled,
                monotonic_ns() - host_started_ns,
                max(0.0, target.time_us - (confirmed.time_us if confirmed is not None else 0.0)),
            )
        finally:
            self._pump_gate.release()

    def close(self):
        self._closed = True
        self._runner.close()
        self._ingress.close()
