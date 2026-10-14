"""Bounded supervisor-only host receipts, not QPU time or job admission.

This ledger records arrival order; it does not enforce scheduler order. Request
bodies and credentials must remain outside this internal metadata boundary.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from threading import Lock
import time


def _identifier(value: str, name: str) -> None:
    # Reject arbitrary objects and subclasses rather than retaining input aliases.
    if type(value) is not str or not 1 <= len(value) <= 128 or not value.isascii():
        raise ValueError(f"{name} must be a nonempty ASCII string of at most 128 characters")


def _principal(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("principal_id must be a nonempty string")
    # Preserve Unicode/long registered identifiers without retaining a subclass.
    plain = str.__str__(value)
    if not plain:
        raise ValueError("principal_id must be a nonempty string")
    return plain


@dataclass(frozen=True)
class ReceiptLimits:
    max_pending: int
    max_history: int

    def __post_init__(self) -> None:
        for value in (self.max_pending, self.max_history):
            if type(value) is not int or value < 1:
                raise ValueError("receipt limits must be positive exact integers")


class ReceiptOutcome(str, Enum):
    COMPLETED = "completed"
    REJECTED = "rejected"
    ABANDONED = "abandoned"


@dataclass(frozen=True)
class RequestReceipt:
    sequence: int
    received_at_ns: int
    principal_id: str
    operation_id: str
    clock_domain: str = "host_monotonic_ns"

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValueError("receipt sequence must be a positive exact integer")
        if type(self.received_at_ns) is not int or self.received_at_ns < 0:
            raise ValueError("receipt timestamp must be a nonnegative exact integer")
        object.__setattr__(self, "principal_id", _principal(self.principal_id))
        _identifier(self.operation_id, "operation_id")
        _identifier(self.clock_domain, "clock_domain")


@dataclass(frozen=True)
class ReceiptRecord:
    receipt: RequestReceipt
    outcome: ReceiptOutcome | None
    job_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.receipt) is not RequestReceipt:
            raise ValueError("record requires an immutable request receipt")
        if self.outcome is not None and not isinstance(self.outcome, ReceiptOutcome):
            raise ValueError("record outcome must be a ReceiptOutcome")
        if self.job_id is not None:
            _identifier(self.job_id, "job_id")


class ReceiptCapacityError(RuntimeError):
    """Host receipt capacity, not a simulated service failure or exercise budget."""


class RequestReceiptLedger:
    """Synchronize identity-bound pending tickets and finite completion history."""

    def __init__(
        self, limits: ReceiptLimits, *, clock: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if type(limits) is not ReceiptLimits:
            raise TypeError("explicit ReceiptLimits are required")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._limits = ReceiptLimits(limits.max_pending, limits.max_history)
        self._clock = clock
        self._pending: dict[int, RequestReceipt] = {}
        self._active: set[int] = set()
        self._history: deque[ReceiptRecord] = deque(maxlen=self._limits.max_history)
        self._next_sequence = 1
        self._last_received_at_ns: int | None = None
        self._closed = False
        self._lock = Lock()

    def reserve(self, principal_id: str, operation_id: str) -> RequestReceipt:
        with self._lock:
            if self._closed:
                raise RuntimeError("receipt ledger is closed")
            principal_id = _principal(principal_id)
            _identifier(operation_id, "operation_id")
            if len(self._pending) >= self._limits.max_pending:
                raise ReceiptCapacityError("pending receipt capacity reached")
            timestamp = self._clock()
            if type(timestamp) is not int or timestamp < 0:
                raise ValueError("clock must return a nonnegative exact integer")
            if self._last_received_at_ns is not None and timestamp < self._last_received_at_ns:
                raise ValueError("receipt clock moved backwards")
            receipt = RequestReceipt(self._next_sequence, timestamp, principal_id, operation_id)
            self._pending[receipt.sequence] = receipt
            self._next_sequence += 1
            self._last_received_at_ns = timestamp
            return receipt

    def validate(self, receipt: RequestReceipt, principal_id: str) -> None:
        with self._lock:
            self._validate_pending(receipt, principal_id)

    def claim(self, receipt: RequestReceipt, principal_id: str) -> None:
        """Activate once before a worker starts; cancellation cannot abandon it."""
        with self._lock:
            self._validate_pending(receipt, principal_id)
            if receipt.sequence in self._active:
                raise ValueError("receipt is already active")
            self._active.add(receipt.sequence)

    def finish(
        self, receipt: RequestReceipt, *, outcome: ReceiptOutcome, job_id: str | None = None,
    ) -> bool:
        with self._lock:
            if not isinstance(outcome, ReceiptOutcome):
                raise ValueError("finish requires a ReceiptOutcome")
            if job_id is not None:
                _identifier(job_id, "job_id")
            return self._finish_pending(receipt, outcome, job_id)

    def abandon(self, receipt: RequestReceipt) -> bool:
        with self._lock:
            if self._is_pending(receipt) and receipt.sequence in self._active:
                return False
            return self._finish_pending(receipt, ReceiptOutcome.ABANDONED)

    def records(self) -> tuple[ReceiptRecord, ...]:
        """Supervisor-only immutable snapshot; never a participant observation."""
        with self._lock:
            records = [*self._history]
            records.extend(ReceiptRecord(receipt, None) for receipt in self._pending.values())
            return tuple(sorted(records, key=lambda record: record.receipt.sequence))

    def forget_completed(self, receipt: RequestReceipt) -> bool:
        """Release exact completed read evidence after its bounded reply is cached."""
        with self._lock:
            if type(receipt) is not RequestReceipt or receipt.sequence in self._pending:
                raise ValueError("only an exact completed receipt can be forgotten")
            index = next((i for i, record in enumerate(self._history)
                          if record.receipt is receipt), None)
            if index is None:
                return False
            if self._history[index].outcome is None:
                raise ValueError("an unfinished receipt cannot be forgotten")
            retained = list(self._history)
            del retained[index]
            self._history = deque(retained, maxlen=self._limits.max_history)
            return True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for receipt in self._pending.values():
                self._history.append(ReceiptRecord(receipt, ReceiptOutcome.ABANDONED))
            self._pending.clear()
            self._active.clear()

    def _validate_pending(self, receipt: RequestReceipt, principal_id: str) -> None:
        # These private helpers require the ledger lock; do not nest public calls.
        if not self._is_pending(receipt):
            raise ValueError("receipt is not an outstanding ticket of this ledger")
        if receipt.principal_id != _principal(principal_id):
            raise PermissionError("receipt belongs to another principal")

    def _finish_pending(
        self, receipt: RequestReceipt, outcome: ReceiptOutcome, job_id: str | None = None,
    ) -> bool:
        if not self._is_pending(receipt):
            return False
        record = ReceiptRecord(receipt, outcome, job_id)
        del self._pending[receipt.sequence]
        self._active.discard(receipt.sequence)
        self._history.append(record)
        return True

    def _is_pending(self, receipt: RequestReceipt) -> bool:
        # Called only under the ledger lock. Equality would accept copied tickets.
        return type(receipt) is RequestReceipt and self._pending.get(receipt.sequence) is receipt
