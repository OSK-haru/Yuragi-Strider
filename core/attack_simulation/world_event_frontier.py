"""Bounded declared-world bookkeeping, not a live clock or QPU transaction.

All positions and the ingress watermark come from a trusted coordinator. The
watermark asserts that no further ingress at or before it will be registered;
this component cannot infer that assertion from host time. It owns no physical
state, jobs, RNG, budgets, event scheduling or participant-visible timestamps.
"""

from dataclasses import dataclass
from threading import Lock, RLock

from core.attack_simulation.event_commit_boundary import (
    EventOrderError, EventPosition, UnresolvedIngressError, validate_event_commit,
)


class WorldFrontierCapacityError(RuntimeError):
    """Host bookkeeping is full, not a simulated QPU failure."""


@dataclass(frozen=True)
class WorldIngressTicket:
    sequence: int
    position: EventPosition


@dataclass(frozen=True)
class WorldFrontierSnapshot:
    target: EventPosition | None
    sealed_through: EventPosition | None
    confirmed: EventPosition | None
    unresolved: tuple[WorldIngressTicket, ...]
    closed: bool


class DeclaredWorldFrontier:
    """Atomically track timing metadata from an explicitly supplied world trace.

    This is NOT an atomic check + QPU commit implementation. Before a future
    live integration can publish physical effects it must coordinate the QPU
    transaction, scheduler and this frontier, with an exhaustive ingress feed.
    A receipt must not be resolved until its admission/rejection/cancellation
    decision and any resulting scheduled effects have been recorded by owners.
    """

    def __init__(self, *, clock_domain: str, max_unresolved: int) -> None:
        EventPosition(clock_domain, 0, 0)  # Reuse strict world-domain validation.
        if type(max_unresolved) is not int or max_unresolved < 1:
            raise ValueError("an explicit positive ingress limit is required")
        self._clock_domain = clock_domain
        self._max_unresolved = max_unresolved
        self._target: EventPosition | None = None
        self._sealed_through: EventPosition | None = None
        self._confirmed: EventPosition | None = None
        self._pending: dict[int, WorldIngressTicket] = {}
        self._next_sequence = 1
        self._closed = False
        self._lock = Lock()
        self._transaction_bound = False

    def bind_transaction_lock(self, lock) -> None:
        """Trusted fresh assembly; share one gate with QPU/job authorities."""
        if type(lock) is not type(RLock()):
            raise TypeError("a reentrant transaction lock is required")
        with self._lock:
            if (self._transaction_bound or self._closed or self._target is not None
                    or self._pending or self._confirmed is not None):
                raise ValueError("transaction binding requires a fresh frontier")
            self._transaction_bound = True
            self._lock = lock

    def _position(self, position: EventPosition) -> None:
        if type(position) is not EventPosition:
            raise TypeError("an explicit immutable world position is required")
        if position.clock_domain != self._clock_domain:
            raise ValueError("world clock domains cannot be mixed")

    def _open(self) -> None:
        if self._closed:
            raise RuntimeError("world frontier is closed")

    def set_target(self, position: EventPosition) -> None:
        """Update nominal progress independently of numerical completion."""
        with self._lock:
            self._open()
            self._position(position)
            if self._target is not None and position.key < self._target.key:
                raise EventOrderError("world target cannot move backwards")
            self._target = position

    def register_ingress(self, position: EventPosition) -> WorldIngressTicket:
        with self._lock:
            self._open()
            self._position(position)
            if self._sealed_through is not None and position.key <= self._sealed_through.key:
                raise EventOrderError("ingress is behind the declared watermark")
            if self._confirmed is not None and position.key <= self._confirmed.key:
                raise EventOrderError("ingress is behind the confirmed frontier")
            if len(self._pending) >= self._max_unresolved:
                raise WorldFrontierCapacityError("unresolved world ingress capacity reached")
            ticket = WorldIngressTicket(self._next_sequence, position)
            self._pending[ticket.sequence] = ticket
            self._next_sequence += 1
            return ticket

    def resolve_ingress(self, ticket: WorldIngressTicket) -> None:
        """Retire the exact reservation once its owner's decision is complete."""
        with self._lock:
            self._open()
            if type(ticket) is not WorldIngressTicket or self._pending.get(ticket.sequence) is not ticket:
                raise ValueError("ticket is foreign, copied or already resolved")
            del self._pending[ticket.sequence]

    def seal_ingress(self, position: EventPosition) -> None:
        """Trusted assertion of exhaustive arrivals, never a host-clock guess.

        Earlier registered but unresolved requests remain barriers. A live
        ingress adapter must publish this watermark under its own arrival/time
        mapping contract; simply reading a wall clock is not sufficient.
        """
        with self._lock:
            self._open()
            self._position(position)
            if self._sealed_through is not None and position.key < self._sealed_through.key:
                raise EventOrderError("ingress watermark cannot move backwards")
            if self._target is None or position.key > self._target.key:
                raise ValueError("ingress watermark must not exceed the declared target")
            self._sealed_through = position

    def confirm(self, candidate: EventPosition) -> None:
        """Check and advance timing metadata only; no physical state is committed."""
        with self._lock:
            self._validate_confirmation(candidate)
            self._confirmed = candidate

    def _validate_confirmation(self, candidate: EventPosition) -> None:
        """Called under the common transaction gate; never mutates metadata."""
        self._open()
        self._position(candidate)
        if self._target is None or candidate.key > self._target.key:
            raise ValueError("candidate exceeds the declared world target")
        if self._sealed_through is None or candidate.key > self._sealed_through.key:
            raise UnresolvedIngressError("declare exhaustive ingress before confirming")
        validate_event_commit(
            committed=self._confirmed, candidate=candidate,
            unresolved=tuple(ticket.position for ticket in self._pending.values()),
            max_unresolved=self._max_unresolved,
        )

    def commit_with(self, candidate: EventPosition, update):
        """Trusted short, prevalidated owner update + frontier confirmation.

        Owners MUST share this reentrant gate and validate all candidate writes
        before update. No numerical work or arbitrary participant callback here.
        An exception before owner mutation leaves the frontier unchanged; this
        method cannot roll back an arbitrary callback that partially mutates.
        """
        with self._lock:
            if not self._transaction_bound:
                raise ValueError("atomic owner commits require a shared transaction gate")
            self._validate_confirmation(candidate)
            result = update()
            self._confirmed = candidate
            return result

    def snapshot(self) -> WorldFrontierSnapshot:
        with self._lock:
            return WorldFrontierSnapshot(self._target, self._sealed_through,
                                         self._confirmed, tuple(self._pending.values()), self._closed)

    def close(self) -> None:
        """Stop bookkeeping; retain evidence, without Reset or checkpoint resume."""
        with self._lock:
            self._closed = True
