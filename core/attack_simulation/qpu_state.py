"""Revisioned in-memory state authority for the shared-QPU runtime."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
from threading import RLock
from weakref import WeakValueDictionary

import numpy as np

from core.gates import Matrix
from core.attack_simulation.qpu_topology import RetainedRegionBinding

MAX_DENSE_LOCAL_QUBITS = 10  # 16 MiB raw complex128 matrix, excluding copies/tuple overhead.


class RevisionConflictError(RuntimeError):
    """Raised when a transition tries to commit from a stale revision."""


@dataclass(frozen=True)
class QpuStateSnapshot:
    """The sole authoritative dynamic state of one local QPU region."""

    schema_version: str
    qpu_id: str
    revision: int
    device_time_us: float
    physical_qubit_ids: tuple[int, ...]
    local_dimension: int
    density_matrix: Matrix
    device_config_version: str
    owner_tenant_id: str | None = None
    last_event_id: str | None = None
    last_job_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.schema_version, "schema_version")
        _identifier(self.qpu_id, "qpu_id")
        _identifier(self.device_config_version, "device_config_version")
        if int(self.revision) < 0:
            raise ValueError("revision must be non-negative")
        object.__setattr__(self, "revision", int(self.revision))
        device_time = float(self.device_time_us)
        if not math.isfinite(device_time) or device_time < 0.0:
            raise ValueError("device_time_us must be finite and non-negative")
        object.__setattr__(self, "device_time_us", device_time)
        qubits = tuple(int(value) for value in self.physical_qubit_ids)
        if not qubits or len(set(qubits)) != len(qubits) or any(value < 0 for value in qubits):
            raise ValueError("physical_qubit_ids must be unique and non-negative")
        object.__setattr__(self, "physical_qubit_ids", qubits)
        dimension = int(self.local_dimension)
        if dimension not in (2, 3):
            raise ValueError("local_dimension must be two or three")
        object.__setattr__(self, "local_dimension", dimension)
        matrix = _immutable_density_matrix(self.density_matrix)
        _validate_density_matrix(matrix, dimension ** len(qubits))
        object.__setattr__(self, "density_matrix", matrix)
        if self.owner_tenant_id is not None:
            _identifier(self.owner_tenant_id, "owner_tenant_id")
        if self.last_event_id is not None:
            _identifier(self.last_event_id, "last_event_id")
        if self.last_job_id is not None:
            _identifier(self.last_job_id, "last_job_id")


@dataclass(frozen=True)
class QpuStateTransition:
    """One atomic physical event to commit against an expected revision."""

    event_id: str
    event_kind: str
    job_id: str
    next_density_matrix: Matrix
    duration_us: float
    next_owner_tenant_id: str | None
    scenario_manifest_id: str | None = None
    next_physical_qubit_ids: tuple[int, ...] | None = None

    def __post_init__(self) -> None:
        _identifier(self.event_id, "event_id")
        object.__setattr__(self, "next_density_matrix", _immutable_density_matrix(self.next_density_matrix))
        _identifier(self.event_kind, "event_kind")
        _identifier(self.job_id, "job_id")
        duration = float(self.duration_us)
        if not math.isfinite(duration) or duration < 0.0:
            raise ValueError("duration_us must be finite and non-negative")
        object.__setattr__(self, "duration_us", duration)
        if self.next_owner_tenant_id is not None:
            _identifier(self.next_owner_tenant_id, "next_owner_tenant_id")
        if self.scenario_manifest_id is not None:
            _identifier(self.scenario_manifest_id, "scenario_manifest_id")
        if self.next_physical_qubit_ids is not None:
            qubits = tuple(self.next_physical_qubit_ids)
            if (len(qubits) == 0 or len(set(qubits)) != len(qubits)
                    or any(type(value) is not int or value < 0 for value in qubits)):
                raise ValueError("next_physical_qubit_ids must be unique non-negative integers")
            object.__setattr__(self, "next_physical_qubit_ids", qubits)


@dataclass(frozen=True)
class QpuEventRecord:
    event_id: str
    event_kind: str
    job_id: str
    revision_before: int
    revision_after: int
    device_time_start_us: float
    device_time_end_us: float
    owner_before: str | None
    owner_after: str | None
    scenario_manifest_id: str | None = None
    physical_qubit_ids_before: tuple[int, ...] = ()
    physical_qubit_ids_after: tuple[int, ...] = ()


@dataclass(frozen=True)
class InactiveRegionState:
    """One disjoint retained register, stored without a joint dense matrix."""

    physical_qubit_ids: tuple[int, ...]
    density_matrix: Matrix
    owner_tenant_id: str | None
    updated_at_us: float

    def __post_init__(self) -> None:
        ids = self.physical_qubit_ids
        if (type(ids) is not tuple or not ids or len(set(ids)) != len(ids)
                or any(type(qid) is not int or qid < 0 for qid in ids)):
            raise ValueError("inactive region requires unique physical qubit IDs")
        matrix = _immutable_density_matrix(self.density_matrix)
        _validate_density_matrix(matrix, 2 ** len(ids))
        object.__setattr__(self, "density_matrix", matrix)
        if self.owner_tenant_id is not None:
            _identifier(self.owner_tenant_id, "owner_tenant_id")
        if (type(self.updated_at_us) not in (int, float)
                or not math.isfinite(self.updated_at_us) or self.updated_at_us < 0):
            raise ValueError("inactive region update time must be finite and non-negative")


@dataclass(frozen=True, eq=False)
class PreparedQpuBatch:
    """Supervisor capability issued only after detached numerical validation."""

    expected_snapshot: QpuStateSnapshot
    final_snapshot: QpuStateSnapshot
    events: tuple[QpuEventRecord, ...]
    _issuer: object = field(repr=False)
    expected_inactive_region: InactiveRegionState | None = None
    final_inactive_region: InactiveRegionState | None = None


@dataclass(frozen=True)
class _CommittedQpuBatch:
    token: PreparedQpuBatch
    history_start: int


class QpuStateStore:
    """Thread-safe single-writer authority with optimistic revision checks.

    This implementation is intentionally in-memory.  Snapshot persistence and
    restart restoration remain a separate adapter contract.
    """

    def __init__(self, initial_snapshot: QpuStateSnapshot, *, region_binding: RetainedRegionBinding | None = None,
                 alternate_region_binding: RetainedRegionBinding | None = None,
                 inactive_region: InactiveRegionState | None = None) -> None:
        if region_binding is not None:
            if type(region_binding) is not RetainedRegionBinding:
                raise TypeError("region_binding must be a RetainedRegionBinding")
        self._region_binding = region_binding
        if (alternate_region_binding is None) != (inactive_region is None):
            raise ValueError("alternate region binding and state must be supplied together")
        if alternate_region_binding is not None:
            if (region_binding is None or type(alternate_region_binding) is not RetainedRegionBinding
                    or alternate_region_binding.topology != region_binding.topology
                    or len(alternate_region_binding.physical_qubit_ids) != len(region_binding.physical_qubit_ids)
                    or set(alternate_region_binding.physical_qubit_ids) & set(region_binding.physical_qubit_ids)
                    or {initial_snapshot.physical_qubit_ids, inactive_region.physical_qubit_ids}
                    != {region_binding.physical_qubit_ids, alternate_region_binding.physical_qubit_ids}
                    or inactive_region.updated_at_us > initial_snapshot.device_time_us):
                raise ValueError("two disjoint equally sized retained regions are required")
        self._alternate_region_binding = alternate_region_binding
        self._inactive_region = inactive_region
        if region_binding is not None:
            self._binding_for(initial_snapshot.physical_qubit_ids).validate_snapshot(initial_snapshot)
        self._snapshot = initial_snapshot
        self._events: list[QpuEventRecord] = []
        self._event_ids: set[str] = set()
        self._lock = RLock()
        self._transaction_lock_bound = False
        self._batch_issuer = object()
        self._prepared_batches: WeakValueDictionary[int, PreparedQpuBatch] = WeakValueDictionary()
        self._rollback_batch: _CommittedQpuBatch | None = None

    def bind_transaction_lock(self, lock: object) -> None:
        """Trusted assembly only: bind one shared RLock before any state history.

        Assembly must finish before the store is published to readers/writers.
        """
        if type(lock) is not type(RLock()):
            raise TypeError("transaction lock must be an RLock")
        with self._lock:
            if (self._transaction_lock_bound or self._events or self._prepared_batches
                    or self._rollback_batch is not None
                    or self._snapshot.revision != 0
                    or self._snapshot.last_event_id is not None
                    or self._snapshot.last_job_id is not None):
                raise RuntimeError("transaction lock can bind only once on a fresh store")
            self._transaction_lock_bound = True
            self._lock = lock

    def prepare_batch(
        self,
        expected_snapshot: QpuStateSnapshot,
        final_snapshot: QpuStateSnapshot,
        events: tuple[QpuEventRecord, ...],
        max_events: int,
        final_inactive_region: InactiveRegionState | None = None,
    ) -> PreparedQpuBatch:
        """Validate a detached execution, leaving the authoritative store intact.

        Call outside the transaction lock; density construction/PSD checks are
        numerical work. The caller supplies the execution's bounded event cap.
        """
        if type(max_events) is not int or max_events < 1:
            raise ValueError("max_events must be a positive integer")
        if type(events) is not tuple or len(events) > max_events:
            raise ValueError("events must be a bounded tuple")
        if type(expected_snapshot) is not QpuStateSnapshot or type(final_snapshot) is not QpuStateSnapshot:
            raise TypeError("batch snapshots must be QpuStateSnapshot values")
        with self._lock:
            if self._snapshot is not expected_snapshot:
                raise RevisionConflictError("batch expected snapshot is not current")

        # Reconstruct rather than trusting a supplied dataclass's validation.
        # Neither this eigensolve nor event-chain validation holds the live lock.
        metadata = ("schema_version", "qpu_id", "local_dimension", "device_config_version")
        if any(getattr(final_snapshot, name) != getattr(expected_snapshot, name) for name in metadata):
            raise ValueError("batch cannot change QPU metadata or topology")
        if self._inactive_region is None and final_inactive_region is not None:
            raise ValueError("a single-region store cannot acquire an inactive region")
        if self._inactive_region is not None:
            if (type(final_inactive_region) is not InactiveRegionState
                    or final_inactive_region.updated_at_us > final_snapshot.device_time_us):
                raise ValueError("regional batch needs a complete inactive state")
            allowed = {self._region_binding.physical_qubit_ids,
                       self._alternate_region_binding.physical_qubit_ids}
            if ({final_snapshot.physical_qubit_ids, final_inactive_region.physical_qubit_ids} != allowed):
                raise ValueError("regional batch must retain exactly the declared regions")
        elif final_snapshot.physical_qubit_ids != expected_snapshot.physical_qubit_ids:
            raise ValueError("batch cannot change the fixed physical region")
        final = replace(final_snapshot)
        binding = self._binding_for(final.physical_qubit_ids)
        if binding is not None:
            binding.validate_snapshot(final)

        revision = expected_snapshot.revision
        time_us = expected_snapshot.device_time_us
        owner = expected_snapshot.owner_tenant_id
        physical_ids = expected_snapshot.physical_qubit_ids
        parked_ids = self._inactive_region.physical_qubit_ids if self._inactive_region else None
        parked_owner = self._inactive_region.owner_tenant_id if self._inactive_region else None
        parked_time = self._inactive_region.updated_at_us if self._inactive_region else None
        seen: set[str] = set()
        records: list[QpuEventRecord] = []
        for event in events:
            if type(event) is not QpuEventRecord:
                raise TypeError("events must contain QpuEventRecord values")
            for name in ("event_id", "event_kind", "job_id"):
                _batch_identifier(getattr(event, name), name)
            for name in ("owner_before", "owner_after", "scenario_manifest_id"):
                value = getattr(event, name)
                if value is not None:
                    _batch_identifier(value, name)
            if event.event_id in seen:
                raise ValueError("batch event IDs must be unique")
            seen.add(event.event_id)
            if (type(event.revision_before) is not int or type(event.revision_after) is not int
                    or event.revision_before != revision or event.revision_after != revision + 1):
                raise ValueError("batch revisions must be contiguous")
            for value in (event.device_time_start_us, event.device_time_end_us):
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise ValueError("batch event times must be finite and non-negative")
            if event.device_time_start_us != time_us or event.device_time_end_us < time_us:
                raise ValueError("batch event durations must form a contiguous chain")
            if event.owner_before != owner:
                raise ValueError("batch owner chain does not match the snapshot")
            if (type(event.physical_qubit_ids_before) is not tuple
                    or type(event.physical_qubit_ids_after) is not tuple
                    or any(type(q) is not int for q in event.physical_qubit_ids_before + event.physical_qubit_ids_after)
                    or event.physical_qubit_ids_before != physical_ids
                    or (event.physical_qubit_ids_after != physical_ids
                        and (self._inactive_region is None
                             or event.event_kind != "tenant_region_assignment"
                             or event.physical_qubit_ids_after not in allowed))):
                raise ValueError("batch event topology does not match the snapshot")
            if event.physical_qubit_ids_after != physical_ids:
                if (event.physical_qubit_ids_after != parked_ids
                        or event.owner_after != parked_owner):
                    raise ValueError("batch regional assignment must restore the parked owner")
                parked_ids, parked_owner, parked_time = physical_ids, owner, event.device_time_start_us
            records.append(replace(event))
            revision, time_us, owner = event.revision_after, event.device_time_end_us, event.owner_after
            physical_ids = event.physical_qubit_ids_after
        if (final.revision != revision or final.device_time_us != time_us
                or final.owner_tenant_id != owner or final.physical_qubit_ids != physical_ids):
            raise ValueError("batch event chain does not match the final snapshot")
        if self._inactive_region is not None and (
                final_inactive_region.physical_qubit_ids != parked_ids
                or final_inactive_region.owner_tenant_id != parked_owner
                or final_inactive_region.updated_at_us != parked_time):
            raise ValueError("batch parked-region history does not match its event chain")
        if records:
            if final.last_event_id != records[-1].event_id or final.last_job_id != records[-1].job_id:
                raise ValueError("batch last event/job does not match the final snapshot")
        elif final != expected_snapshot or final_inactive_region != self._inactive_region:
            raise ValueError("an empty batch cannot change the state")
        else:
            final = expected_snapshot

        token = PreparedQpuBatch(expected_snapshot, final, tuple(records), self._batch_issuer,
                                 self._inactive_region, final_inactive_region)
        with self._lock:
            if self._snapshot is not expected_snapshot:
                raise RevisionConflictError("batch expected snapshot is no longer current")
            if seen.intersection(self._event_ids):
                raise ValueError("batch event_id already committed")
            self._prepared_batches[id(token)] = token
        return token

    def validate_prepared(self, token: PreparedQpuBatch) -> None:
        """Precheck under the shared lock before *any* coordinated mutation.

        The coordinator must retain that lock through every participant commit.
        """
        with self._lock:
            if (type(token) is not PreparedQpuBatch or token._issuer is not self._batch_issuer
                    or self._prepared_batches.get(id(token)) is not token):
                raise ValueError("unissued or consumed QPU batch token")
            if self._snapshot is not token.expected_snapshot:
                raise RevisionConflictError("batch expected snapshot is no longer current")
            if any(event.event_id in self._event_ids for event in token.events):
                raise ValueError("batch event_id already committed")

    def commit_prepared(self, token: PreparedQpuBatch) -> QpuStateSnapshot:
        """Publish already validated state with only O(batch history) bookkeeping."""
        with self._lock:
            self.validate_prepared(token)
            rollback = _CommittedQpuBatch(token, len(self._events))
            self._events.extend(token.events)
            self._event_ids.update(event.event_id for event in token.events)
            self._snapshot = token.final_snapshot
            self._inactive_region = token.final_inactive_region
            del self._prepared_batches[id(token)]
            self._rollback_batch = rollback
            return self._snapshot

    def _rollback_prepared_commit(self, token: PreparedQpuBatch) -> bool:
        """Undo only the latest unpublished commit under the supervisor's gate.

        The coordinator must retain the shared lock from prevalidation through
        compensation, before frontier confirmation/readers can observe state.
        Only one bounded batch journal is retained; ordinary commits retire it.
        Return False for an exact still-pending token, True when reverted.
        """
        with self._lock:
            if (type(token) is PreparedQpuBatch and token._issuer is self._batch_issuer
                    and self._prepared_batches.get(id(token)) is token):
                self.validate_prepared(token)
                return False
            rollback = self._rollback_batch
            if (type(token) is not PreparedQpuBatch or token._issuer is not self._batch_issuer
                    or rollback is None or rollback.token is not token):
                raise ValueError("QPU rollback requires the latest committed issued token")
            start = rollback.history_start
            if (self._snapshot is not token.final_snapshot
                    or len(self._events) != start + len(token.events)
                    or any(self._events[start + index] is not event
                           or event.event_id not in self._event_ids
                           for index, event in enumerate(token.events))):
                raise RevisionConflictError("QPU rollback state is no longer current")
            del self._events[start:]
            self._event_ids.difference_update(event.event_id for event in token.events)
            self._snapshot = token.expected_snapshot
            self._inactive_region = token.expected_inactive_region
            self._prepared_batches[id(token)] = token
            self._rollback_batch = None
            return True

    @property
    def region_binding(self) -> RetainedRegionBinding | None:
        """Supervisor metadata; not a participant topology projection."""
        with self._lock:
            return self._binding_for(self._snapshot.physical_qubit_ids)

    def _binding_for(self, physical_qubit_ids: tuple[int, ...]) -> RetainedRegionBinding | None:
        for binding in (self._region_binding, self._alternate_region_binding):
            if binding is not None and binding.physical_qubit_ids == physical_qubit_ids:
                return binding
        if self._region_binding is not None:
            raise ValueError("physical region is outside the frozen bindings")
        return None

    @property
    def regional_bindings(self) -> tuple[RetainedRegionBinding, ...]:
        return tuple(binding for binding in (self._region_binding, self._alternate_region_binding)
                     if binding is not None)

    def inactive_region(self) -> InactiveRegionState | None:
        with self._lock:
            return self._inactive_region

    def clone_detached(self) -> QpuStateStore:
        """Copy the active and parked states for bounded numerical preparation."""
        with self._lock:
            return QpuStateStore(self._snapshot, region_binding=self._region_binding,
                                 alternate_region_binding=self._alternate_region_binding,
                                 inactive_region=self._inactive_region)

    def read(self) -> QpuStateSnapshot:
        with self._lock:
            return self._snapshot

    def events(self) -> tuple[QpuEventRecord, ...]:
        with self._lock:
            return tuple(self._events)

    def _forget_job_events(self, job_id: str) -> int:
        """Trusted retention: drop a discarded job's event records.

        Event IDs follow the monotonic revision, so a dropped ID cannot be
        committed again. The latest committed batch stays for its rollback.
        """
        with self._lock:
            rollback = self._rollback_batch
            if rollback is not None and any(e.job_id == job_id for e in rollback.token.events):
                raise ValueError("the latest committed batch is retained for rollback")
            kept, removed_before_start = [], 0
            for index, event in enumerate(self._events):
                if event.job_id != job_id:
                    kept.append(event)
                    continue
                self._event_ids.discard(event.event_id)
                if rollback is not None and index < rollback.history_start:
                    removed_before_start += 1
            removed = len(self._events) - len(kept)
            self._events = kept
            if removed_before_start:
                self._rollback_batch = _CommittedQpuBatch(
                    rollback.token, rollback.history_start - removed_before_start)
            return removed

    def validate_state(self, matrix: Matrix) -> None:
        """Check a candidate result before a multi-event service operation begins."""

        with self._lock:
            dimension = self._snapshot.local_dimension ** len(
                self._snapshot.physical_qubit_ids
            )
        _validate_density_matrix(matrix, dimension)

    def commit(
        self,
        expected_revision: int,
        transition: QpuStateTransition,
    ) -> QpuStateSnapshot:
        with self._lock:
            current = self._snapshot
            if int(expected_revision) != current.revision:
                raise RevisionConflictError(
                    f"expected revision {expected_revision}, current {current.revision}"
                )
            if transition.event_id in self._event_ids:
                raise ValueError(f"event_id already committed: {transition.event_id}")
            next_qubits = transition.next_physical_qubit_ids or current.physical_qubit_ids
            if len(next_qubits) != len(current.physical_qubit_ids):
                raise ValueError("resource assignment cannot change the local state dimension")
            if next_qubits != current.physical_qubit_ids:
                if transition.event_kind not in {"isolated_resource_assignment", "tenant_region_assignment"}:
                    raise ValueError("physical resource may change only in an assignment event")
                if (transition.event_kind == "isolated_resource_assignment"
                        and transition.next_owner_tenant_id is not None):
                    raise ValueError("resource assignment must clear the previous owner")
            if transition.event_kind == "tenant_region_assignment":
                if (self._inactive_region is None
                        or next_qubits != self._inactive_region.physical_qubit_ids
                        or transition.next_owner_tenant_id != self._inactive_region.owner_tenant_id):
                    raise ValueError("tenant assignment requires the other retained region")
            elif next_qubits != current.physical_qubit_ids:
                _validate_density_matrix(transition.next_density_matrix,
                                         current.local_dimension ** len(current.physical_qubit_ids))
                assigned = np.asarray(transition.next_density_matrix, dtype=complex)
                if (abs(assigned[0, 0] - 1.0) > 1e-10
                        or np.any(np.abs(assigned[0, 1:]) > 1e-10)
                        or np.any(np.abs(assigned[1:, :]) > 1e-10)):
                    raise ValueError("isolated resource assignment must start from fresh zero state")
            # Snapshot construction validates the complete candidate before any
            # mutation. Do not repeat the same PSD eigensolve on ordinary commits.
            next_snapshot = QpuStateSnapshot(
                schema_version=current.schema_version,
                qpu_id=current.qpu_id,
                revision=current.revision + 1,
                device_time_us=current.device_time_us + transition.duration_us,
                physical_qubit_ids=next_qubits,
                local_dimension=current.local_dimension,
                density_matrix=transition.next_density_matrix,
                device_config_version=current.device_config_version,
                owner_tenant_id=transition.next_owner_tenant_id,
                last_event_id=transition.event_id,
                last_job_id=transition.job_id,
            )
            binding = self._binding_for(next_qubits)
            if binding is not None:
                binding.validate_snapshot(next_snapshot)
            record = QpuEventRecord(
                event_id=transition.event_id,
                event_kind=transition.event_kind,
                job_id=transition.job_id,
                revision_before=current.revision,
                revision_after=next_snapshot.revision,
                device_time_start_us=current.device_time_us,
                device_time_end_us=next_snapshot.device_time_us,
                owner_before=current.owner_tenant_id,
                owner_after=next_snapshot.owner_tenant_id,
                scenario_manifest_id=transition.scenario_manifest_id,
                physical_qubit_ids_before=current.physical_qubit_ids,
                physical_qubit_ids_after=next_qubits,
            )
            self._snapshot = next_snapshot
            if transition.event_kind == "tenant_region_assignment":
                self._inactive_region = InactiveRegionState(
                    current.physical_qubit_ids, current.density_matrix,
                    current.owner_tenant_id, current.device_time_us,
                )
            self._events.append(record)
            self._event_ids.add(transition.event_id)
            self._rollback_batch = None
            return next_snapshot


def single_qubit_zero_snapshot(
    *,
    qpu_id: str,
    physical_qubit_id: int,
    device_config_version: str,
) -> QpuStateSnapshot:
    return local_zero_snapshot(qpu_id=qpu_id, physical_qubit_ids=(physical_qubit_id,),
                               device_config_version=device_config_version)


def local_zero_snapshot(
    *, qpu_id: str, physical_qubit_ids: tuple[int, ...], device_config_version: str,
) -> QpuStateSnapshot:
    if not 1 <= len(physical_qubit_ids) <= MAX_DENSE_LOCAL_QUBITS:
        raise ValueError("initial local region exceeds the dense adapter's capacity")
    dimension = 1 << len(physical_qubit_ids)
    state = np.zeros((dimension, dimension), dtype=complex)
    state[0, 0] = 1.0
    return QpuStateSnapshot(
        schema_version="shared_qpu_state_v1",
        qpu_id=qpu_id,
        revision=0,
        device_time_us=0.0,
        physical_qubit_ids=physical_qubit_ids,
        local_dimension=2,
        density_matrix=tuple(tuple(row) for row in state.tolist()),
        device_config_version=device_config_version,
    )


def _immutable_density_matrix(matrix: Matrix) -> Matrix:
    # Preserve already frozen core results without another full matrix copy.
    # Mutable arrays/lists and custom numeric objects must not alias the store.
    if type(matrix) is tuple and all(
        type(row) is tuple and all(type(value) is complex for value in row)
        for row in matrix
    ):
        return matrix
    return tuple(tuple(complex(value) for value in row) for row in matrix)


def _validate_density_matrix(matrix: Matrix, dimension: int) -> None:
    if len(matrix) != dimension or any(len(row) != dimension for row in matrix):
        raise ValueError("density_matrix dimension does not match the QPU region")
    array = np.asarray(matrix, dtype=complex)
    if not np.all(np.isfinite(array)):
        raise ValueError("density_matrix must contain finite values")
    if np.max(np.abs(array - array.conj().T)) > 1e-10:
        raise ValueError("density_matrix must be Hermitian")
    if abs(np.trace(array) - 1.0) > 1e-10:
        raise ValueError("density_matrix trace must equal one")
    diagonal = np.diag(array)
    # A verified diagonal matrix has these exact eigenvalues. This preserves
    # the PSD check, while avoiding a cubic solve after full-basis collapse.
    eigenvalues = (diagonal.real if np.count_nonzero(array) == np.count_nonzero(diagonal)
                   else np.linalg.eigvalsh(array))
    if float(np.min(eigenvalues)) < -1e-10:
        raise ValueError("density_matrix must be positive semidefinite")


def _identifier(value: str, name: str) -> str:
    converted = str(value).strip()
    if not converted:
        raise ValueError(f"{name} must not be empty")
    return converted


def _batch_identifier(value: object, name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
