"""Immutable commands and a deterministic single-QPU FIFO scheduler."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from collections import deque
from contextlib import contextmanager
from typing import Any, Callable
from uuid import uuid4
from functools import wraps
from threading import RLock
from weakref import WeakKeyDictionary

from core.attack_simulation.resource_binding import PhysicalQubitLayout, PhysicalQubitRef
from core.attack_simulation.circuit_program import CircuitExecutionOperation
from core.attack_simulation.request_receipts import RequestReceipt


class JobStatus(str, Enum):
    SUBMITTED = "submitted"
    VALIDATING = "validating"
    QUEUED = "queued"
    ALLOCATED = "allocated"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    INTERNAL_ERROR = "internal_error"


TERMINAL_JOB_STATUSES = frozenset({
    JobStatus.SUCCEEDED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
    JobStatus.REJECTED,
    JobStatus.INTERNAL_ERROR,
})


class JobRetentionExpiredError(KeyError):
    """The job existed, but its record was discarded by the retention bound.

    Carries only the owner scope, so callers can keep other tenants' jobs
    indistinguishable from unknown IDs.
    """

    def __init__(self, job_id: str, tenant_id: str, principal_id: str | None) -> None:
        super().__init__(f"job record expired: {job_id}")
        self.tenant_id, self.principal_id = tenant_id, principal_id


@dataclass(frozen=True)
class VictimPrepareOperation:
    output_bit: int

    @property
    def catalog_operation_id(self) -> str:
        return "program.submit"

    def __post_init__(self) -> None:
        if self.output_bit not in (0, 1):
            raise ValueError("output_bit must be zero or one")


@dataclass(frozen=True)
class AttackerMeasureOperation:
    """One physical observation sample; batching belongs to the orchestrator."""

    sampling_seed: int

    @property
    def catalog_operation_id(self) -> str:
        return "program.submit"

    def __post_init__(self) -> None:
        seed = int(self.sampling_seed)
        if seed < 0 or seed > 2 ** 32 - 1:
            raise ValueError("sampling_seed must be between zero and 2**32 - 1")
        object.__setattr__(self, "sampling_seed", seed)


JobOperation = VictimPrepareOperation | AttackerMeasureOperation | CircuitExecutionOperation


@dataclass(frozen=True)
class JobCommand:
    tenant_id: str
    operation: JobOperation
    idempotency_key: str
    scenario_manifest_id: str | None = None
    target: PhysicalQubitRef | None = None
    created_by_principal_id: str | None = None
    layout: PhysicalQubitLayout | None = None

    def __post_init__(self) -> None:
        _identifier(self.tenant_id, "tenant_id")
        _identifier(self.idempotency_key, "idempotency_key")
        if self.scenario_manifest_id is not None:
            _identifier(self.scenario_manifest_id, "scenario_manifest_id")
        if self.target is not None and not isinstance(self.target, PhysicalQubitRef):
            raise TypeError("target must be a PhysicalQubitRef")
        if self.layout is not None and not isinstance(self.layout, PhysicalQubitLayout):
            raise TypeError("layout must be a PhysicalQubitLayout")
        if self.created_by_principal_id is not None:
            _identifier(self.created_by_principal_id, "created_by_principal_id")
        if not isinstance(
            self.operation,
            (VictimPrepareOperation, AttackerMeasureOperation, CircuitExecutionOperation),
        ):
            raise TypeError("unsupported job operation")


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    acceptance_sequence: int
    command: JobCommand
    status: JobStatus
    status_history: tuple[JobStatus, ...]
    result: dict[str, Any] | None = None
    error_code: str | None = None
    ingress_receipt: RequestReceipt | None = None


@dataclass
class _MutableJob:
    job_id: str
    acceptance_sequence: int
    command: JobCommand
    status: JobStatus
    status_history: list[JobStatus]
    result: dict[str, Any] | None = None
    error_code: str | None = None
    ingress_receipt: RequestReceipt | None = None
    transition_version: int = 0


@dataclass(frozen=True, eq=False)
class PreparedJobSuccess:
    """Opaque supervisor capability for an exact RUNNING job generation."""

    job_id: str
    command: JobCommand
    expected_status: JobStatus
    transition_version: int
    _issuer: object = field(repr=False)


@dataclass(frozen=True)
class _PreparedSuccessData:
    job: _MutableJob
    result: dict[str, Any]
    returned_record: JobRecord


@dataclass(frozen=True)
class _CommittedJobSuccess:
    token: PreparedJobSuccess
    prepared: _PreparedSuccessData
    history_length: int
    result_before: dict[str, Any] | None
    error_before: str | None


@dataclass(frozen=True)
class _DeclaredStartCheckpoint:
    job: _MutableJob
    command: JobCommand
    history_length: int
    transition_version: int
    result_before: dict[str, Any] | None
    error_before: str | None
    queue_version: int
    queue_length: int
    next_head: str | None
    queue_index: int = 0


@dataclass(frozen=True)
class _DeclaredCancelCheckpoint:
    job: _MutableJob
    command: JobCommand
    history_length: int
    transition_version: int
    queue_version: int
    queue_length: int
    queue_index: int


def _synchronized(method):
    """Protect queue/record transitions, never hold this lock during physics."""
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return locked


class FifoJobScheduler:
    """Atomic FIFO bookkeeping for a single physical executor."""

    queue_policy_id = "on_demand_fifo_v1"
    cancellation_policy_id = "queued_only"

    def __init__(self) -> None:
        self._lock = RLock()
        self._next_sequence = 1
        self._queue: deque[str] = deque()
        self._jobs: dict[str, _MutableJob] = {}
        self._idempotency: dict[tuple[str, str | None, str], str] = {}
        # Discarded job ID -> owner scope. Their idempotency keys stay, so a
        # replay is refused instead of executing the job a second time.
        self._expired: dict[str, tuple[str, str | None]] = {}
        self._transaction_lock_bound = False
        self._success_issuer = object()
        self._prepared_successes: WeakKeyDictionary[PreparedJobSuccess, _PreparedSuccessData] = WeakKeyDictionary()
        self._rollback_success: _CommittedJobSuccess | None = None
        self._queue_version = 0
        self._rollback_start: _DeclaredStartCheckpoint | None = None
        self._starting_declared: _DeclaredStartCheckpoint | None = None
        self._rollback_cancel: _DeclaredCancelCheckpoint | None = None
        self._cancelling_declared = False
        self._lane_of: Callable[[JobCommand], object] | None = None

    def bind_lanes(self, lane_of: Callable[[JobCommand], object]) -> None:
        """Trusted assembly: FIFO is kept per lane, so one lane's wait never blocks another.

        Acceptance order stays one global sequence; only the START rule changes.
        Without lanes the whole queue is one lane (the original single FIFO).
        """
        with self._lock:
            if self._lane_of is not None or self._jobs:
                raise RuntimeError("lanes bind once on a fresh scheduler")
            self._lane_of = lane_of

    def _lane_head(self, job_id: str) -> bool:
        if self._lane_of is None:
            return bool(self._queue) and self._queue[0] == job_id
        lane = self._lane_of(self._jobs[job_id].command)
        for queued in self._queue:
            if self._lane_of(self._jobs[queued].command) == lane:
                return queued == job_id
        return False

    def _retire_rollback_journals(self) -> None:
        self._rollback_success = None
        if self._starting_declared is None:
            self._rollback_start = None
        if not self._cancelling_declared:
            self._rollback_cancel = None

    def _cancel_declared(self, job_id: str) -> JobRecord:
        """Bounded journal around queued cancellation in a shared transaction."""
        with self._lock:
            job = self._job(job_id)
            if job.status is not JobStatus.QUEUED or self._cancelling_declared:
                raise ValueError("declared cancellation requires a queued common job")
            checkpoint = _DeclaredCancelCheckpoint(job, job.command, len(job.status_history),
                job.transition_version, self._queue_version, len(self._queue), self._queue.index(job_id))
            self._rollback_cancel = checkpoint
            self._cancelling_declared = True
            try:
                return self.cancel(job_id)
            except BaseException:
                self._rollback_declared_cancel(job_id)
                raise
            finally:
                self._cancelling_declared = False

    def _rollback_declared_cancel(self, job_id: str) -> None:
        """Undo the latest unpublished suffix, never a published cancellation."""
        with self._lock:
            checkpoint = self._rollback_cancel
            job = self._jobs.get(job_id)
            if checkpoint is None or job is not checkpoint.job or job.command is not checkpoint.command:
                raise ValueError("no exact current cancellation checkpoint")
            suffix = int(job.status is JobStatus.CANCELLED)
            if (job.status not in {JobStatus.QUEUED, JobStatus.CANCELLED}
                    or job.transition_version != checkpoint.transition_version + suffix
                    or len(job.status_history) != checkpoint.history_length + suffix):
                raise ValueError("cancellation generation changed")
            if (self._queue_version == checkpoint.queue_version
                    and len(self._queue) == checkpoint.queue_length
                    and self._queue[checkpoint.queue_index] == job_id and not suffix):
                self._rollback_cancel = None
                return
            if (self._queue_version != checkpoint.queue_version + 1
                    or len(self._queue) != checkpoint.queue_length - 1 or job_id in self._queue):
                raise ValueError("cancellation queue generation changed")
            self._queue.insert(checkpoint.queue_index, job_id)
            self._queue_version = checkpoint.queue_version
            del job.status_history[checkpoint.history_length:]
            job.status = JobStatus.QUEUED
            job.transition_version = checkpoint.transition_version
            self._rollback_cancel = None

    def _start_declared(self, job_id: str) -> JobRecord:
        """Supervisor START: checkpoint before allocation, then return RUNNING.

        The caller holds the shared gate through the matching frontier callback
        and any compensation. This journal survives failure after either normal
        allocation/running mutation, retaining only one job and queue counters.
        """
        with self._lock:
            job = self._job(job_id)
            if (type(job_id) is not str or job.status is not JobStatus.QUEUED
                    or not self._lane_head(job_id)):
                raise ValueError("declared START requires the exact QUEUED FIFO head")
            index = self._queue.index(job_id)
            if self._starting_declared is not None:
                raise RuntimeError("a declared START is already active")
            checkpoint = _DeclaredStartCheckpoint(
                job, job.command, len(job.status_history), job.transition_version,
                job.result, job.error_code, self._queue_version, len(self._queue),
                self._queue[index + 1] if index + 1 < len(self._queue) else None,
                index,
            )
            self._rollback_success = None
            self._rollback_start = self._starting_declared = checkpoint
            try:
                allocated = self.allocate_next() if index == 0 else self.allocate_job(job_id)
                if allocated is None or allocated.job_id != job_id:
                    raise RuntimeError("declared START FIFO head changed")
                return self.mark_running(job_id)
            finally:
                self._starting_declared = None

    def _rollback_declared_start(self, job_id: str) -> bool:
        """Compensate the exact latest unpublished START, without copying FIFO.

        Return False only for a still-QUEUED exact head, True when allocation or
        RUNNING is reverted. Subsequent queue/status mutations retire the slot.
        """
        with self._lock:
            if type(job_id) is not str:
                raise ValueError("declared START rollback requires an owned job ID")
            job = self._jobs.get(job_id)
            checkpoint = self._rollback_start
            if checkpoint is None:
                if (job is not None and job.status is JobStatus.QUEUED
                        and self._lane_head(job_id)):
                    return False
                raise ValueError("no current declared START for this job")
            if (job is not checkpoint.job or job.command is not checkpoint.command
                    or job.result is not checkpoint.result_before
                    or job.error_code != checkpoint.error_before):
                raise ValueError("declared START job is no longer current")
            if (job.status is JobStatus.QUEUED
                    and job.transition_version == checkpoint.transition_version
                    and len(job.status_history) == checkpoint.history_length
                    and self._queue_version == checkpoint.queue_version
                    and len(self._queue) == checkpoint.queue_length
                    and checkpoint.queue_index < len(self._queue)
                    and self._queue[checkpoint.queue_index] == job_id):
                return False
            suffix = {
                JobStatus.QUEUED: (),
                JobStatus.ALLOCATED: (JobStatus.ALLOCATED,),
                JobStatus.RUNNING: (JobStatus.ALLOCATED, JobStatus.RUNNING),
            }.get(job.status)
            if (suffix is None
                    or job.transition_version != checkpoint.transition_version + len(suffix)
                    or len(job.status_history) != checkpoint.history_length + len(suffix)
                    or any(job.status_history[checkpoint.history_length + index] is not status
                           for index, status in enumerate(suffix))
                    or self._queue_version != checkpoint.queue_version + 1
                    or len(self._queue) != checkpoint.queue_length - 1
                    or (self._queue[checkpoint.queue_index]
                        if checkpoint.queue_index < len(self._queue) else None) != checkpoint.next_head):
                raise ValueError("declared START queue or generation is no longer current")
            self._queue.insert(checkpoint.queue_index, job_id)
            self._queue_version = checkpoint.queue_version
            del job.status_history[checkpoint.history_length:]
            job.status = JobStatus.QUEUED
            job.transition_version = checkpoint.transition_version
            job.result = checkpoint.result_before
            job.error_code = checkpoint.error_before
            self._rollback_start = None
            self._rollback_success = None
            return True

    def bind_transaction_lock(self, lock: object) -> None:
        """Trusted assembly only, once before publishing or submitting any jobs."""
        if type(lock) is not type(RLock()):
            raise TypeError("transaction lock must be an RLock")
        with self._lock:
            if (self._transaction_lock_bound or self._jobs or self._queue
                    or self._idempotency or self._next_sequence != 1):
                raise RuntimeError("transaction lock can bind only once on a fresh scheduler")
            self._transaction_lock_bound = True
            self._lock = lock

    def prepare_success(self, job_id: str, result: dict[str, Any]) -> PreparedJobSuccess:
        """Detach the result outside the shared lock, without changing job state.

        The coordinator has already allocated/started this job atomically with
        the frontier START. Its execution/result work cap belongs to that caller;
        dictionary/deepcopy semantics match the existing success path.
        """
        with self._lock:
            job = self._job(job_id)
            if job.status is not JobStatus.RUNNING:
                raise ValueError("prepared success requires a RUNNING job")
            token = PreparedJobSuccess(job_id, job.command, job.status,
                                       job.transition_version, self._success_issuer)
            history = tuple(job.status_history)
            sequence, receipt, error_code = job.acceptance_sequence, job.ingress_receipt, job.error_code
        detached_result = deepcopy(dict(result))
        # Prepare the returned copy too: publication performs no heavy copy and
        # a caller mutating its returned record cannot mutate the scheduler.
        returned = JobRecord(job_id, sequence, token.command, JobStatus.SUCCEEDED,
                             history + (JobStatus.SUCCEEDED,), deepcopy(detached_result),
                             error_code=error_code, ingress_receipt=receipt)
        with self._lock:
            if (self._jobs.get(job_id) is not job or job.command is not token.command
                    or job.status is not token.expected_status
                    or job.transition_version != token.transition_version):
                raise ValueError("job changed during success preparation")
            self._prepared_successes[token] = _PreparedSuccessData(job, detached_result, returned)
        return token

    def validate_prepared(self, token: PreparedJobSuccess) -> None:
        """Recheck the issued capability before any coordinated store mutation.

        The coordinator retains the shared RLock through all commits. Numerical
        failure leaves the job RUNNING for explicit supervisor recovery.
        """
        with self._lock:
            if type(token) is not PreparedJobSuccess or token._issuer is not self._success_issuer:
                raise ValueError("unissued or consumed job success token")
            prepared = self._prepared_successes.get(token)
            if prepared is None:
                raise ValueError("unissued or consumed job success token")
            job = self._jobs.get(token.job_id)
            if (job is not prepared.job or job.command is not token.command
                    or job.status is not token.expected_status
                    or job.status is not JobStatus.RUNNING
                    or job.transition_version != token.transition_version):
                raise ValueError("prepared success job is no longer current")

    def commit_prepared_success(self, token: PreparedJobSuccess) -> JobRecord:
        """Complete an already RUNNING job; no allocation or result copying."""
        with self._lock:
            self.validate_prepared(token)
            prepared = self._prepared_successes[token]
            rollback = _CommittedJobSuccess(token, prepared, len(prepared.job.status_history),
                                            prepared.job.result, prepared.job.error_code)
            self._transition(prepared.job, JobStatus.SUCCEEDED)
            prepared.job.result = prepared.result
            del self._prepared_successes[token]
            self._rollback_success = rollback
            return prepared.returned_record

    def _rollback_prepared_success(self, token: PreparedJobSuccess) -> bool:
        """Undo the latest unpublished success while the supervisor holds its gate.

        Restore the exact RUNNING generation and staged data so the same token
        can retry. Every intervening scheduler mutation retires this one slot.
        The result prestate is retained by reference, never copied under lock.
        Return False for an exact still-pending token, True when reverted.
        """
        with self._lock:
            if (type(token) is PreparedJobSuccess and token._issuer is self._success_issuer
                    and token in self._prepared_successes):
                self.validate_prepared(token)
                return False
            rollback = self._rollback_success
            if (type(token) is not PreparedJobSuccess or token._issuer is not self._success_issuer
                    or rollback is None or rollback.token is not token):
                raise ValueError("job rollback requires the latest committed issued token")
            prepared = rollback.prepared
            job = self._jobs.get(token.job_id)
            if (job is not prepared.job or job.command is not token.command
                    or job.status is not JobStatus.SUCCEEDED
                    or job.transition_version != token.transition_version + 1
                    or len(job.status_history) != rollback.history_length + 1
                    or job.status_history[-1] is not JobStatus.SUCCEEDED
                    or job.result is not prepared.result
                    or job.error_code != rollback.error_before):
                raise ValueError("job rollback state is no longer current")
            del job.status_history[rollback.history_length:]
            job.status = token.expected_status
            job.transition_version = token.transition_version
            job.result = rollback.result_before
            job.error_code = rollback.error_before
            self._prepared_successes[token] = prepared
            self._rollback_success = None
            return True

    @contextmanager
    def admission_transaction(self):
        """Trusted service boundary for short, atomic acceptance bookkeeping.

        Use the same lock as queue admission (and coordinated world commits).
        Compilation and physical execution must remain outside this boundary.
        """
        with self._lock:
            yield

    @_synchronized
    def submit(
        self, command: JobCommand, *, receipt: RequestReceipt | None = None,
        before_accept: Callable[[], None] | None = None,
    ) -> JobRecord:
        # Supervisor metadata is separate from command equality/idempotency.
        # Validate even on replay, but never replace the first arrival record.
        if receipt is not None:
            if type(receipt) is not RequestReceipt:
                raise TypeError("receipt must be an immutable RequestReceipt")
            if receipt.principal_id != command.created_by_principal_id:
                raise PermissionError("receipt belongs to a different principal")
        idempotency_scope = (
            command.tenant_id,
            command.created_by_principal_id,
            command.idempotency_key,
        )
        existing_id = self._idempotency.get(idempotency_scope)
        if existing_id is not None:
            existing = self._job(existing_id)
            if existing.command != command:
                raise ValueError("idempotency key was reused for a different command")
            return _snapshot(existing)

        # Trusted accounting runs only for a new, valid acceptance. A refusal
        # leaves the queue and its idempotency/rollback records unchanged.
        if before_accept is not None:
            before_accept()
        self._retire_rollback_journals()
        sequence = self._next_sequence
        self._next_sequence += 1
        # Acceptance order is private scheduler metadata, not a public counter
        # of other tenants' activity.
        job_id = f"job-{uuid4().hex}"
        job = _MutableJob(
            job_id=job_id,
            acceptance_sequence=sequence,
            command=command,
            status=JobStatus.SUBMITTED,
            status_history=[JobStatus.SUBMITTED],
            ingress_receipt=receipt,
        )
        self._jobs[job_id] = job
        self._idempotency[idempotency_scope] = job_id
        self._transition(job, JobStatus.VALIDATING)
        self._transition(job, JobStatus.QUEUED)
        self._queue.append(job_id)
        self._queue_version += 1
        return _snapshot(job)

    @_synchronized
    def get(self, job_id: str) -> JobRecord:
        return _snapshot(self._job(job_id))

    def _forget_terminal(self, job_id: str) -> None:
        """Trusted retention: drop a terminal record, keeping its idempotency key.

        The caller proves the result was released and no evidence still refers
        to the job. Open rollback journals or a staged success keep the record.
        """
        with self._lock:
            job = self._job(job_id)
            if job.status not in TERMINAL_JOB_STATUSES or job_id in self._queue:
                raise ValueError("only a terminal job record can be discarded")
            journals = (self._rollback_success.prepared.job if self._rollback_success else None,
                        self._rollback_start.job if self._rollback_start else None,
                        self._starting_declared.job if self._starting_declared else None,
                        self._rollback_cancel.job if self._rollback_cancel else None)
            if any(entry is job for entry in journals) or any(
                    data.job is job for data in self._prepared_successes.values()):
                raise ValueError("a job with an open rollback journal cannot be discarded")
            del self._jobs[job_id]
            self._expired[job_id] = (job.command.tenant_id, job.command.created_by_principal_id)

    @_synchronized
    def cancel(self, job_id: str) -> JobRecord:
        job = self._job(job_id)
        if job.status is not JobStatus.QUEUED:
            raise ValueError("the queued_only policy cannot cancel this job state")
        self._retire_rollback_journals()
        self._queue.remove(job_id)
        self._queue_version += 1
        self._transition(job, JobStatus.CANCELLED)
        return _snapshot(job)

    @_synchronized
    def allocate_next(self) -> JobRecord | None:
        if not self._queue:
            return None
        self._retire_rollback_journals()
        job = self._jobs[self._queue.popleft()]
        self._queue_version += 1
        self._transition(job, JobStatus.ALLOCATED)
        return _snapshot(job)

    @_synchronized
    def allocate_job(self, job_id: str) -> JobRecord:
        self._retire_rollback_journals()
        self._queue.remove(job_id)
        job = self._jobs[job_id]
        self._queue_version += 1
        self._transition(job, JobStatus.ALLOCATED)
        return _snapshot(job)

    @_synchronized
    def mark_running(self, job_id: str) -> JobRecord:
        job = self._job(job_id)
        self._transition(job, JobStatus.RUNNING)
        return _snapshot(job)

    @_synchronized
    def mark_succeeded(self, job_id: str, result: dict[str, Any]) -> JobRecord:
        job = self._job(job_id)
        self._retire_rollback_journals()
        job.result = deepcopy(dict(result))
        self._transition(job, JobStatus.SUCCEEDED)
        return _snapshot(job)

    @_synchronized
    def mark_internal_error(self, job_id: str, error_code: str) -> JobRecord:
        job = self._job(job_id)
        self._retire_rollback_journals()
        job.error_code = _identifier(error_code, "error_code")
        self._transition(job, JobStatus.INTERNAL_ERROR)
        return _snapshot(job)

    @_synchronized
    def queued_job_ids(self) -> tuple[str, ...]:
        return tuple(self._queue)

    def _job(self, job_id: str) -> _MutableJob:
        try:
            return self._jobs[job_id]
        except KeyError as error:
            scope = self._expired.get(job_id)
            if scope is not None:
                raise JobRetentionExpiredError(job_id, *scope) from None
            raise KeyError(f"unknown job: {job_id}") from error

    def _transition(self, job: _MutableJob, next_status: JobStatus) -> None:
        allowed = {
            JobStatus.SUBMITTED: {JobStatus.VALIDATING},
            JobStatus.VALIDATING: {JobStatus.QUEUED, JobStatus.REJECTED},
            JobStatus.QUEUED: {JobStatus.ALLOCATED, JobStatus.CANCELLED},
            JobStatus.ALLOCATED: {JobStatus.RUNNING, JobStatus.CANCELLED},
            JobStatus.RUNNING: {
                JobStatus.SUCCEEDED,
                JobStatus.FAILED,
                JobStatus.CANCEL_REQUESTED,
                JobStatus.INTERNAL_ERROR,
            },
            JobStatus.CANCEL_REQUESTED: {
                JobStatus.SUCCEEDED,
                JobStatus.CANCELLED,
                JobStatus.FAILED,
                JobStatus.INTERNAL_ERROR,
            },
        }
        if next_status not in allowed.get(job.status, set()):
            raise ValueError(f"invalid job transition: {job.status} -> {next_status}")
        job.status = next_status
        job.status_history.append(next_status)
        job.transition_version += 1
        self._retire_rollback_journals()


def _snapshot(job: _MutableJob) -> JobRecord:
    return JobRecord(
        job_id=job.job_id,
        acceptance_sequence=job.acceptance_sequence,
        command=job.command,
        status=job.status,
        status_history=tuple(job.status_history),
        result=None if job.result is None else deepcopy(job.result),
        error_code=job.error_code,
        ingress_receipt=job.ingress_receipt,
    )


def _identifier(value: str, name: str) -> str:
    converted = str(value).strip()
    if not converted:
        raise ValueError(f"{name} must not be empty")
    return converted
