"""Trusted, bounded declared-arrival ordering; no service or physical execution.

The owner publishes every source before dispatching and serializes callbacks
with workload transitions. This book makes metadata changes atomic, but cannot
make a service call, QPU commit, or exhaustive live ingress feed atomic. Result
barriers constrain admissions here; they are NOT physical frontier tickets.
"""

from dataclasses import dataclass
from threading import Lock, RLock
from weakref import WeakKeyDictionary

from core.attack_simulation.event_commit_boundary import (
    EventOrderError, EventPosition, UnresolvedIngressError,
)
from core.attack_simulation.victim_workload import (
    CausalBarrier, Outcome, Phase, WorkloadEvent, WorkloadPlan, WorkloadState,
)


def _producer(value: str) -> None:
    if (type(value) is not str or not 1 <= len(value) <= 128
            or not value.isascii() or any(c.isspace() for c in value)):
        raise ValueError("producer_id must be a bounded nonempty ASCII identifier")


def _event_id(value: int) -> None:
    if type(value) is not int or value < 1:
        raise ValueError("event_id must be an exact positive integer")


@dataclass(frozen=True)
class DeclaredArrival:
    """Explicit metadata for a trusted source other than a WorkloadState."""
    event_id: int
    position: EventPosition
    dispatching: bool = False

    def __post_init__(self) -> None:
        _event_id(self.event_id)
        if type(self.position) is not EventPosition or type(self.dispatching) is not bool:
            raise TypeError("an immutable position and explicit boolean phase are required")


@dataclass(frozen=True)
class IngressReservation:
    """Identity token: copied/equal values never authorize retirement."""
    producer_id: str
    event_id: int
    position: EventPosition

    def __post_init__(self) -> None:
        _producer(self.producer_id)
        _event_id(self.event_id)
        if type(self.position) is not EventPosition:
            raise TypeError("an immutable EventPosition is required")

    @property
    def key(self) -> tuple[str, int]:
        return self.producer_id, self.event_id

    @property
    def order_key(self) -> tuple[float, int, str, int]:
        return (*self.position.key, self.producer_id, self.event_id)


@dataclass(frozen=True)
class ProducerBarrier:
    producer_id: str
    producer_event_id: int
    earliest_next_arrival: EventPosition

    @property
    def key(self) -> tuple[str, int]:
        return self.producer_id, self.producer_event_id


@dataclass(frozen=True)
class IngressRetirement:
    reservation: IngressReservation
    decision: str


@dataclass(frozen=True)
class DeclaredIngressSnapshot:
    """Supervisor-only evidence; eligibility is not a transferable admission."""
    reservations: tuple[IngressReservation, ...]
    dispatching: tuple[IngressReservation, ...]
    barriers: tuple[ProducerBarrier, ...]
    history: tuple[IngressRetirement, ...]
    granted: IngressReservation | None
    confirmed_cutoff: EventPosition | None
    last_retired: IngressReservation | None
    eligible: IngressReservation | None
    closed: bool


class DeclaredIngressCapacityError(RuntimeError):
    """Host bookkeeping exhaustion, never a simulated denial or QPU failure."""


@dataclass(frozen=True, eq=False)
class PreparedBarrierReplacement:
    """Exact, weakly retained supervisor token for a changed control outcome."""
    expected: ProducerBarrier
    replacement: ProducerBarrier
    _issuer: object


class DeclaredIngressBook:
    """Atomically replace one producer's arrivals AND causal barriers.

    Deterministic global ties use (position.time_us, position.order, producer_id,
    event_id); ASCII producer identity is lexical and event ID is numeric. Host
    registration/callback/completion order never breaks ties. Only a barrier
    whose full (producer_id, producer_event_id) matches the candidate is exempt.

    next_due grants one exact token before a callback; repeated calls return it
    for exception replay, not permission to run concurrent callbacks. Retire
    only after real common admission/denial: retire(token, waiting_state)
    atomically records the decision AND publishes the bound WAITING state.
    Neither retirement nor host completion removes result barriers.
    Omission of ANY unresolved reservation is rejected, including DISPATCHING
    terminal steps without branches. Retired arrivals in retry snapshots are
    recognized but never reinserted. Binding a source to a different plan/run
    is rejected; use a distinct producer_id for another run.

    Pending arrivals and causal barriers each have max_pending entries. History
    is non-evicting up to max_history and preserves accepted decisions until an
    explicit trusted completed-reply release. The monotonic last-retired
    position remains after release. next_due fails BEFORE a new callback when
    history is full.
    Workload source bindings have at most max_pending + max_history entries. No
    implicit seal, cutoff, arbitrary eviction or recovery exists. Last-retired
    global order is retained as admission history: new arrivals cannot
    backinsert before/equal to that decision, even without a confirmed cutoff.
    Exact retained replays remain valid.
    confirm_admission_cutoff is an explicit
    trusted admission assertion, not a QPU frontier or proof of exhaustive feed.
    """

    def __init__(self, clock_domain: str, max_pending: int, max_history: int) -> None:
        EventPosition(clock_domain, 0, 0)
        for limit in (max_pending, max_history):
            if type(limit) is not int or limit < 1:
                raise ValueError("explicit positive exact integer limits are required")
        self._clock_domain = clock_domain
        self._max_pending = max_pending
        self._max_history = max_history
        self._pending: dict[tuple[str, int], IngressReservation] = {}
        self._dispatching: set[tuple[str, int]] = set()
        self._barriers: dict[tuple[str, int], ProducerBarrier] = {}
        self._history: dict[tuple[str, int], IngressRetirement] = {}
        self._owners: dict[str, tuple[str, WorkloadPlan]] = {}
        self._result_inputs: dict[tuple[str, int], tuple[str, str]] = {}
        self._granted: IngressReservation | None = None
        self._last_retired: IngressReservation | None = None
        self._confirmed_cutoff: EventPosition | None = None
        self._closed = False
        self._lock = Lock()
        self._transaction_lock_bound = False
        self._replacement_issuer = object()
        self._prepared_replacements = WeakKeyDictionary()

    def bind_transaction_lock(self, lock) -> None:
        """Unpublished pre-start assembly, before any grant/cutoff/retirement.

        Existing frozen source reservations may already be present. The trusted
        assembler must exclude concurrent users while replacing the local lock.
        """
        if type(lock) is not type(RLock()):
            raise TypeError("a shared reentrant transaction lock is required")
        with self._lock:
            if (self._transaction_lock_bound or self._closed or self._granted is not None
                    or self._history or self._confirmed_cutoff is not None):
                raise ValueError("bind only an unpublished unadvanced ingress book")
            self._transaction_lock_bound = True
            self._lock = lock

    def _open(self) -> None:
        if self._closed:
            raise RuntimeError("declared ingress book is closed")

    def _position(self, value: EventPosition) -> None:
        if type(value) is not EventPosition:
            raise TypeError("an immutable EventPosition is required")
        if value.clock_domain != self._clock_domain:
            raise ValueError("foreign world clock domain")

    def _known(self, key: tuple[str, int]) -> IngressReservation | None:
        record = self._history.get(key)
        return record.reservation if record is not None else self._pending.get(key)

    def _reservation(self, producer_id: str, arrival: DeclaredArrival) -> IngressReservation:
        self._position(arrival.position)
        known = self._known((producer_id, arrival.event_id))
        if known is not None:
            if known.position != arrival.position:
                raise ValueError("event identity is already bound to another position")
            return known
        token = IngressReservation(producer_id, arrival.event_id, arrival.position)
        if self._last_retired is not None and token.order_key <= self._last_retired.order_key:
            raise EventOrderError("arrival would backinsert before a retired admission decision")
        if (self._confirmed_cutoff is not None
                and token.position.key <= self._confirmed_cutoff.key):
            raise EventOrderError("arrival is behind the explicitly confirmed admission cutoff")
        if self._granted is not None and token.order_key < self._granted.order_key:
            raise EventOrderError("new arrival would precede an unresolved granted admission")
        return token

    def reserve(self, producer_id: str, event_id: int,
                position: EventPosition) -> IngressReservation:
        """Add one explicit source arrival; duplicate identity/position replays exactly."""
        _producer(producer_id)
        arrival = DeclaredArrival(event_id, position)
        with self._lock:
            self._open()
            token = self._reservation(producer_id, arrival)
            if self._known(token.key) is not None:
                return token
            if len(self._pending) >= self._max_pending:
                raise DeclaredIngressCapacityError("pending arrival capacity reached")
            self._pending[token.key] = token
            return token

    def _workload(self, state: WorkloadState) -> tuple[
        tuple[DeclaredArrival, ...], tuple[CausalBarrier, ...], tuple[str, WorkloadPlan],
    ]:
        if type(state) is not WorkloadState or type(state.plan) is not WorkloadPlan:
            raise TypeError("a trusted immutable WorkloadState is required")
        if state.closed:
            raise RuntimeError("cannot publish a closed workload")
        _producer(state.run_id)
        if state.plan.clock_domain != self._clock_domain:
            raise ValueError("foreign workload clock domain")
        if type(state.events) is not tuple or len(state.events) > state.plan.max_events:
            raise ValueError("workload events must be a bounded immutable tuple")
        seen = set()
        for event in state.events:
            if type(event) is not WorkloadEvent or type(event.phase) is not Phase:
                raise TypeError("typed workload events and phases are required")
            _event_id(event.event_id)
            self._position(event.arrival)
            if event.event_id in seen:
                raise ValueError("duplicate workload event identity")
            seen.add(event.event_id)
        arrivals = tuple(DeclaredArrival(e.event_id, e.arrival, e.phase is Phase.DISPATCHING)
                         for e in state.ingress_reservations())
        return arrivals, state.barriers(), (state.run_id, state.plan)

    def _owner(self, producer_id: str, owner: tuple[str, WorkloadPlan]) -> None:
        if producer_id in self._owners and self._owners[producer_id] != owner:
            raise ValueError("producer belongs to another workload plan/run")
        if (producer_id not in self._owners
                and len(self._owners) >= self._max_pending + self._max_history):
            raise DeclaredIngressCapacityError("workload producer binding capacity reached")

    def publish(self, producer_id: str, state: WorkloadState) -> tuple[IngressReservation, ...]:
        """Publish one source atomically; retired DISPATCHING replays never revive.

        Use retire(token, waiting_state) for an admission completion, avoiding a
        gap between removing its reservation and publishing its result barrier.
        """
        _producer(producer_id)
        arrivals, barriers, owner = self._workload(state)
        with self._lock:
            self._open()
            self._owner(producer_id, owner)
            tokens = self._publish(producer_id, arrivals, barriers)
            self._owners[producer_id] = owner
            self._bind_result_inputs(producer_id, state)
            return tokens

    def _bind_result_inputs(self, producer_id: str, state: WorkloadState) -> None:
        self._result_inputs = {key: value for key, value in self._result_inputs.items()
                               if key[0] != producer_id}
        self._result_inputs.update({(producer_id, event.event_id): (event.job_id, event.step_id)
            for event in state.events if event.phase is Phase.WAITING and event.job_id is not None
            and (producer_id, event.event_id) in self._barriers})

    def cancellation_successor_bound(self, expected: ProducerBarrier, job_id: str,
                                     release: EventPosition) -> EventPosition:
        """Pure bound from the bound common job and declared successor durations."""
        with self._lock:
            self._open()
            self._position(release)
            if type(expected) is not ProducerBarrier or self._barriers.get(expected.key) is not expected:
                raise ValueError("require the exact current result dependency")
            binding = self._result_inputs.get(expected.key)
            if binding is None or binding[0] != job_id:
                raise ValueError("dependency belongs to another common job")
            plan = self._owners[expected.producer_id][1]
            step = next(s for s in plan.steps if s.step_id == binding[1])
            successors = [s for s in plan.steps if any(b.next_step == s.step_id for b in step.branches)]
            candidates = []
            for successor in successors:
                time_us = release.time_us
                for delay in (step.durations.inbound_us, successor.durations.classical_us,
                              successor.durations.outbound_us):
                    next_time = EventPosition(release.clock_domain, time_us + delay, 0).time_us
                    if delay > 0 and next_time == time_us:
                        raise ValueError("positive successor duration collapsed in world time")
                    time_us = next_time
                candidates.append(time_us)
            time_us = min(candidates)
            return EventPosition(release.clock_domain, time_us,
                                 release.order + 1 if time_us == release.time_us else 0)

    def publish_snapshot(self, producer_id: str,
                         ingress_reservations: tuple[DeclaredArrival, ...],
                         barriers: tuple[CausalBarrier, ...]) -> tuple[IngressReservation, ...]:
        """Atomic explicit-source alternative to publish; trusted metadata only."""
        _producer(producer_id)
        with self._lock:
            self._open()
            if producer_id in self._owners:
                raise ValueError("a workload-bound producer must use publish")
            return self._publish(producer_id, ingress_reservations, barriers)

    def _publish(self, producer_id: str, arrivals: tuple[DeclaredArrival, ...],
                 barriers: tuple[CausalBarrier, ...],
                 retirement: IngressRetirement | None = None) -> tuple[IngressReservation, ...]:
        # Stage everything under the lock; commit only after ALL checks succeed.
        if (type(arrivals) is not tuple or type(barriers) is not tuple
                or len(arrivals) > self._max_pending + self._max_history
                or len(barriers) > self._max_pending):
            raise DeclaredIngressCapacityError("producer snapshot must be bounded immutable tuples")
        pending = {k: v for k, v in self._pending.items() if k[0] != producer_id}
        dispatching = {k for k in self._dispatching if k[0] != producer_id}
        dependencies = {k: v for k, v in self._barriers.items() if k[0] != producer_id}
        history = dict(self._history)
        granted = self._granted
        last_retired = self._last_retired
        if retirement is not None:
            history[retirement.reservation.key] = retirement
            granted = None
            if (last_retired is None
                    or retirement.reservation.order_key > last_retired.order_key):
                last_retired = retirement.reservation
        tokens = []
        seen = set()
        for arrival in arrivals:
            if type(arrival) is not DeclaredArrival:
                raise TypeError("explicit DeclaredArrival values are required")
            token = self._reservation(producer_id, arrival)
            if token.key in seen:
                raise ValueError("duplicate arrival identity")
            seen.add(token.key)
            tokens.append(token)
            if token.key not in history:
                pending[token.key] = token
                if arrival.dispatching or token.key in self._dispatching:
                    dispatching.add(token.key)
        if any(k[0] == producer_id and k not in pending and k not in history
               for k in self._pending):
            raise UnresolvedIngressError("retire exact unresolved reservations before removing them")
        if last_retired is not None and any(
                t.order_key <= last_retired.order_key for t in pending.values()):
            raise EventOrderError("replacement would backinsert before a retired admission decision")
        for barrier in barriers:
            if type(barrier) is not CausalBarrier:
                raise TypeError("explicit CausalBarrier values are required")
            _event_id(barrier.producer_event_id)
            self._position(barrier.earliest_next_arrival)
            dependency = ProducerBarrier(producer_id, barrier.producer_event_id,
                                         barrier.earliest_next_arrival)
            # A closed-source coordinator may prove a tighter service-result
            # lower bound. Republishing the unchanged workload must not lose it.
            prior = self._barriers.get(dependency.key)
            if prior is not None and prior.earliest_next_arrival.key > dependency.earliest_next_arrival.key:
                dependency = prior
            if dependency.key in dependencies:
                raise ValueError("duplicate causal barrier identity")
            if (self._confirmed_cutoff is not None
                    and dependency.earliest_next_arrival.key <= self._confirmed_cutoff.key):
                raise EventOrderError("barrier is behind the confirmed admission cutoff")
            if last_retired is not None and self._blocks(dependency, last_retired):
                raise EventOrderError("barrier would precede a retired admission decision")
            if granted is not None and self._blocks(dependency, granted):
                raise EventOrderError("barrier would invalidate an unresolved granted admission")
            dependencies[dependency.key] = dependency
        if len(pending) > self._max_pending or len(dependencies) > self._max_pending:
            raise DeclaredIngressCapacityError("pending arrival or causal barrier capacity reached")
        self._pending, self._dispatching, self._barriers = pending, dispatching, dependencies
        self._history, self._granted = history, granted
        self._last_retired = last_retired
        return tuple(tokens)

    @staticmethod
    def _blocks(barrier: ProducerBarrier, candidate: IngressReservation) -> bool:
        return (barrier.key != candidate.key
                and barrier.earliest_next_arrival.key <= candidate.position.key)

    def refine_barrier(self, producer_id: str, event_id: int, earliest: EventPosition) -> None:
        """Trusted causal proof, never a participant hint or barrier removal.

        Only increase an existing lower bound. The assembler must prove that no
        result/control route can produce a successor before this position. In
        particular a live cancellation route invalidates queued-job assumptions
        unless its arrivals and response times are coordinated as well.
        """
        _producer(producer_id)
        _event_id(event_id)
        with self._lock:
            self._open()
            self._position(earliest)
            key = producer_id, event_id
            prior = self._barriers.get(key)
            if prior is None:
                raise ValueError("only an existing result dependency can be refined")
            if earliest.key < prior.earliest_next_arrival.key:
                raise EventOrderError("a proven result lower bound cannot move backwards")
            self._barriers[key] = ProducerBarrier(producer_id, event_id, earliest)

    def _eligible(self, through: EventPosition) -> IngressReservation | None:
        candidate = min(self._pending.values(), key=lambda t: t.order_key, default=None)
        if (candidate is None or candidate.position.key > through.key
                or any(self._blocks(b, candidate) for b in self._barriers.values())):
            return None
        return candidate

    def _validate_replacement_position(self, barrier: ProducerBarrier) -> None:
        self._position(barrier.earliest_next_arrival)
        if (self._confirmed_cutoff is not None
                and barrier.earliest_next_arrival.key <= self._confirmed_cutoff.key):
            raise EventOrderError("replacement crosses confirmed admission cutoff")
        for reservation in (self._last_retired, self._granted):
            if reservation is not None and self._blocks(barrier, reservation):
                raise EventOrderError("replacement invalidates granted or retired arrival")

    def prepare_barrier_replacement(self, expected: ProducerBarrier,
                                    earliest: EventPosition) -> PreparedBarrierReplacement:
        """Stage control-dependent evidence without pretending it is refinement.

        Decreasing a success-only bound is permitted only while no confirmed
        cutoff, grant or retired admission relies on it. Physics completeness is
        a separate coordinator obligation under the same transaction gate.
        """
        with self._lock:
            self._open()
            if type(expected) is not ProducerBarrier or self._barriers.get(expected.key) is not expected:
                raise ValueError("replacement requires the exact current dependency")
            self._position(earliest)
            replacement = ProducerBarrier(*expected.key, earliest)
            self._validate_replacement_position(replacement)
            token = PreparedBarrierReplacement(expected, replacement, self._replacement_issuer)
            self._prepared_replacements[token] = True
            return token

    def _replacement_token(self, token: PreparedBarrierReplacement) -> None:
        if (type(token) is not PreparedBarrierReplacement
                or token._issuer is not self._replacement_issuer
                or token not in self._prepared_replacements):
            raise ValueError("replacement token is foreign, copied or retired")

    def commit_barrier_replacement(self, token: PreparedBarrierReplacement) -> ProducerBarrier:
        """Short CAS commit; exact replay does not overwrite newer evidence."""
        with self._lock:
            self._open()
            self._replacement_token(token)
            current = self._barriers.get(token.expected.key)
            if current is token.replacement:
                return current
            if current is not token.expected:
                raise ValueError("dependency changed during replacement preparation")
            self._validate_replacement_position(token.replacement)
            self._barriers[token.expected.key] = token.replacement
            return token.replacement

    def _rollback_barrier_replacement(self, token: PreparedBarrierReplacement) -> None:
        """Compensate only this unpublished CAS while the shared gate is held."""
        with self._lock:
            self._replacement_token(token)
            current = self._barriers.get(token.expected.key)
            if current is token.expected:
                return
            if current is not token.replacement:
                raise ValueError("cannot undo a later dependency transition")
            self._barriers[token.expected.key] = token.expected

    def next_due(self, through: EventPosition) -> IngressReservation | None:
        """Grant earliest unresolved arrival, or None; never skip a blocked dispatch."""
        with self._lock:
            self._open()
            self._position(through)
            token = self._eligible(through)
            if token is None:
                return None
            if self._granted is None:
                if len(self._history) >= self._max_history:
                    raise DeclaredIngressCapacityError("retirement history full; no new admission grant")
                self._granted = token
                self._dispatching.add(token.key)
            return token

    def _exact(self, token: IngressReservation) -> None:
        if type(token) is not IngressReservation or self._known(token.key) is not token:
            raise ValueError("reservation is foreign, copied or unknown")

    def retire(self, token: IngressReservation, state: WorkloadState | None = None,
               *, decision: str | None = None) -> IngressRetirement:
        """Atomically retire AND publish WAITING after real common admission/denial.

        With state, decision derives only from its explicitly bound submission:
        a job_id means admitted, Outcome.DENIED with no job means denied. This
        trusts the assembler, not worker completion. For an explicit non-workload
        source, omit state and supply decision='admitted' or 'denied'; its already
        published barriers remain. Workload-bound sources MUST supply state.
        Exceptions before a common decision leave the same grant for replay.
        """
        replacement = self._workload(state) if state is not None else None
        with self._lock:
            self._open()
            self._exact(token)
            if replacement is not None:
                arrivals, barriers, owner = replacement
                self._owner(token.producer_id, owner)
                event = next((e for e in state.events if e.event_id == token.event_id), None)
                if (event is None or event.phase is not Phase.WAITING
                        or event.arrival != token.position):
                    raise ValueError("completion must bind this exact arrival to WAITING")
                if event.job_id is None and event.outcome is Outcome.DENIED:
                    bound_decision = "denied"
                elif type(event.job_id) is str and event.outcome is None:
                    _producer(event.job_id)
                    bound_decision = "admitted"
                else:
                    raise ValueError("WAITING must contain a common job or an explicit denial")
                if decision is not None and decision != bound_decision:
                    raise ValueError("explicit decision differs from the bound submission")
                decision = bound_decision
            elif token.producer_id in self._owners:
                raise ValueError("workload retirement requires the bound WAITING state")
            if type(decision) is not str or decision not in ("admitted", "denied"):
                raise ValueError("retirement requires an explicit admitted/denied decision")
            record = self._history.get(token.key)
            if record is not None:
                if record.decision != decision:
                    raise ValueError("retirement decision cannot change on replay")
                if replacement is None:
                    return record
            if self._granted is not token:
                if record is None:
                    raise UnresolvedIngressError("only the exact granted reservation can retire")
                # A replay must not release another outstanding grant.
                if self._granted is not None:
                    raise UnresolvedIngressError("cannot replay completion during another admission")
            record = record or IngressRetirement(token, decision)
            if replacement is not None:
                self._publish(token.producer_id, arrivals, barriers, retirement=record)
                self._owners[token.producer_id] = owner
                self._bind_result_inputs(token.producer_id, state)
                return record
            # next_due reserved history capacity before allowing the service call.
            self._history[token.key] = record
            del self._pending[token.key]
            self._dispatching.discard(token.key)
            self._granted = None
            self._last_retired = token
            return record

    def decision(self, token: IngressReservation) -> str | None:
        """Trusted exact-token replay lookup, also available after close."""
        with self._lock:
            self._exact(token)
            record = self._history.get(token.key)
            return None if record is None else record.decision

    def forget_retired_reply(self, token: IngressReservation) -> bool:
        """Free a retired arrival after its exact completed reply is cached.

        The monotonic last-retired position remains, so this cannot permit an
        older arrival to be inserted after the request evidence is reclaimed.
        Callers must first cache the exact response and verify completed receipt
        evidence; unresolved requests are never eligible for this operation.
        """
        with self._lock:
            self._open()
            self._exact(token)
            if token.key in self._pending or self._granted is token:
                raise UnresolvedIngressError("an unresolved request cannot be forgotten")
            record = self._history.get(token.key)
            if record is None:
                return False
            if record.decision not in {"admitted", "denied"}:
                raise ValueError("only a completed request arrival may be forgotten")
            del self._history[token.key]
            return True

    def confirm_admission_cutoff(self, position: EventPosition) -> None:
        """Explicit trusted cutoff only; refuse to cross unresolved ingress/results."""
        with self._lock:
            self._open()
            self._position(position)
            if self._confirmed_cutoff is not None and position.key < self._confirmed_cutoff.key:
                raise EventOrderError("confirmed admission cutoff cannot move backwards")
            if (any(t.position.key <= position.key for t in self._pending.values())
                    or any(b.earliest_next_arrival.key <= position.key
                           for b in self._barriers.values())):
                raise UnresolvedIngressError("cutoff would cross unresolved ingress or causal result")
            self._confirmed_cutoff = position

    def snapshot(self, through: EventPosition | None = None) -> DeclaredIngressSnapshot:
        """Immutable consistent supervisor evidence; optional read-only eligibility."""
        with self._lock:
            if through is not None:
                self._position(through)
            reservations = tuple(sorted(self._pending.values(), key=lambda t: t.order_key))
            eligible = (self._eligible(through) if through is not None and not self._closed
                        and (self._granted is not None or len(self._history) < self._max_history)
                        else None)
            return DeclaredIngressSnapshot(
                reservations, tuple(t for t in reservations if t.key in self._dispatching),
                tuple(sorted(self._barriers.values(), key=lambda b: (
                    *b.earliest_next_arrival.key, *b.key))),
                tuple(self._history.values()), self._granted, self._confirmed_cutoff,
                self._last_retired,
                eligible, self._closed,
            )

    def close(self) -> None:
        """Fence changes, retaining all unresolved and retired evidence."""
        with self._lock:
            self._closed = True
