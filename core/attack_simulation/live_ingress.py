"""Supervisor-only live receipt capture and admission completeness evidence.

The assembler supplies one pre-start clock, book and shared RLock. Every source
must publish, grant, retire and confirm through this owner (or under that same
gate); direct concurrent mutation/sampling of the supplied owners is forbidden.
The owner creates its receipt ledger so its timestamp comes from the SAME call
that updates clock.target, not an independently sampled or client supplied ns.

Call capture BEFORE compilation/worker dispatch. Run those operations outside
the gate, and retire only after an explicit common service decision. Exceptions,
lost replies and cancellations do not retire arrivals. No abandon operation,
physical frontier mutation, HTTP adapter, worker or automatic retry is provided.
Receipts and book snapshots are trusted metadata, never participant responses.
"""

from dataclasses import dataclass
import math
from threading import RLock

from core.attack_simulation.declared_ingress import (
    DeclaredArrival, DeclaredIngressBook, DeclaredIngressCapacityError,
    DeclaredIngressSnapshot, IngressReservation, IngressRetirement, _producer,
)
from core.attack_simulation.declared_world_clock import DeclaredWorldClock
from core.attack_simulation.event_commit_boundary import EventOrderError, EventPosition
from core.attack_simulation.request_receipts import (
    ReceiptLimits, ReceiptOutcome, ReceiptRecord, RequestReceipt, RequestReceiptLedger,
)
from core.attack_simulation.victim_workload import CausalBarrier, WorkloadState


@dataclass(frozen=True)
class LiveIngressSettings:
    """Frozen pre-start assumptions; all fields, including zero, are required.

    outbound_delay_us is a declared world duration from host receipt to service
    arrival, without multiplying it by the clock rate. basis records its trusted
    configuration provenance, not a measured provider coefficient. The existing
    ledger's host domain is explicitly checked rather than silently renamed.
    """

    clock_domain: str
    host_clock_domain: str
    outbound_delay_us: float
    basis: str
    max_pending: int
    max_history: int

    def __post_init__(self) -> None:
        EventPosition(self.clock_domain, 0, 0)
        if self.host_clock_domain != "host_monotonic_ns" or type(self.host_clock_domain) is not str:
            raise ValueError("the receipt ledger requires host_monotonic_ns")
        if type(self.outbound_delay_us) not in (int, float):
            raise ValueError("outbound delay must be finite and nonnegative")
        try:
            delay = float(self.outbound_delay_us)
        except OverflowError:
            raise ValueError("outbound delay exceeds the supported range") from None
        if not math.isfinite(delay) or delay < 0:
            raise ValueError("outbound delay must be finite and nonnegative")
        object.__setattr__(self, "outbound_delay_us", delay)
        if type(self.basis) is not str or not self.basis.strip() or len(self.basis) > 1024:
            raise ValueError("an explicit nonempty bounded timing basis is required")
        ReceiptLimits(self.max_pending, self.max_history)


class LiveIngress:
    """Short metadata gate shared by ALL sources, not a service execution lock.

    The live producer namespace is exclusive to this owner. All its operations
    and principals share that namespace; event identity, not invented time/order,
    preserves same-ns receipt ties. For global ordering, grants as well as cutoffs
    require a strictly past time horizon: future same-time live receipts could
    otherwise backinsert before a retired declared producer's lexical tie.

    Private clock state is read only to obtain target's validated host sample
    and to enforce pre-start assembly. No host trace/ns mapping or order-setting
    API is added. The supplied gate may also guard the supervisor's transitions;
    compilation and common-service callbacks must never hold it.
    """

    def __init__(self, settings: LiveIngressSettings, *, clock: DeclaredWorldClock,
                 book: DeclaredIngressBook, gate: RLock, producer_id: str) -> None:
        if (type(settings) is not LiveIngressSettings
                or type(clock) is not DeclaredWorldClock
                or type(book) is not DeclaredIngressBook
                or type(gate) is not type(RLock())):
            raise TypeError("explicit settings, shared clock/book and RLock are required")
        _producer(producer_id)
        with gate:
            if clock._origin_ns is not None or clock._closed:
                raise RuntimeError("live ingress must be assembled before clock start")
            if (clock.settings.clock_domain != settings.clock_domain
                    or book._clock_domain != settings.clock_domain):
                raise ValueError("clock, ingress and book must share the world domain")
            if (book._max_pending != settings.max_pending
                    or book._max_history != settings.max_history):
                raise ValueError("book and live receipt capacities must agree explicitly")
            snapshot = book.snapshot()
            if snapshot.closed:
                raise RuntimeError("declared ingress book is closed")
            if (producer_id in book._owners
                    or any(t.producer_id == producer_id for t in snapshot.reservations)
                    or any(r.reservation.producer_id == producer_id for r in snapshot.history)
                    or any(b.producer_id == producer_id for b in snapshot.barriers)):
                raise ValueError("live producer namespace is already in use")
            self._settings = settings
            self._clock = clock
            self._book = book
            self._gate = gate
            self._producer_id = producer_id
            self._live: dict[tuple[str, int], tuple[RequestReceipt, IngressReservation]] = {}
            self._next_event_id = 1
            self._staged: IngressReservation | None = None
            self._sampling = self._capturing = self._closed = False
            self._last_capture_ns: int | None = None
            self._last_arrival: EventPosition | None = None
            self._last_sample_ns: int | None = None
            self._last_target: EventPosition | None = None
            self._receipts = RequestReceiptLedger(
                ReceiptLimits(settings.max_pending, settings.max_history),
                clock=self._capture_ns,
            )

    @property
    def settings(self) -> LiveIngressSettings:
        return self._settings

    def _open(self) -> None:
        if self._closed:
            raise RuntimeError("live ingress is closed")
        if self._sampling or self._capturing:
            raise RuntimeError("host clock callbacks must not reenter ingress")

    def _position(self, position: EventPosition) -> None:
        if type(position) is not EventPosition:
            raise TypeError("an immutable EventPosition is required")
        if position.clock_domain != self._settings.clock_domain:
            raise ValueError("foreign world clock domain")

    def _sample(self) -> EventPosition:
        # Retain the last GOOD mapping separately: clock.target permits float
        # collapse and has already updated its ns when our stronger check fails.
        previous_ns = (self._last_sample_ns if self._last_sample_ns is not None
                       else self._clock._origin_ns)
        previous = self._last_target
        if previous is None and previous_ns is not None:
            previous = self._clock._map_receipt_ns(previous_ns, order=0)
        self._sampling = True
        try:
            target = self._clock.target()
        finally:
            self._sampling = False
        if (previous is not None and self._clock._last_ns > previous_ns
                and target.time_us <= previous.time_us):
            raise ValueError("positive host progress collapsed in world coordinates")
        self._last_sample_ns = self._clock._last_ns
        self._last_target = target
        return target

    def sample(self) -> EventPosition:
        """Update the existing nominal target, returning world metadata only."""
        with self._gate:
            self._open()
            return self._sample()

    def _capture_ns(self) -> int:
        # Ledger.reserve validates metadata/capacity BEFORE invoking this clock.
        # Reserve in the book before returning the sample to the ledger. A failed
        # book reservation therefore creates no orphan receipt or fake denial.
        if not self._capturing:
            raise RuntimeError("receipt sampling is owned by capture")
        self._sample()
        timestamp = self._clock._last_ns
        received = self._clock._map_receipt_ns(timestamp, order=0)
        delay = self._settings.outbound_delay_us
        arrival = EventPosition(received.clock_domain, received.time_us + delay, 0)
        if delay > 0 and arrival.time_us <= received.time_us:
            raise ValueError("positive outbound delay collapsed in world coordinates")
        if (self._last_capture_ns is not None and timestamp > self._last_capture_ns
                and arrival.time_us <= self._last_arrival.time_us):
            raise ValueError("distinct receipt times collapsed at the service arrival")
        self._staged = self._book.reserve(self._producer_id, self._next_event_id, arrival)
        return timestamp

    def capture(self, principal_id: str, operation_id: str) -> RequestReceipt:
        """Capture trusted identity/operation only, before any host preparation.

        Return the ledger's exact object. Body, credentials, client timestamps,
        arbitrary order hints and compilation callbacks are not accepted here.
        """
        with self._gate:
            self._open()
            snapshot = self._book.snapshot()
            if snapshot.closed:
                raise RuntimeError("declared ingress book is closed")
            # Even an accidentally duplicated owner must not turn a foreign
            # book replay into a new, differently authenticated live receipt.
            known = (*snapshot.reservations, *(r.reservation for r in snapshot.history))
            if (any(t.producer_id == self._producer_id
                    and (t.key not in self._live or self._live[t.key][1] is not t)
                    for t in known)
                    or any(b.producer_id == self._producer_id for b in snapshot.barriers)):
                raise ValueError("live producer namespace contains foreign evidence")
            if (len(snapshot.reservations) >= self._settings.max_pending
                    or len(snapshot.history) >= self._settings.max_history):
                raise DeclaredIngressCapacityError("live capture bookkeeping capacity reached")
            self._capturing = True
            try:
                receipt = self._receipts.reserve(principal_id, operation_id)
                token = self._staged
                self._live[token.key] = (receipt, token)
                self._next_event_id += 1
                self._last_capture_ns = receipt.received_at_ns
                self._last_arrival = token.position
                return receipt
            finally:
                self._staged = None
                self._capturing = False

    def reservation(self, receipt: RequestReceipt) -> IngressReservation:
        """Supervisor exact-object lookup, including replay and closed evidence."""
        with self._gate:
            if self._sampling or self._capturing:
                raise RuntimeError("host clock callbacks must not reenter ingress")
            if type(receipt) is not RequestReceipt:
                raise ValueError("an exact receipt of this owner is required")
            pair = self._live.get((self._producer_id, receipt.sequence))
            if pair is None or pair[0] is not receipt:
                raise ValueError("receipt is foreign, copied or unknown")
            return pair[1]

    def release_reply(self, receipt: RequestReceipt) -> None:
        """Forget a replied operation; exact replay is retained by caller."""
        with self._gate:
            self._open()
            if type(receipt) is not RequestReceipt:
                raise ValueError("an exact live receipt is required")
            pair = self._live.get((self._producer_id, receipt.sequence))
            if pair is None or pair[0] is not receipt:
                raise ValueError("receipt is foreign, copied or already released")
            token = pair[1]
            if self._book.decision(token) is None:
                raise ValueError("operation has not reached its service decision")
            if not any(record.receipt is receipt and record.outcome is ReceiptOutcome.COMPLETED
                       for record in self._receipts.records()):
                raise ValueError("completed receipt evidence is unavailable")
            if not self._book.forget_retired_reply(token):
                raise ValueError("retired request evidence is unavailable")
            if not self._receipts.forget_completed(receipt):
                raise RuntimeError("validated completed receipt disappeared during release")
            del self._live[token.key]

    def _source(self, producer_id: str) -> None:
        if producer_id == self._producer_id:
            raise ValueError("live producer namespace is exclusively receipt-bound")

    def publish(self, producer_id: str, state: WorkloadState) -> tuple[IngressReservation, ...]:
        with self._gate:
            self._open()
            self._source(producer_id)
            return self._book.publish(producer_id, state)

    def publish_snapshot(self, producer_id: str, arrivals: tuple[DeclaredArrival, ...],
                         barriers: tuple[CausalBarrier, ...]) -> tuple[IngressReservation, ...]:
        with self._gate:
            self._open()
            self._source(producer_id)
            return self._book.publish_snapshot(producer_id, arrivals, barriers)

    def refine_barrier(self, producer_id: str, event_id: int, earliest: EventPosition) -> None:
        with self._gate:
            self._open()
            self._source(producer_id)
            self._book.refine_barrier(producer_id, event_id, earliest)

    def next_due(self, through: EventPosition) -> IngressReservation | None:
        """Exact grant before service callbacks; history capacity is checked first.

        The supervisor must serialize/replay service calls for this one grant.
        This method does not mark compilation, host failure or a lost response
        as a common service decision.
        """
        with self._gate:
            self._open()
            self._position(through)
            if through.time_us >= self._sample().time_us:
                raise EventOrderError("admission horizon must be strictly before sampled time")
            return self._book.next_due(through)

    def retire(self, token: IngressReservation, state: WorkloadState | None = None,
               *, decision: str | None = None, job_id: str | None = None) -> IngressRetirement:
        """Explicit service decision only; exact granted-token retirement/replay.

        Live receipts finish as COMPLETED for either common admitted or denied
        decisions; host preparation failures remain pending. Optional job_id is
        supervisor evidence only. Workload sources use the book's bound WAITING
        transition, including its result barriers. No host cancellation semantics
        are inferred. Completed live records can be released only after their
        exact projected reply is copied into the driver's bounded replay cache.
        """
        with self._gate:
            self._open()
            if type(token) is not IngressReservation:
                raise ValueError("an exact reservation is required")
            pair = self._live.get(token.key)
            if token.producer_id != self._producer_id:
                if job_id is not None:
                    raise ValueError("workload job evidence comes from its bound state")
                return self._book.retire(token, state, decision=decision)
            if pair is None or pair[1] is not token or state is not None:
                raise ValueError("live retirement requires its exact receipt-bound reservation")
            receipt = pair[0]
            if decision == "denied" and job_id is not None:
                raise ValueError("a denied decision cannot bind a job")
            record = ReceiptRecord(receipt, ReceiptOutcome.COMPLETED, job_id)
            previous = self._book.decision(token)
            if previous is None:
                self._receipts.validate(receipt, receipt.principal_id)
            else:
                retained = next(r for r in self._receipts.records() if r.receipt is receipt)
                if retained != record:
                    raise ValueError("receipt completion evidence cannot change on replay")
            result = self._book.retire(token, decision=decision)
            if previous is None:
                self._receipts.finish(receipt, outcome=record.outcome, job_id=record.job_id)
            return result

    def confirm_admission_cutoff(self, position: EventPosition) -> None:
        """Trusted supervisor proof up to this book, NEVER a physical seal.

        Sampling and the assertion share capture's gate. Only strictly smaller
        time_us is provable: equal-ns/equal-time future receipts are possible.
        No epsilon, nextafter or made-up tie order seals the sampled coordinate.
        The book additionally refuses unresolved arrivals and result barriers.
        """
        with self._gate:
            self._open()
            self._position(position)
            if position.time_us >= self._sample().time_us:
                raise EventOrderError("cutoff must be strictly before sampled time")
            self._book.confirm_admission_cutoff(position)

    def snapshot(self) -> DeclaredIngressSnapshot:
        """Trusted book evidence only; does not sample, grant or expose host trace."""
        with self._gate:
            if self._sampling or self._capturing:
                raise RuntimeError("host clock callbacks must not reenter ingress")
            return self._book.snapshot()

    def close(self) -> None:
        """Fence this run, preserving unresolved receipts, grants and all history.

        Ledger.close would abandon pending receipts and could evict evidence;
        deliberately leave that internal ledger intact behind this owner's fence.
        No service cancellation, physical mutation or origin restart is implied.
        """
        with self._gate:
            if self._sampling or self._capturing:
                raise RuntimeError("host clock callbacks must not reenter ingress")
            self._closed = True
            self._clock.close()
            self._book.close()
