"""Standalone Python wrapper for optional Rust dense kernels."""

from __future__ import annotations

import importlib
import math
from collections.abc import Sequence

from core.backend_boundary import RUST_RESET_SEQUENCE_SCHEMA_VERSION
from core.gates import Matrix


def is_rust_kernel_available() -> bool:
    """Return whether the optional yuragi_strider_rust module is importable."""

    try:
        importlib.import_module("yuragi_strider_rust")
    except Exception:
        return False
    return True


def flatten_complex_matrix(matrix: Sequence[Sequence[complex]]) -> list[float]:
    """Return row-major interleaved complex values for a square matrix."""

    if not isinstance(matrix, Sequence) or isinstance(matrix, (str, bytes)):
        raise ValueError("matrix must be a square matrix")
    dimension = len(matrix)
    if dimension <= 0:
        raise ValueError("matrix must be non-empty")
    flat: list[float] = []
    for row_index, row in enumerate(matrix):
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
            raise ValueError(f"matrix[{row_index}] must be a row sequence")
        if len(row) != dimension:
            raise ValueError("matrix must be square and non-ragged")
        for value in row:
            try:
                complex_value = complex(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("matrix entries must be complex-compatible") from exc
            flat.extend([float(complex_value.real), float(complex_value.imag)])
    if len(flat) != 2 * dimension * dimension:
        raise ValueError("matrix flattening produced an unexpected length")
    return flat


def unflatten_complex_matrix(flat: Sequence[float], d: int) -> Matrix:
    """Return a tuple matrix from row-major interleaved complex values."""

    d = _validate_dimension(d)
    expected = 2 * d * d
    if len(flat) != expected:
        raise ValueError(
            f"flat length must be 2 * d * d; received len(flat)={len(flat)} for d={d}"
        )
    rows = []
    for row in range(d):
        values = []
        for column in range(d):
            index = 2 * (row * d + column)
            values.append(complex(float(flat[index]), float(flat[index + 1])))
        rows.append(tuple(values))
    return tuple(rows)


def flatten_collapse_ops(
    collapse_ops: Sequence[Sequence[Sequence[complex]]],
) -> list[float]:
    """Return concatenated flat row-major collapse operators."""

    flat: list[float] = []
    expected_dimension: int | None = None
    for index, operator in enumerate(collapse_ops):
        dimension = _validate_square_matrix(operator, f"collapse_ops[{index}]")
        if expected_dimension is None:
            expected_dimension = dimension
        elif dimension != expected_dimension:
            raise ValueError("all collapse operators must have the same dimension")
        flat.extend(flatten_complex_matrix(operator))
    return flat


class RustDenseSession:
    """Persistent Rust RK4 state with prepared operators and reusable buffers."""

    def __init__(
        self,
        rho: Sequence[Sequence[complex]],
        collapse_ops: Sequence[Sequence[Sequence[complex]]],
    ) -> None:
        dimension = _validate_square_matrix(rho, "rho")
        for index, operator in enumerate(collapse_ops):
            if _validate_square_matrix(operator, f"collapse_ops[{index}]") != dimension:
                raise ValueError("collapse operators must match rho dimension")
        rust_module = _load_rust_module()
        self._dimension = dimension
        self._session = rust_module.DenseRk4Session(
            flatten_complex_matrix(rho),
            flatten_collapse_ops(collapse_ops),
            len(collapse_ops),
            dimension,
        )
        self._synced_state = _as_matrix(rho)
        self._synced_ideal_state = _as_matrix(rho)
        self._hamiltonian_ids: dict[Matrix, int] = {}

    @classmethod
    def from_local_rates(
        cls,
        rho: Sequence[Sequence[complex]],
        n_qubits: int,
        gamma_down_per_us: float,
        gamma_up_per_us: float,
        gamma_phi_per_us: float,
    ) -> "RustDenseSession":
        """Create a session while constructing local noise operators in Rust."""

        dimension = _validate_square_matrix(rho, "rho")
        if int(n_qubits) != n_qubits or int(n_qubits) < 0:
            raise ValueError("n_qubits must be a non-negative integer")
        if dimension != 1 << int(n_qubits):
            raise ValueError("rho dimension must equal 2 ** n_qubits")
        rust_module = _load_rust_module()
        instance = cls.__new__(cls)
        instance._dimension = dimension
        instance._session = rust_module.DenseRk4Session.from_local_rates(
            flatten_complex_matrix(rho),
            int(n_qubits),
            float(gamma_down_per_us),
            float(gamma_up_per_us),
            float(gamma_phi_per_us),
        )
        instance._synced_state = _as_matrix(rho)
        instance._synced_ideal_state = _as_matrix(rho)
        instance._hamiltonian_ids = {}
        return instance

    def _sync_state(self, rho: Sequence[Sequence[complex]]) -> None:
        state = _as_matrix(rho)
        if state != self._synced_state:
            if len(state) != self._dimension:
                raise ValueError("rho dimension changed during a Rust session")
            self._session.set_state(flatten_complex_matrix(state))
            self._synced_state = state

    def evolve_cleaned(
        self,
        rho: Sequence[Sequence[complex]],
        hamiltonian: Sequence[Sequence[complex]],
        dt: float,
        substeps: int,
    ) -> Matrix:
        states = self.evolve_piecewise_cleaned_samples(
            rho, [hamiltonian], [dt], [substeps]
        )
        return states[-1]

    def evolve_piecewise_cleaned_samples(
        self,
        rho: Sequence[Sequence[complex]],
        hamiltonians: Sequence[Sequence[Sequence[complex]]],
        dts: Sequence[float],
        substeps: Sequence[int],
    ) -> tuple[Matrix, ...]:
        self._sync_state(rho)
        if not hamiltonians or len(hamiltonians) != len(dts) or len(dts) != len(substeps):
            raise ValueError("hamiltonians, dts, and substeps must have the same non-zero length")
        checked_dts: list[float] = []
        checked_substeps: list[int] = []
        for index, (hamiltonian, dt, count) in enumerate(zip(hamiltonians, dts, substeps)):
            if _validate_square_matrix(hamiltonian, f"hamiltonians[{index}]") != self._dimension:
                raise ValueError("hamiltonians must match the session dimension")
            value = float(dt)
            if not math.isfinite(value):
                raise ValueError("dts must be finite")
            if int(count) != count or int(count) <= 0:
                raise ValueError("substeps entries must be positive integers")
            checked_dts.append(value)
            checked_substeps.append(int(count))
        result = self._session.evolve_registered_cleaned_samples(
            self._registered_hamiltonian_ids(hamiltonians),
            checked_dts,
            checked_substeps,
        )
        matrix_size = 2 * self._dimension * self._dimension
        expected = len(hamiltonians) * matrix_size
        if len(result) != expected:
            raise RuntimeError("yuragi_strider_rust returned an unexpected piecewise output length")
        states = tuple(
            unflatten_complex_matrix(result[start:start + matrix_size], self._dimension)
            for start in range(0, expected, matrix_size)
        )
        self._synced_state = states[-1]
        return states

    def evolve_paired_piecewise_metrics(
        self,
        rho: Sequence[Sequence[complex]],
        ideal_state: Sequence[Sequence[complex]],
        hamiltonians: Sequence[Sequence[Sequence[complex]]],
        dts: Sequence[float],
        substeps: Sequence[int],
    ) -> tuple[tuple[Matrix, ...], tuple[Matrix, ...], tuple[tuple[float, float, float, float], ...]]:
        """Evolve noisy and ideal trajectories and compute metrics in one Rust call."""

        self._sync_state(rho)
        ideal = _as_matrix(ideal_state)
        if ideal != self._synced_ideal_state:
            if len(ideal) != self._dimension:
                raise ValueError("ideal_state dimension changed during a Rust session")
            self._session.set_ideal_state(flatten_complex_matrix(ideal))
            self._synced_ideal_state = ideal
        flat_hamiltonians, checked_dts, checked_substeps = self._prepare_piecewise_inputs(
            hamiltonians, dts, substeps
        )
        noisy_flat, ideal_flat, metrics_flat = self._session.evolve_paired_piecewise_metrics(
            flat_hamiltonians, checked_dts, checked_substeps
        )
        matrix_size = 2 * self._dimension * self._dimension
        expected = len(hamiltonians) * matrix_size
        if len(noisy_flat) != expected or len(ideal_flat) != expected:
            raise RuntimeError("yuragi_strider_rust returned an unexpected paired output length")
        if len(metrics_flat) != 4 * len(hamiltonians):
            raise RuntimeError("yuragi_strider_rust returned an unexpected metrics length")
        noisy_states = tuple(
            unflatten_complex_matrix(noisy_flat[start:start + matrix_size], self._dimension)
            for start in range(0, expected, matrix_size)
        )
        ideal_states = tuple(
            unflatten_complex_matrix(ideal_flat[start:start + matrix_size], self._dimension)
            for start in range(0, expected, matrix_size)
        )
        metrics = tuple(
            (
                float(metrics_flat[index]),
                float(metrics_flat[index + 1]),
                float(metrics_flat[index + 2]),
                float(metrics_flat[index + 3]),
            )
            for index in range(0, len(metrics_flat), 4)
        )
        self._synced_state = noisy_states[-1]
        self._synced_ideal_state = ideal_states[-1]
        return noisy_states, ideal_states, metrics

    def evolve_paired_registered_compact(
        self,
        rho: Sequence[Sequence[complex]],
        ideal_state: Sequence[Sequence[complex]],
        hamiltonians: Sequence[Sequence[Sequence[complex]]],
        dts: Sequence[float],
        substeps: Sequence[int],
        capture_flags: Sequence[bool],
    ) -> tuple[
        dict[int, tuple[Matrix, Matrix]],
        tuple[tuple[float, float, float, float], ...],
    ]:
        """Return metrics for every point and matrices only where required."""

        self._sync_state(rho)
        ideal = _as_matrix(ideal_state)
        if ideal != self._synced_ideal_state:
            if len(ideal) != self._dimension:
                raise ValueError("ideal_state dimension changed during a Rust session")
            self._session.set_ideal_state(flatten_complex_matrix(ideal))
            self._synced_ideal_state = ideal
        checked_dts, checked_substeps = self._validate_piecewise_schedule(
            hamiltonians, dts, substeps
        )
        if len(capture_flags) != len(hamiltonians):
            raise ValueError("capture_flags must match hamiltonians length")
        hamiltonian_ids = self._registered_hamiltonian_ids(hamiltonians)
        indices, noisy_flat, ideal_flat, metrics_flat = (
            self._session.evolve_paired_registered_compact(
                hamiltonian_ids,
                checked_dts,
                checked_substeps,
                [bool(flag) for flag in capture_flags],
            )
        )
        matrix_size = 2 * self._dimension * self._dimension
        if len(noisy_flat) != len(indices) * matrix_size or len(ideal_flat) != len(noisy_flat):
            raise RuntimeError("yuragi_strider_rust returned an unexpected compact state length")
        if len(metrics_flat) != 4 * len(hamiltonians):
            raise RuntimeError("yuragi_strider_rust returned an unexpected compact metrics length")
        captured: dict[int, tuple[Matrix, Matrix]] = {}
        for offset, raw_index in enumerate(indices):
            index = int(raw_index)
            start = offset * matrix_size
            captured[index] = (
                unflatten_complex_matrix(noisy_flat[start:start + matrix_size], self._dimension),
                unflatten_complex_matrix(ideal_flat[start:start + matrix_size], self._dimension),
            )
        metrics = tuple(
            tuple(float(metrics_flat[index + delta]) for delta in range(4))
            for index in range(0, len(metrics_flat), 4)
        )
        final_pair = captured[len(hamiltonians) - 1]
        self._synced_state, self._synced_ideal_state = final_pair
        return captured, metrics

    def _registered_hamiltonian_ids(
        self,
        hamiltonians: Sequence[Sequence[Sequence[complex]]],
    ) -> list[int]:
        identifiers: list[int] = []
        for hamiltonian in hamiltonians:
            matrix = _as_matrix(hamiltonian)
            identifier = self._hamiltonian_ids.get(matrix)
            if identifier is None:
                identifier = int(
                    self._session.register_hamiltonian(flatten_complex_matrix(matrix))
                )
                self._hamiltonian_ids[matrix] = identifier
            identifiers.append(identifier)
        return identifiers

    def _validate_piecewise_schedule(
        self,
        hamiltonians: Sequence[Sequence[Sequence[complex]]],
        dts: Sequence[float],
        substeps: Sequence[int],
    ) -> tuple[list[float], list[int]]:
        if not hamiltonians or len(hamiltonians) != len(dts) or len(dts) != len(substeps):
            raise ValueError("hamiltonians, dts, and substeps must have the same non-zero length")
        checked_dts: list[float] = []
        checked_substeps: list[int] = []
        for index, (hamiltonian, dt, count) in enumerate(zip(hamiltonians, dts, substeps)):
            if _validate_square_matrix(hamiltonian, f"hamiltonians[{index}]") != self._dimension:
                raise ValueError("hamiltonians must match the session dimension")
            value = float(dt)
            if not math.isfinite(value):
                raise ValueError("dts must be finite")
            if int(count) != count or int(count) <= 0:
                raise ValueError("substeps entries must be positive integers")
            checked_dts.append(value)
            checked_substeps.append(int(count))
        return checked_dts, checked_substeps

    def _prepare_piecewise_inputs(
        self,
        hamiltonians: Sequence[Sequence[Sequence[complex]]],
        dts: Sequence[float],
        substeps: Sequence[int],
    ) -> tuple[list[float], list[float], list[int]]:
        if not hamiltonians or len(hamiltonians) != len(dts) or len(dts) != len(substeps):
            raise ValueError("hamiltonians, dts, and substeps must have the same non-zero length")
        flat_hamiltonians: list[float] = []
        checked_dts: list[float] = []
        checked_substeps: list[int] = []
        for index, (hamiltonian, dt, count) in enumerate(zip(hamiltonians, dts, substeps)):
            if _validate_square_matrix(hamiltonian, f"hamiltonians[{index}]") != self._dimension:
                raise ValueError("hamiltonians must match the session dimension")
            value = float(dt)
            if not math.isfinite(value):
                raise ValueError("dts must be finite")
            if int(count) != count or int(count) <= 0:
                raise ValueError("substeps entries must be positive integers")
            flat_hamiltonians.extend(flatten_complex_matrix(hamiltonian))
            checked_dts.append(value)
            checked_substeps.append(int(count))
        return flat_hamiltonians, checked_dts, checked_substeps


def _as_matrix(matrix: Sequence[Sequence[complex]]) -> Matrix:
    return tuple(tuple(complex(value) for value in row) for row in matrix)


def rust_lindblad_rhs(
    rho: Sequence[Sequence[complex]],
    hamiltonian: Sequence[Sequence[complex]],
    collapse_ops: Sequence[Sequence[Sequence[complex]]],
) -> Matrix:
    """Evaluate the raw Lindblad RHS through the optional Rust kernel."""

    dimension = _validate_evolution_inputs(rho, hamiltonian, collapse_ops)
    rust_module = _load_rust_module()
    result = rust_module.lindblad_rhs_flat(
        flatten_complex_matrix(rho),
        flatten_complex_matrix(hamiltonian),
        flatten_collapse_ops(collapse_ops),
        len(collapse_ops),
        dimension,
    )
    return unflatten_complex_matrix(result, dimension)


def rust_apply_local_reset_sequence(
    state: Sequence[Sequence[complex]],
    *,
    subsystem_count: int,
    local_dimension: int,
    targets: Sequence[int],
    success_probabilities: Sequence[float],
    target_populations: Sequence[Sequence[float]],
    reset_count: int,
) -> Matrix:
    """Apply a declared local replacement sequence in one Rust call.

    The operation implements ``E(rho)=(1-s)rho+s*tau*Tr_q(rho)`` for each
    target, preserving the existing most-significant-subsystem convention.
    """

    if type(subsystem_count) is not int or subsystem_count < 1:
        raise ValueError("subsystem_count must be positive")
    if type(local_dimension) is not int or local_dimension not in (2, 3):
        raise ValueError("local_dimension must be 2 or 3")
    if type(reset_count) is not int or reset_count < 0:
        raise ValueError("reset_count must be a non-negative integer")
    dimension = local_dimension ** subsystem_count
    flat_state = flatten_complex_matrix(state)
    if len(flat_state) != 2 * dimension * dimension:
        raise ValueError("state dimension does not match reset register")
    checked_targets = tuple(targets)
    if any(type(target) is not int for target in checked_targets):
        raise ValueError("reset targets must be integers")
    if any(type(value) not in (int, float) for value in success_probabilities):
        raise ValueError("success probabilities must be numeric")
    if any(type(value) not in (int, float) for row in target_populations for value in row):
        raise ValueError("target populations must be numeric")
    checked_successes = tuple(float(value) for value in success_probabilities)
    checked_populations = tuple(tuple(float(value) for value in row) for row in target_populations)
    if (not checked_targets or len(set(checked_targets)) != len(checked_targets)
            or any(target < 0 or target >= subsystem_count for target in checked_targets)):
        raise ValueError("reset targets must be unique and inside the register")
    if len(checked_successes) != len(checked_targets):
        raise ValueError("one success probability is required per reset target")
    if len(checked_populations) != len(checked_targets):
        raise ValueError("one target population vector is required per reset target")
    for success, populations in zip(checked_successes, checked_populations, strict=True):
        if not math.isfinite(success) or not 0.0 <= success <= 1.0:
            raise ValueError("success probabilities must be finite and between zero and one")
        if (len(populations) != local_dimension
                or any(not math.isfinite(value) or value < 0.0 for value in populations)
                or not math.isclose(sum(populations), 1.0, rel_tol=0.0, abs_tol=1e-12)):
            raise ValueError("target populations must be a normalized local state")

    flat_populations = [value for row in checked_populations for value in row]
    rust_module = _load_rust_module()
    result = rust_module.apply_local_reset_sequence_flat(
        RUST_RESET_SEQUENCE_SCHEMA_VERSION,
        flat_state,
        subsystem_count,
        local_dimension,
        list(checked_targets),
        list(checked_successes),
        flat_populations,
        reset_count,
    )
    return unflatten_complex_matrix(result, dimension)


def rust_binary_contrast_statistics(
    schema_version: str,
    observation_source: str,
    trials_0: int,
    outcome_one_0: int,
    trials_1: int,
    outcome_one_1: int,
) -> tuple[float, float, float, float, float | None, float, float]:
    """Compute generic binary contrast metrics from approved aggregate counts."""

    if type(schema_version) is not str or schema_version != "security_binary_contrast_v1":
        raise ValueError("unsupported binary contrast schema version")
    if type(observation_source) is not str or observation_source not in (
        "actor_visible", "service_published",
    ):
        raise ValueError("binary analysis requires an approved observation source")
    for name, value in (("trials_0", trials_0), ("trials_1", trials_1)):
        if type(value) is not int or not 1 <= value <= 2**53:
            raise ValueError(f"{name} must be a positive exactly representable integer")
    for name, value, trials in (("outcome_one_0", outcome_one_0, trials_0),
                                ("outcome_one_1", outcome_one_1, trials_1)):
        if type(value) is not int or not 0 <= value <= trials:
            raise ValueError(f"{name} must be between zero and its trial count")
    rust_module = _load_rust_module()
    operation = getattr(rust_module, "binary_contrast_statistics_v1", None)
    if not callable(operation):
        raise RuntimeError("Rust extension does not implement security_binary_contrast_v1")
    result = operation(
        schema_version, observation_source, trials_0, outcome_one_0,
        trials_1, outcome_one_1,
    )
    return tuple(result)


def rust_reset_training_analysis(
    observations: Sequence[tuple[int, int, int]],
) -> tuple[int, int, int, int, int, int, float, bool, float, float, float, float, float | None, float, float]:
    """Pool labelled public results and calculate Reset metrics in one Rust call."""

    normalized: list[tuple[int, int, int]] = []
    for label, shots, ones in observations:
        if type(label) is not int or label not in (0, 1):
            raise ValueError("reset training labels must be zero or one")
        if type(shots) is not int or not 1 <= shots <= 2**53:
            raise ValueError("reset training shots must be positive exact integers")
        if type(ones) is not int or not 0 <= ones <= shots:
            raise ValueError("reset training outcomes must be between zero and shots")
        normalized.append((label, shots, ones))
    if not normalized:
        raise ValueError("reset training observations must not be empty")
    rust_module = _load_rust_module()
    operation = getattr(rust_module, "reset_training_analysis_v1", None)
    if not callable(operation):
        raise RuntimeError("Rust extension does not implement reset_training_analysis_v1")
    result = operation("security_binary_contrast_v1", normalized)
    if not isinstance(result, dict):
        raise RuntimeError("Rust reset analysis returned an invalid result shape")
    counts = result.get("observation_count_by_label")
    shots = result.get("shots_by_label")
    ones = result.get("outcome_one_by_label")
    probabilities = result.get("probability_one_by_class")
    if any(not isinstance(values, list) or len(values) != 2 for values in (counts, shots, ones, probabilities)):
        raise RuntimeError("Rust reset analysis returned invalid class aggregates")
    return (
        counts[0], counts[1], shots[0], ones[0], shots[1], ones[1],
        result["threshold"], result["predict_one_when_at_or_above"],
        probabilities[0], probabilities[1],
        result["difference_class_1_minus_class_0"], result["standard_error"],
        result["z_score"], result["balanced_prior_mutual_information_bits"],
        result["balanced_prior_bayes_accuracy"],
    )


def rust_gksl_exponential_superoperator(
    hamiltonian: Sequence[Sequence[complex]],
    collapse_ops: Sequence[Sequence[Sequence[complex]]],
    duration_us: float,
) -> Matrix:
    """Build one finite-time GKSL superoperator in the Rust kernel."""

    dimension = _validate_square_matrix(hamiltonian, "hamiltonian")
    for index, operator in enumerate(collapse_ops):
        op_dimension = _validate_square_matrix(
            operator,
            f"collapse_ops[{index}]",
        )
        if op_dimension != dimension:
            raise ValueError(
                "collapse operators must match hamiltonian dimension"
            )
    duration = float(duration_us)
    if not math.isfinite(duration) or duration < 0.0:
        raise ValueError("duration_us must be finite and non-negative")

    rust_module = _load_rust_module()
    result = rust_module.gksl_exponential_superoperator_flat(
        flatten_complex_matrix(hamiltonian),
        flatten_collapse_ops(collapse_ops),
        len(collapse_ops),
        dimension,
        duration,
    )
    return unflatten_complex_matrix(result, dimension * dimension)


def rust_gksl_piecewise_superoperator(
    hamiltonians: Sequence[Sequence[Sequence[complex]]],
    interval_durations_us: Sequence[float],
    collapse_ops: Sequence[Sequence[Sequence[complex]]],
) -> Matrix:
    """Build a time-ordered piecewise GKSL superoperator in Rust."""

    if not hamiltonians:
        raise ValueError("hamiltonians must be non-empty")
    if len(interval_durations_us) != len(hamiltonians):
        raise ValueError(
            "interval_durations_us length must match hamiltonians"
        )
    dimension = _validate_square_matrix(
        hamiltonians[0],
        "hamiltonians[0]",
    )
    flat_hamiltonians: list[float] = []
    durations: list[float] = []
    for index, hamiltonian in enumerate(hamiltonians):
        h_dimension = _validate_square_matrix(
            hamiltonian,
            f"hamiltonians[{index}]",
        )
        if h_dimension != dimension:
            raise ValueError(
                "hamiltonian dimension must remain constant"
            )
        flat_hamiltonians.extend(flatten_complex_matrix(hamiltonian))
        duration = float(interval_durations_us[index])
        if not math.isfinite(duration) or duration <= 0.0:
            raise ValueError(
                "interval durations must be finite and positive"
            )
        durations.append(duration)
    for index, operator in enumerate(collapse_ops):
        op_dimension = _validate_square_matrix(
            operator,
            f"collapse_ops[{index}]",
        )
        if op_dimension != dimension:
            raise ValueError(
                "collapse operators must match hamiltonian dimension"
            )

    rust_module = _load_rust_module()
    result = rust_module.gksl_piecewise_superoperator_flat(
        flat_hamiltonians,
        durations,
        len(hamiltonians),
        flatten_collapse_ops(collapse_ops),
        len(collapse_ops),
        dimension,
    )
    return unflatten_complex_matrix(result, dimension * dimension)


def rust_rk4_time_dependent_stages(
    rho: Sequence[Sequence[complex]],
    hamiltonian_stages: Sequence[Sequence[Sequence[complex]]],
    collapse_ops: Sequence[Sequence[Sequence[complex]]],
    dt: float,
) -> tuple[Matrix, Matrix, Matrix, Matrix]:
    """Return raw RK4 derivatives for four explicitly supplied Hamiltonians."""

    dimension = _validate_time_dependent_inputs(
        rho,
        hamiltonian_stages,
        collapse_ops,
        dt,
    )
    rust_module = _load_rust_module()
    flat_stages = rust_module.rk4_time_dependent_stages_flat(
        flatten_complex_matrix(rho),
        *(flatten_complex_matrix(stage) for stage in hamiltonian_stages),
        flatten_collapse_ops(collapse_ops),
        len(collapse_ops),
        dimension,
        float(dt),
    )
    matrix_size = 2 * dimension * dimension
    if len(flat_stages) != 4 * matrix_size:
        raise RuntimeError(
            "yuragi_strider_rust returned an unexpected RK4 stage length"
        )
    return (
        unflatten_complex_matrix(flat_stages[0:matrix_size], dimension),
        unflatten_complex_matrix(
            flat_stages[matrix_size:2 * matrix_size],
            dimension,
        ),
        unflatten_complex_matrix(
            flat_stages[2 * matrix_size:3 * matrix_size],
            dimension,
        ),
        unflatten_complex_matrix(
            flat_stages[3 * matrix_size:4 * matrix_size],
            dimension,
        ),
    )


def rust_rk4_time_dependent_step(
    rho: Sequence[Sequence[complex]],
    hamiltonian_stages: Sequence[Sequence[Sequence[complex]]],
    collapse_ops: Sequence[Sequence[Sequence[complex]]],
    dt: float,
) -> Matrix:
    """Advance one raw RK4 step from four explicitly supplied Hamiltonians."""

    dimension = _validate_time_dependent_inputs(
        rho,
        hamiltonian_stages,
        collapse_ops,
        dt,
    )
    rust_module = _load_rust_module()
    result = rust_module.rk4_time_dependent_step_flat(
        flatten_complex_matrix(rho),
        *(flatten_complex_matrix(stage) for stage in hamiltonian_stages),
        flatten_collapse_ops(collapse_ops),
        len(collapse_ops),
        dimension,
        float(dt),
    )
    return unflatten_complex_matrix(result, dimension)


def rust_rk4_evolve_segment(
    rho: Sequence[Sequence[complex]],
    hamiltonian: Sequence[Sequence[complex]],
    collapse_ops: Sequence[Sequence[Sequence[complex]]],
    dt: float,
    substeps: int,
) -> Matrix:
    """Evolve one raw RK4 segment with the optional Rust dense kernel."""

    if int(substeps) != substeps or int(substeps) <= 0:
        raise ValueError("substeps must be a positive integer")
    dt = float(dt)
    if not math.isfinite(dt):
        raise ValueError("dt must be finite")

    dimension = _validate_square_matrix(rho, "rho")
    h_dimension = _validate_square_matrix(hamiltonian, "hamiltonian")
    if h_dimension != dimension:
        raise ValueError("rho and hamiltonian must have the same dimension")

    for index, operator in enumerate(collapse_ops):
        op_dimension = _validate_square_matrix(operator, f"collapse_ops[{index}]")
        if op_dimension != dimension:
            raise ValueError("collapse operators must match rho dimension")

    rust_module = _load_rust_module()
    result = rust_module.rk4_evolve_flat(
        flatten_complex_matrix(rho),
        flatten_complex_matrix(hamiltonian),
        flatten_collapse_ops(collapse_ops),
        len(collapse_ops),
        dimension,
        dt,
        int(substeps),
    )
    return unflatten_complex_matrix(result, dimension)


def rust_rk4_evolve_segment_cleaned(
    rho: Sequence[Sequence[complex]],
    hamiltonian: Sequence[Sequence[complex]],
    collapse_ops: Sequence[Sequence[Sequence[complex]]],
    dt: float,
    substeps: int,
) -> Matrix:
    """Evolve one RK4 segment with Rust cleanup after every substep."""

    if int(substeps) != substeps or int(substeps) <= 0:
        raise ValueError("substeps must be a positive integer")
    dt = float(dt)
    if not math.isfinite(dt):
        raise ValueError("dt must be finite")

    dimension = _validate_square_matrix(rho, "rho")
    h_dimension = _validate_square_matrix(hamiltonian, "hamiltonian")
    if h_dimension != dimension:
        raise ValueError("rho and hamiltonian must have the same dimension")

    for index, operator in enumerate(collapse_ops):
        op_dimension = _validate_square_matrix(operator, f"collapse_ops[{index}]")
        if op_dimension != dimension:
            raise ValueError("collapse operators must match rho dimension")

    rust_module = _load_rust_module()
    result = rust_module.rk4_evolve_cleaned_flat(
        flatten_complex_matrix(rho),
        flatten_complex_matrix(hamiltonian),
        flatten_collapse_ops(collapse_ops),
        len(collapse_ops),
        dimension,
        dt,
        int(substeps),
    )
    return unflatten_complex_matrix(result, dimension)


def rust_rk4_evolve_segment_samples(
    rho: Sequence[Sequence[complex]],
    hamiltonian: Sequence[Sequence[complex]],
    collapse_ops: Sequence[Sequence[Sequence[complex]]],
    dt: float,
    sample_substeps: Sequence[int],
) -> tuple[Matrix, ...]:
    """Evolve one segment and return cleaned states at requested samples."""

    sample_substeps = _validate_sample_substeps(sample_substeps)
    dt = float(dt)
    if not math.isfinite(dt):
        raise ValueError("dt must be finite")

    dimension = _validate_square_matrix(rho, "rho")
    h_dimension = _validate_square_matrix(hamiltonian, "hamiltonian")
    if h_dimension != dimension:
        raise ValueError("rho and hamiltonian must have the same dimension")

    for index, operator in enumerate(collapse_ops):
        op_dimension = _validate_square_matrix(operator, f"collapse_ops[{index}]")
        if op_dimension != dimension:
            raise ValueError("collapse operators must match rho dimension")

    rust_module = _load_rust_module()
    result = rust_module.rk4_evolve_cleaned_samples_flat(
        flatten_complex_matrix(rho),
        flatten_complex_matrix(hamiltonian),
        flatten_collapse_ops(collapse_ops),
        len(collapse_ops),
        dimension,
        dt,
        list(sample_substeps),
    )
    matrix_size = 2 * dimension * dimension
    expected = len(sample_substeps) * matrix_size
    if len(result) != expected:
        raise RuntimeError(
            "yuragi_strider_rust returned an unexpected sampled output length"
        )
    return tuple(
        unflatten_complex_matrix(result[start:start + matrix_size], dimension)
        for start in range(0, expected, matrix_size)
    )


def _load_rust_module():
    try:
        return importlib.import_module("yuragi_strider_rust")
    except Exception as exc:
        raise RuntimeError("yuragi_strider_rust is not available") from exc


def _validate_evolution_inputs(
    rho: Sequence[Sequence[complex]],
    hamiltonian: Sequence[Sequence[complex]],
    collapse_ops: Sequence[Sequence[Sequence[complex]]],
) -> int:
    dimension = _validate_square_matrix(rho, "rho")
    h_dimension = _validate_square_matrix(hamiltonian, "hamiltonian")
    if h_dimension != dimension:
        raise ValueError("rho and hamiltonian must have the same dimension")
    for index, operator in enumerate(collapse_ops):
        op_dimension = _validate_square_matrix(operator, f"collapse_ops[{index}]")
        if op_dimension != dimension:
            raise ValueError("collapse operators must match rho dimension")
    return dimension


def _validate_time_dependent_inputs(
    rho: Sequence[Sequence[complex]],
    hamiltonian_stages: Sequence[Sequence[Sequence[complex]]],
    collapse_ops: Sequence[Sequence[Sequence[complex]]],
    dt: float,
) -> int:
    if len(hamiltonian_stages) != 4:
        raise ValueError("hamiltonian_stages must contain exactly four matrices")
    dt = float(dt)
    if not math.isfinite(dt):
        raise ValueError("dt must be finite")
    dimension = _validate_square_matrix(rho, "rho")
    for index, hamiltonian in enumerate(hamiltonian_stages):
        h_dimension = _validate_square_matrix(
            hamiltonian,
            f"hamiltonian_stages[{index}]",
        )
        if h_dimension != dimension:
            raise ValueError("Hamiltonian stages must match rho dimension")
    for index, operator in enumerate(collapse_ops):
        op_dimension = _validate_square_matrix(operator, f"collapse_ops[{index}]")
        if op_dimension != dimension:
            raise ValueError("collapse operators must match rho dimension")
    return dimension


def _validate_dimension(d: int) -> int:
    if isinstance(d, bool):
        raise ValueError("d must be a positive integer")
    try:
        d = int(d)
    except (TypeError, ValueError) as exc:
        raise ValueError("d must be a positive integer") from exc
    if d <= 0:
        raise ValueError("d must be greater than 0")
    return d


def _validate_square_matrix(matrix: Sequence[Sequence[complex]], name: str) -> int:
    if not isinstance(matrix, Sequence) or isinstance(matrix, (str, bytes)):
        raise ValueError(f"{name} must be a square matrix")
    dimension = len(matrix)
    if dimension <= 0:
        raise ValueError(f"{name} must be non-empty")
    for row_index, row in enumerate(matrix):
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
            raise ValueError(f"{name}[{row_index}] must be a row sequence")
        if len(row) != dimension:
            raise ValueError(f"{name} must be square and non-ragged")
        for value in row:
            try:
                complex(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} entries must be complex-compatible") from exc
    return dimension


def _validate_sample_substeps(sample_substeps: Sequence[int]) -> tuple[int, ...]:
    if not isinstance(sample_substeps, Sequence) or isinstance(sample_substeps, (str, bytes)):
        raise ValueError("sample_substeps must be a sequence of positive integers")
    if len(sample_substeps) == 0:
        raise ValueError("sample_substeps must be non-empty")
    values: list[int] = []
    for index, value in enumerate(sample_substeps):
        if isinstance(value, bool):
            raise ValueError("sample_substeps entries must be positive integers")
        try:
            integer = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("sample_substeps entries must be positive integers") from exc
        if integer != value or integer <= 0:
            raise ValueError("sample_substeps entries must be positive integers")
        values.append(integer)
    return tuple(values)
