"""Opt-in declared workload admission over the real authenticated session.

This connects frozen plans, the nominal clock and ordered common admission.
Physical execution is a separate optional declared-trace owner; its registered
result release is checked here. This module does NOT generate live ingress
seals, physical windows, release durations, autonomous execution or recovery.
Until live mapping exists this world rejects uncoordinated HTTP requests.
All methods here are trusted supervisor APIs, never participant routes.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Lock, RLock
from time import monotonic_ns
from types import MappingProxyType

from api.attack_sessions import AttackSession, SessionBusyError
from core.attack_simulation.declared_ingress import DeclaredIngressBook
from core.attack_simulation.declared_world_clock import DeclaredWorldClock, WorldClockSettings
from core.attack_simulation.event_commit_boundary import EventPosition
from core.attack_simulation.victim_workload import (
    Phase, Submission, WorkloadState, advance_result, begin_dispatch,
    bind_submission, close_workload, dispatch_operation, read_own_result, start_workload,
)
from core.attack_simulation.workload_config import parse_workload_plan


class WorkloadInfrastructureError(RuntimeError):
    """Preserve pending evidence; never invent a simulated denial or failure."""


@dataclass(frozen=True)
class WorkloadDispatch:
    producer_id: str
    event_id: int
    submission: Submission


class _DeclaredClient:
    def __init__(self, runner, principal_id, credential):
        self._runner = runner
        self._principal = principal_id
        self._credential = credential

    @property
    def principal_id(self):
        return self._principal

    def _context(self):
        try:
            actual = self._runner._session.workload_principal(self._runner._owner, self._credential)
            if actual != self._principal:
                raise PermissionError("workload identity changed")
        except PermissionError:
            raise WorkloadInfrastructureError("workload authentication or world binding is unavailable") from None

    def submit_declared_program(self, request, *, arrival):
        # Arrival is reserved by the book BEFORE this method. It never becomes
        # a host receipt or a participant-provided backdate field.
        self._context()
        try:
            return self._runner._session.request(
                self._credential, lambda client, workspace: client.submit_program(request),
                operation_id="program.submit", _workload_owner=self._runner._owner,
            )
        except PermissionError:
            self._context()  # Expiry/close is infrastructure; a grant denial is an outcome.
            raise

    def get_result(self, job_id):
        self._context()
        try:
            return self._runner._session.request(
                self._credential, lambda client, workspace: client.get_result(job_id),
                operation_id="job.result.read", _workload_owner=self._runner._owner,
            )
        except PermissionError:
            self._context()
            raise

    def commit_transition(self, update):
        try:
            return self._runner._session.commit_workload_transition(
                self._runner._owner, self._credential, self._principal, update,
            )
        except PermissionError:
            raise WorkloadInfrastructureError("workload authentication or world binding is unavailable") from None

    def result_position(self, job_id):
        self._context()
        return self._runner._session.workload_result_position(
            self._runner._owner, self._credential, job_id,
        )


class DeclaredWorkloadRunner:
    """One session, frozen actor plans, finite ordered admission, no OS code.

    A short state lock protects snapshots; the single dispatch gate is separate
    from clock sampling and from the session's physical executor. Host waiting
    never advances a workload's declared transport or device duration.
    """

    def __init__(self, *, session: AttackSession, credentials: Mapping[str, str],
                 run_id: str, max_pending: int, max_history: int,
                 host_ns: Callable[[], int] = monotonic_ns):
        if type(session) is not AttackSession or not isinstance(credentials, Mapping):
            raise TypeError("a common session and virtual credential bindings are required")
        manifest = session.manifest
        clock_config = manifest.experiment_config.get("world_clock")
        if not isinstance(clock_config, Mapping) or set(clock_config) != {"clock_domain", "rate"}:
            raise ValueError("freeze explicit world clock settings before environment establishment")
        settings = WorldClockSettings(**dict(clock_config))
        clock = DeclaredWorldClock(settings, host_ns=host_ns)
        book = DeclaredIngressBook(clock_domain=settings.clock_domain,
                                   max_pending=max_pending, max_history=max_history)
        states = {}
        plans = []
        for role, config in (("attacker", manifest.attacker_config), ("victim", manifest.victim_config)):
            if config.get("workload") is None:      # absent, or a free-mode slot the author left empty
                continue
            plan = parse_workload_plan(config["workload"])
            if plan.clock_domain != settings.clock_domain or plan.plan_id in states:
                raise ValueError("workloads need a common domain and unique producer IDs")
            session.validate_workload_binding(plan, role=role)
            state = start_workload(plan, run_id=run_id)
            states[plan.plan_id] = state
            book.publish(plan.plan_id, state)
            plans.append(plan)
        if set(credentials) != {plan.actor_id for plan in plans}:
            raise ValueError("bind exactly the declared workload actors' virtual credentials")
        bound_credentials = dict(credentials)
        for actor, credential in bound_credentials.items():
            if session.workload_principal(None, credential) != actor:
                raise ValueError("workload credentials belong to another actor")
        self._session = session
        self._clock = clock
        self._book = book
        self._states = states
        self._owner = object()
        self._clients = {plan.plan_id: _DeclaredClient(self, plan.actor_id, bound_credentials[plan.actor_id])
                         for plan in plans}
        self._lock = RLock()
        self._dispatch_gate = Lock()
        self._closed = False
        self._coordinator_owner = None
        session.claim_workload_runner(self._owner)  # Last, after side-effect-free validation.
        clock.mark_ready(runtime_ready=True, ingress_ready=True,
                         workload_ready=True, manifest_frozen=True)

    def _open(self):
        if self._closed:
            raise WorkloadInfrastructureError("declared world has been discarded")

    def claim_world_coordinator(self, owner: object) -> None:
        """One fresh closed-source owner; manual advancement is then fenced."""
        with self._dispatch_gate, self._lock:
            self._open()
            if (owner is None or self._coordinator_owner is not None
                    or self._clock._origin_ns is not None or self._book.snapshot().history):
                raise ValueError("world coordination requires a fresh unstarted runner")
            self._coordinator_owner = owner

    def _check_coordinator(self, owner):
        if self._coordinator_owner is not owner:
            raise PermissionError("this runner requires its bound world coordinator")

    def start(self, *, _coordinator_owner=None) -> EventPosition:
        with self._lock:
            self._open()
            self._check_coordinator(_coordinator_owner)
            for client in self._clients.values():
                client._context()
            return self._clock.start()

    def target(self) -> EventPosition:
        # No dispatch/state lock: nominal time still progresses during compile.
        return self._clock.target()

    def states(self) -> Mapping[str, WorkloadState]:
        """Immutable supervisor evidence, not an attacker observation view."""
        with self._lock:
            return MappingProxyType(dict(self._states))

    def ingress_snapshot(self):
        return self._book.snapshot()

    def dispatch_next(self, *, _coordinator_owner=None, _through=None) -> WorkloadDispatch | None:
        """Attempt one due operation; exceptions retain its reservation for retry.

        No unbounded loop, sleeping or automatic retry. Every attempt uses the
        common session's credentials, grants, host limits and idempotency path.
        """
        if not self._dispatch_gate.acquire(blocking=False):
            raise SessionBusyError("workload admission is already active")
        try:
            self._check_coordinator(_coordinator_owner)
            if _through is not None:
                if (_coordinator_owner is None or type(_through) is not EventPosition
                        or _through.clock_domain != self._clock.settings.clock_domain):
                    raise PermissionError("an exact coordinator must supply the sampled live horizon")
                target = _through
            else:
                target = self.target()
            with self._lock:
                self._open()
                # The sampling horizon covers declared ties at the sampled time;
                # this is not a QPU watermark, confirmed frontier or public order.
                order = max((e.arrival.order for state in self._states.values()
                             for e in state.events), default=-1) + 1
                through = (target if _through is not None
                           else EventPosition(target.clock_domain, target.time_us, order))
                token = self._book.next_due(through)
                if token is None:
                    return None
                state = self._states[token.producer_id]
                event = next(e for e in state.events if e.event_id == token.event_id)
                if event.phase is Phase.SCHEDULED:
                    state = begin_dispatch(state, token.event_id, through=through)
                    self._book.publish(token.producer_id, state)
                    self._states[token.producer_id] = state
                elif event.phase is not Phase.DISPATCHING:
                    raise WorkloadInfrastructureError("ingress and workload phase disagree")
            submission = dispatch_operation(state, token.event_id, self._clients[token.producer_id])
            with self._lock:
                self._open()
                next_state = bind_submission(state, token.event_id, submission)
                def commit():
                    self._book.retire(token, next_state)
                    self._states[token.producer_id] = next_state
                self._clients[token.producer_id].commit_transition(commit)
                return WorkloadDispatch(token.producer_id, token.event_id, submission)
        finally:
            self._dispatch_gate.release()

    def complete_result(self, producer_id: str, event_id: int, *, available: EventPosition,
                        _coordinator_owner=None) -> None:
        """Supply declared release only after terminal common-result retrieval.

        Release time MUST come from the future physical/service coordinator.
        This method cannot prove its relation to a physical event timestamp;
        neither worker completion nor a browser timestamp is inferred here.
        """
        if not self._dispatch_gate.acquire(blocking=False):
            raise SessionBusyError("workload advancement is already active")
        try:
            self._check_coordinator(_coordinator_owner)
            with self._lock:
                self._open()
                state = self._states[producer_id]
                event = next(e for e in state.events if e.event_id == event_id)
                client = self._clients[producer_id]
            client._context()  # DENIED/no-job also needs a still-live binding.
            result = None if event.outcome is not None and event.job_id is None else read_own_result(state, event_id, client)
            if event.job_id is not None:
                declared_release = client.result_position(event.job_id)
                if declared_release is not None and available != declared_release:
                    raise ValueError("workload result time differs from committed service release")
            next_state = advance_result(state, event_id, available=available, result=result)
            with self._lock:
                self._open()
                def commit():
                    self._book.publish(producer_id, next_state)
                    self._states[producer_id] = next_state
                client.commit_transition(commit)
        finally:
            self._dispatch_gate.release()

    def refine_result_bound(self, producer_id, event_id, *, available, _coordinator_owner):
        """Closed-source service proof; public command/credential cannot call it."""
        with self._dispatch_gate, self._lock:
            self._open()
            self._check_coordinator(_coordinator_owner)
            self._check_closed_source_proof()
            state = self._states[producer_id]
            event = next(e for e in state.events if e.event_id == event_id)
            barrier = next((b for b in state.barriers() if b.producer_event_id == event_id), None)
            if barrier is None:
                return
            if available.clock_domain != event.arrival.clock_domain or available.key < event.arrival.key:
                raise ValueError("result lower bound must follow its own arrival")
            step = next(s for s in state.plan.steps if s.step_id == event.step_id)
            successors = [s for s in state.plan.steps if any(b.next_step == s.step_id for b in step.branches)]
            # Match the pure advancement's addition order, not subtraction of
            # rounded absolute coordinates. Exhausted branches are safe to keep
            # as a looser lower bound.
            time_us = min(available.time_us + step.durations.inbound_us
                          + s.durations.classical_us + s.durations.outbound_us for s in successors)
            position = EventPosition(available.clock_domain, time_us,
                                     available.order + 1 if time_us == available.time_us else 0)
            existing = next(b for b in self._book.snapshot().barriers
                            if b.key == (producer_id, event_id))
            if position.key <= existing.earliest_next_arrival.key:
                return
            self._clients[producer_id].commit_transition(
                lambda: self._book.refine_barrier(producer_id, event_id, position))

    def seal_physical_through(self, executor, candidate, *, _coordinator_owner):
        """Exhaustive proof for this closed, exclusively owned source set only."""
        def commit():
            self._check_closed_source_proof()
            if (executor.runtime._declared_executor is not executor
                    or executor.runtime is not self._session._service._runtime):
                raise PermissionError("physical owner belongs to another world")
            snapshot = executor.frontier.snapshot()
            if snapshot.closed or snapshot.target is None or candidate.key > snapshot.target.key:
                raise ValueError("physical seal must be within the current world target")
            if snapshot.sealed_through is not None and candidate.key < snapshot.sealed_through.key:
                raise ValueError("physical seal cannot move backwards")
            # No live/control route or unregistered producer is allowed.
            # The book covers every finite source and successor bound.
            self._book.confirm_admission_cutoff(candidate)
            executor.frontier.seal_ingress(candidate)
        self.commit_coordinator_transition(commit, _coordinator_owner=_coordinator_owner)

    def commit_coordinator_transition(self, update, *, _coordinator_owner):
        """Commit supervisor metadata for worlds with zero or more actor plans."""
        if not callable(update):
            raise TypeError("a short trusted metadata transition is required")
        with self._dispatch_gate, self._lock:
            self._open()
            self._check_coordinator(_coordinator_owner)
            # The session binds the runner's workload owner, not the world coordinator.
            # Only session disposal/binding loss becomes infrastructure; the
            # update's own fence rejections keep their PermissionError.
            entered = False

            def guarded():
                nonlocal entered
                entered = True
                return update()
            try:
                return self._session.commit_coordinator_transition(self._owner, guarded)
            except PermissionError:
                if entered:
                    raise
                raise WorkloadInfrastructureError("workload world binding is unavailable") from None

    def _check_closed_source_proof(self):
        service = self._session._service
        if service is None:
            raise WorkloadInfrastructureError("the declared service has been discarded")
        executor = service._runtime._declared_executor
        if executor is not None and executor._control_owner is not None:
            raise PermissionError("closed-only source proofs exclude timed control arrivals")

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._session.close()  # Revoke/fence early, drain without the state lock.
        with self._dispatch_gate, self._lock:
            self._states = {key: close_workload(state) for key, state in self._states.items()}
            self._book.close()
            self._clock.close()
