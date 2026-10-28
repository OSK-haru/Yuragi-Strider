"""Bounded state snapshot policy and serialization helpers."""

from __future__ import annotations

import math
import importlib
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Literal

from core.gates import Matrix
from core.backend_boundary import PYTHON_DENSE_BACKEND, RUST_DENSE_BACKEND, RUST_DENSE_PREVIEW_BACKEND


MAX_STATE_SNAPSHOTS = 10
SNAPSHOT_TIME_TOLERANCE_US = 1e-12
SnapshotKind = Literal[
    "initial",
    "uniform_time",
    "custom_time",
    "column_boundary",
    "measurement",
    "after_circuit",
    "idle_sample",
    "final",
]

_KIND_PRIORITY: dict[str, int] = {
    "idle_sample": 10,
    "uniform_time": 20,
    "column_boundary": 40,
    "measurement": 45,
    "after_circuit": 50,
    "initial": 110,
    "final": 110,
    "custom_time": 100,
}


@dataclass(frozen=True)
class SnapshotOptions:
    """Optional request policy for bounded, future-compatible snapshots."""

    enabled: bool = True
    uniform_count: int = 0
    custom_times_us: tuple[float, ...] = ()
    include_initial: bool = True
    include_final: bool = True
    include_column_boundaries: bool = True
    include_after_circuit: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.uniform_count, bool):
            raise ValueError("uniform_count must be an integer")
        count = int(self.uniform_count)
        if count < 0 or count > 100 or count == 1:
            raise ValueError("uniform_count must be 0 or an integer from 2 to 100")
        times = tuple(float(value) for value in self.custom_times_us)
        if len(times) > 100:
            raise ValueError("custom_times_us must contain at most 100 values")
        if any(not math.isfinite(value) for value in times):
            raise ValueError("custom_times_us must contain only finite numbers")
        if any(value < 0.0 for value in times):
            raise ValueError("custom_times_us must not contain negative values")
        object.__setattr__(self, "uniform_count", count)
        object.__setattr__(self, "custom_times_us", times)
        object.__setattr__(self, "enabled", bool(self.enabled))
        object.__setattr__(self, "include_initial", bool(self.include_initial))
        object.__setattr__(self, "include_final", bool(self.include_final))
        object.__setattr__(self, "include_column_boundaries", bool(self.include_column_boundaries))
        object.__setattr__(self, "include_after_circuit", bool(self.include_after_circuit))

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "uniform_count": self.uniform_count,
            "custom_times_us": list(self.custom_times_us),
            "include_initial": self.include_initial,
            "include_final": self.include_final,
            "include_column_boundaries": self.include_column_boundaries,
            "include_after_circuit": self.include_after_circuit,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SnapshotOptions":
        return cls(
            enabled=data.get("enabled", True),
            uniform_count=data.get("uniform_count", 0),
            custom_times_us=tuple(data.get("custom_times_us") or ()),
            include_initial=data.get("include_initial", True),
            include_final=data.get("include_final", True),
            include_column_boundaries=data.get("include_column_boundaries", True),
            include_after_circuit=data.get("include_after_circuit", True),
        )


@dataclass(frozen=True)
class SnapshotRequest:
    requested_time_us: float
    kind: Literal["uniform_time", "custom_time"]
    priority: int


@dataclass(frozen=True)
class SnapshotPlan:
    enabled: bool
    requests: tuple[SnapshotRequest, ...]
    include_initial: bool
    include_final: bool
    include_column_boundaries: bool
    include_after_circuit: bool

    @property
    def requested_times(self) -> tuple[float, ...]:
        return tuple(request.requested_time_us for request in self.requests)


def build_snapshot_plan(
    options: SnapshotOptions | None,
    *,
    actual_duration_us: float,
) -> SnapshotPlan:
    if options is None or not options.enabled:
        return SnapshotPlan(
            enabled=False,
            requests=(),
            include_initial=True,
            include_final=True,
            include_column_boundaries=True,
            include_after_circuit=True,
        )

    if any(time > actual_duration_us + SNAPSHOT_TIME_TOLERANCE_US for time in options.custom_times_us):
        raise ValueError("custom_times_us values must not exceed the total simulation duration")

    requests: list[SnapshotRequest] = []
    if options.uniform_count >= 2:
        for index in range(1, options.uniform_count + 1):
            requests.append(SnapshotRequest(
                requested_time_us=actual_duration_us * index / (options.uniform_count + 1),
                kind="uniform_time",
                priority=_KIND_PRIORITY["uniform_time"],
            ))
    requests.extend(
        SnapshotRequest(
            requested_time_us=time,
            kind="custom_time",
            priority=_KIND_PRIORITY["custom_time"],
        )
        for time in options.custom_times_us
    )
    requests.sort(key=lambda request: (request.requested_time_us, -request.priority))
    deduped: list[SnapshotRequest] = []
    for request in requests:
        if deduped and math.isclose(
            deduped[-1].requested_time_us,
            request.requested_time_us,
            rel_tol=0.0,
            abs_tol=SNAPSHOT_TIME_TOLERANCE_US,
        ):
            if request.priority > deduped[-1].priority:
                deduped[-1] = request
            continue
        deduped.append(request)

    return SnapshotPlan(
        enabled=True,
        requests=tuple(deduped),
        include_initial=options.include_initial,
        include_final=options.include_final,
        include_column_boundaries=options.include_column_boundaries,
        include_after_circuit=options.include_after_circuit,
    )


@dataclass(frozen=True)
class StateSnapshot:
    index: int
    requested_time_us: float | None
    time_us: float
    progress: float
    kind: SnapshotKind
    capture_method: Literal[
        "exact_integration_boundary",
        "nearest_existing_time",
        "simulation_event_boundary",
    ]
    event_kind: str | None
    column_index: int | None
    density_matrix: Matrix

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": int(self.index),
            "requested_time_us": (
                None
                if self.requested_time_us is None
                else float(self.requested_time_us)
            ),
            "time_us": float(self.time_us),
            "progress": float(self.progress),
            "kind": str(self.kind),
            "capture_method": str(self.capture_method),
            "event_kind": self.event_kind,
            "column_index": self.column_index,
            "density_matrix": serialize_complex_matrix(self.density_matrix),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StateSnapshot":
        matrix_data = data["density_matrix"]
        real = matrix_data["real"]
        imag = matrix_data["imag"]
        matrix = tuple(
            tuple(complex(real_value, imag_value) for real_value, imag_value in zip(real_row, imag_row))
            for real_row, imag_row in zip(real, imag)
        )
        return cls(
            index=int(data["index"]),
            requested_time_us=(
                None
                if data.get("requested_time_us") is None
                else float(data["requested_time_us"])
            ),
            time_us=float(data["time_us"]),
            progress=float(data["progress"]),
            kind=str(data["kind"]),  # type: ignore[arg-type]
            capture_method=str(
                data.get("capture_method", "simulation_event_boundary")
            ),  # type: ignore[arg-type]
            event_kind=(
                None if data.get("event_kind") is None else str(data["event_kind"])
            ),
            column_index=(
                None
                if data.get("column_index") is None
                else int(data["column_index"])
            ),
            density_matrix=matrix,
        )


@dataclass(frozen=True)
class SnapshotDiagnostics:
    count: int
    max_count: int
    matrix_dimension: int
    policy: str
    requested_uniform_count: int = 0
    requested_custom_count: int = 0
    event_candidate_count: int = 0
    candidate_count: int = 0
    deduplicated_count: int = 0
    dropped_by_cap_count: int = 0
    exact_capture_count: int = 0
    nearest_capture_count: int = 0
    max_time_error_us: float = 0.0
    selection_backend: str = PYTHON_DENSE_BACKEND
    selection_fallback_reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "state_snapshot_count": float(self.count),
            "state_snapshot_max_count": float(self.max_count),
            "state_snapshot_matrix_dimension": float(self.matrix_dimension),
            "state_snapshot_policy": self.policy,
            "state_snapshot_requested_uniform_count": float(self.requested_uniform_count),
            "state_snapshot_requested_custom_count": float(self.requested_custom_count),
            "state_snapshot_event_candidate_count": float(self.event_candidate_count),
            "state_snapshot_candidate_count": float(self.candidate_count),
            "state_snapshot_returned_count": float(self.count),
            "state_snapshot_deduplicated_count": float(self.deduplicated_count),
            "state_snapshot_dropped_by_cap_count": float(self.dropped_by_cap_count),
            "state_snapshot_exact_capture_count": float(self.exact_capture_count),
            "state_snapshot_nearest_capture_count": float(self.nearest_capture_count),
            "state_snapshot_max_time_error_us": float(self.max_time_error_us),
            "state_snapshot_hard_cap": float(self.max_count),
            "state_snapshot_selection_backend": self.selection_backend,
            "state_snapshot_selection_fallback_reason": self.selection_fallback_reason,
        }


@dataclass
class _SnapshotCandidate:
    requested_time_us: float | None
    time_us: float
    kind: SnapshotKind
    capture_method: str
    event_kind: str | None
    column_index: int | None
    density_matrix: Matrix


class StateSnapshotCollector:
    """Collect semantic density-matrix snapshots and emit a bounded list."""

    def __init__(
        self,
        *,
        actual_duration_us: float,
        max_snapshots: int = MAX_STATE_SNAPSHOTS,
        plan: SnapshotPlan | None = None,
        simulation_backend: str = PYTHON_DENSE_BACKEND,
    ) -> None:
        self.actual_duration_us = float(actual_duration_us)
        self.max_snapshots = int(max_snapshots)
        self.plan = plan
        self.simulation_backend = str(simulation_backend)
        self.selection_backend = PYTHON_DENSE_BACKEND
        self.selection_fallback_reason = ""
        self._candidates: list[_SnapshotCandidate] = []
        self._requested_uniform_count = sum(
            request.kind == "uniform_time" for request in plan.requests
        ) if plan is not None else 0
        self._requested_custom_count = sum(
            request.kind == "custom_time" for request in plan.requests
        ) if plan is not None else 0
        self._event_candidate_count = 0
        self._exact_capture_count = 0
        self._nearest_capture_count = 0
        self._max_time_error_us = 0.0

    def capture(
        self,
        *,
        time_us: float,
        kind: SnapshotKind,
        density_matrix: Matrix,
        column_index: int | None = None,
        requested_time_us: float | None = None,
        capture_method: str | None = None,
        event_kind: str | None = None,
    ) -> None:
        if capture_method is None:
            capture_method = (
                "simulation_event_boundary"
                if requested_time_us is None
                else "exact_integration_boundary"
            )
        if requested_time_us is not None:
            time_error = abs(float(time_us) - float(requested_time_us))
            self._max_time_error_us = max(self._max_time_error_us, time_error)
            if capture_method == "exact_integration_boundary":
                self._exact_capture_count += 1
            elif capture_method == "nearest_existing_time":
                self._nearest_capture_count += 1
        else:
            self._event_candidate_count += 1
        self._candidates.append(
            _SnapshotCandidate(
                requested_time_us=(
                    None
                    if requested_time_us is None
                    else float(requested_time_us)
                ),
                time_us=float(time_us),
                kind=kind,
                capture_method=capture_method,
                event_kind=event_kind,
                column_index=column_index,
                density_matrix=copy_density_matrix(density_matrix),
            )
        )

    def capture_event(
        self,
        *,
        time_us: float,
        event_kind: str,
        density_matrix: Matrix,
        column_index: int | None = None,
    ) -> None:
        if self.plan is not None and self.plan.enabled:
            event_enabled = {
                "initial": self.plan.include_initial,
                "final": self.plan.include_final,
                "column_boundary": self.plan.include_column_boundaries,
                "after_circuit": self.plan.include_after_circuit,
            }.get(event_kind, True)
            if not event_enabled:
                return
        self.capture(
            time_us=time_us,
            kind=event_kind,  # type: ignore[arg-type]
            density_matrix=density_matrix,
            column_index=column_index,
            event_kind=event_kind,
        )

    def capture_requested_time(self, *, time_us: float, density_matrix: Matrix) -> None:
        request = self.request_for_time(time_us)
        if request is None:
            return
        self.capture(
            time_us=time_us,
            requested_time_us=request.requested_time_us,
            kind=request.kind,
            density_matrix=density_matrix,
            capture_method="exact_integration_boundary",
            event_kind=None,
        )

    def request_for_time(self, time_us: float):
        """Return the matching explicit request without requiring a matrix."""

        if self.plan is None or not self.plan.enabled:
            return None
        return next(
            (
                request
                for request in self.plan.requests
                if math.isclose(
                    request.requested_time_us,
                    time_us,
                    rel_tol=0.0,
                    abs_tol=SNAPSHOT_TIME_TOLERANCE_US,
                )
            ),
            None,
        )

    def finalize(self) -> tuple[list[StateSnapshot], SnapshotDiagnostics]:
        candidate_count = len(self._candidates)
        deduplicated_count = 0
        if self.simulation_backend in {RUST_DENSE_BACKEND, RUST_DENSE_PREVIEW_BACKEND}:
            try:
                module = importlib.import_module("yuragi_strider_rust")
                selector = getattr(module, "select_snapshot_indices_v1", None)
                if selector is None:
                    raise RuntimeError("Rust extension does not provide select_snapshot_indices_v1")
                selected_indices, deduplicated_count = selector(
                    [(candidate.time_us, candidate.kind) for candidate in self._candidates],
                    self.max_snapshots,
                )
                selected = [self._candidates[int(index)] for index in selected_indices]
                self.selection_backend = self.simulation_backend
            except (ImportError, RuntimeError, ValueError, IndexError) as exc:
                if self.simulation_backend == RUST_DENSE_BACKEND:
                    raise
                self.selection_fallback_reason = str(exc)
                deduped = _dedupe_by_time(self._candidates)
                deduplicated_count = max(0, candidate_count - len(deduped))
                selected = _select_bounded(deduped, self.max_snapshots)
        else:
            deduped = _dedupe_by_time(self._candidates)
            deduplicated_count = max(0, candidate_count - len(deduped))
            selected = _select_bounded(deduped, self.max_snapshots)
        selected.sort(key=lambda snapshot: snapshot.time_us)

        snapshots = [
            StateSnapshot(
                index=index,
                requested_time_us=candidate.requested_time_us,
                time_us=candidate.time_us,
                progress=_progress(candidate.time_us, self.actual_duration_us),
                kind=candidate.kind,
                capture_method=candidate.capture_method,  # type: ignore[arg-type]
                event_kind=candidate.event_kind,
                column_index=candidate.column_index,
                density_matrix=candidate.density_matrix,
            )
            for index, candidate in enumerate(selected)
        ]
        matrix_dimension = len(snapshots[0].density_matrix) if snapshots else 0
        return snapshots, SnapshotDiagnostics(
            count=len(snapshots),
            max_count=self.max_snapshots,
            matrix_dimension=matrix_dimension,
            policy=(
                "cptp_ready_requested_v1"
                if self.plan is not None and self.plan.enabled
                else "bounded_semantic_v1"
            ),
            requested_uniform_count=self._requested_uniform_count,
            requested_custom_count=self._requested_custom_count,
            event_candidate_count=self._event_candidate_count,
            candidate_count=candidate_count,
            deduplicated_count=deduplicated_count,
            dropped_by_cap_count=max(0, candidate_count - deduplicated_count - len(selected)),
            exact_capture_count=self._exact_capture_count,
            nearest_capture_count=self._nearest_capture_count,
            max_time_error_us=self._max_time_error_us,
            selection_backend=self.selection_backend,
            selection_fallback_reason=self.selection_fallback_reason,
        )


def copy_density_matrix(matrix: Matrix) -> Matrix:
    return tuple(tuple(complex(entry) for entry in row) for row in matrix)


def serialize_complex_matrix(matrix: Matrix) -> dict[str, list[list[float]]]:
    _validate_square_finite_matrix(matrix)
    return {
        "real": [[float(entry.real) for entry in row] for row in matrix],
        "imag": [[float(entry.imag) for entry in row] for row in matrix],
    }


def serialize_state_snapshots(
    snapshots: list[StateSnapshot],
) -> tuple[list[dict[str, Any]], float]:
    started_at = perf_counter()
    serialized = [snapshot.to_dict() for snapshot in snapshots]
    return serialized, (perf_counter() - started_at) * 1000.0


def idle_sample_times(
    times: list[float],
    *,
    completion_time_us: float,
    final_time_us: float,
    max_idle_samples: int = 3,
) -> set[float]:
    if final_time_us <= completion_time_us + SNAPSHOT_TIME_TOLERANCE_US:
        return set()

    idle_times = [
        time
        for time in times
        if completion_time_us + SNAPSHOT_TIME_TOLERANCE_US
        < time
        < final_time_us - SNAPSHOT_TIME_TOLERANCE_US
    ]
    if not idle_times:
        return set()

    if len(idle_times) <= max_idle_samples:
        return set(idle_times)

    selected: set[float] = set()
    for sample_index in range(1, max_idle_samples + 1):
        target_position = sample_index * (len(idle_times) + 1) / (max_idle_samples + 1)
        selected.add(idle_times[min(len(idle_times) - 1, max(0, round(target_position) - 1))])
    return selected


def is_planned_time(time_us: float, planned_times: set[float]) -> bool:
    return any(
        math.isclose(time_us, planned, rel_tol=0.0, abs_tol=SNAPSHOT_TIME_TOLERANCE_US)
        for planned in planned_times
    )


def _dedupe_by_time(candidates: list[_SnapshotCandidate]) -> list[_SnapshotCandidate]:
    deduped: list[_SnapshotCandidate] = []
    for candidate in sorted(candidates, key=lambda item: item.time_us):
        existing_index = _find_matching_time(deduped, candidate.time_us)
        if existing_index is None:
            deduped.append(candidate)
            continue

        existing = deduped[existing_index]
        if _KIND_PRIORITY[candidate.kind] > _KIND_PRIORITY[existing.kind]:
            deduped[existing_index] = candidate

    return deduped


def _select_bounded(
    candidates: list[_SnapshotCandidate],
    max_snapshots: int,
) -> list[_SnapshotCandidate]:
    if len(candidates) <= max_snapshots:
        return list(candidates)

    pinned = [
        candidate
        for candidate in candidates
        if candidate.kind in {"initial", "after_circuit", "final"}
    ]
    selected: list[_SnapshotCandidate] = [
        candidate for candidate in pinned if candidate.kind in {"initial", "final"}
    ]
    remaining_slots = max(0, max_snapshots - len(selected))
    if remaining_slots == 0:
        return _dedupe_by_time(selected)[:max_snapshots]

    intermediates = [
        candidate
        for candidate in candidates
        if candidate not in selected
    ]
    custom = [candidate for candidate in intermediates if candidate.kind == "custom_time"]
    selected.extend(custom[:remaining_slots])
    remaining_slots = max(0, max_snapshots - len(selected))
    if remaining_slots:
        event_candidates = [
            candidate
            for candidate in intermediates
            if candidate not in selected
            and candidate.kind in {"column_boundary", "after_circuit"}
        ]
        selected.extend(event_candidates[:remaining_slots])
    remaining_slots = max(0, max_snapshots - len(selected))
    if remaining_slots:
        uniform_candidates = [
            candidate
            for candidate in intermediates
            if candidate not in selected
            and candidate.kind in {"uniform_time", "idle_sample"}
        ]
        selected.extend(_even_subset(uniform_candidates, remaining_slots))
    return _dedupe_by_time(selected)[:max_snapshots]


def _even_subset(
    candidates: list[_SnapshotCandidate],
    limit: int,
) -> list[_SnapshotCandidate]:
    if len(candidates) <= limit:
        return list(candidates)
    if limit <= 0:
        return []
    if limit == 1:
        return [candidates[len(candidates) // 2]]

    selected: list[_SnapshotCandidate] = []
    last_index = len(candidates) - 1
    for item_index in range(limit):
        candidate_index = round(item_index * last_index / (limit - 1))
        selected.append(candidates[candidate_index])
    return selected


def _find_matching_time(
    candidates: list[_SnapshotCandidate],
    time_us: float,
) -> int | None:
    for index, candidate in enumerate(candidates):
        if math.isclose(
            candidate.time_us,
            time_us,
            rel_tol=0.0,
            abs_tol=SNAPSHOT_TIME_TOLERANCE_US,
        ):
            return index
    return None


def _progress(time_us: float, actual_duration_us: float) -> float:
    if actual_duration_us <= 0.0:
        return 1.0
    return min(1.0, max(0.0, time_us / actual_duration_us))


def _validate_square_finite_matrix(matrix: Matrix) -> None:
    dimension = len(matrix)
    if dimension == 0:
        raise ValueError("density matrix must not be empty")
    for row in matrix:
        if len(row) != dimension:
            raise ValueError("density matrix must be square")
        for entry in row:
            if not math.isfinite(float(entry.real)) or not math.isfinite(float(entry.imag)):
                raise ValueError("density matrix contains non-finite values")
