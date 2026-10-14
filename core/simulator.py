"""Stable core simulation entry point."""

from __future__ import annotations

from bisect import bisect_right
import math
from random import Random
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from threading import local
from time import perf_counter

import numpy as np

from core.backend_boundary import (
    PYTHON_DENSE_BACKEND,
    PYTHON_DENSE_BACKEND_NAME,
    RUST_DENSE_PREVIEW_BACKEND,
    backend_metadata,
)
from core.capabilities import (
    DEFAULT_SIMULATION_MODEL,
    GATE_AWARE_HAMILTONIAN_LINDBLAD_MODEL,
    GATE_AWARE_SPLIT_STEP_MODEL,
    MAX_DENSITY_MATRIX_QUBITS,
    POST_CIRCUIT_DEGRADATION_MODEL,
)
from core.complexity import complexity_diagnostics
from core.dense_numpy import evolve_segment_numpy, should_use_numpy_dense
from core.errors import ValidationIssue
from core.gates import (
    CachedCollapseOperator,
    Matrix,
    apply_non_selective_computational_measurement,
    apply_readout_error,
    apply_unitary_to_density,
    clean_density_matrix,
    column_duration_us,
    effective_hamiltonian_from_unitary,
    effective_hamiltonian_from_involution,
    gate_unitary,
    identity_matrix,
    initial_density_matrix,
    matmul,
    multi_qubit_environment_collapse_operators,
    output_probabilities,
    prepare_collapse_operators,
    rk4_step_cached,
    trace,
    unitary_from_hamiltonian,
    zero_hamiltonian,
)
from core.classical_branching import execute_classical_branches
from core.execution_representation import (
    representation_diagnostics,
    select_execution_representation,
)
from core.gate_compiler import GateCompilationResult, compile_gate_aware_circuit
from core.evolution_methods import EXPLICIT_CPTP, FIXED_STEP_RK4
from core.gate_aware_cptp import (
    GATE_AWARE_CPTP_EVOLUTION_ID,
    GateAwareCPTPEvolver,
)
from core.internal_profiling import active_internal_profile
from core.metrics import effective_time
from core.physical_environment import (
    EnvironmentRates,
    SUPPORTED_ENVIRONMENT_MODELS,
    UNIFIED_ENVIRONMENT_MODEL,
    compute_environment_rates,
    compute_environment_rates_per_qubit,
    environment_rates_to_derived_parameters,
    per_qubit_rates_to_derived_parameters,
)
from core.results import SimulationConfig, SimulationResult
from core.rust_dense_kernel import (
    RustDenseSession,
)
from core.simulation_backends import (
    get_simulation_backend,
    register_simulation_backend,
    registered_simulation_models,
)
from core.state_snapshots import (
    SnapshotPlan,
    StateSnapshot,
    StateSnapshotCollector,
    build_snapshot_plan,
    idle_sample_times,
    is_planned_time,
)


_RUST_SESSION_CACHE_LOCAL = local()
_RUST_SESSION_CACHE_LIMIT = 8
from core.physical_timeline import build_physical_timeline
from core.statevector import execute_statevector_branches, statevector_branch_purity
from core.validation import (
    diagnose_simulation_result,
    has_blocking_issues,
    validate_simulation_config,
)


MAX_RK4_RATE_STEP_PRODUCT = 1.0
MAX_GENERATOR_STEP_PRODUCT = 1.0
@dataclass
class _SimulationSeries:
    times: list[float]
    fidelity: list[float]
    purity: list[float]
    final_noisy_state: Matrix
    final_ideal_state: Matrix
    metadata: dict[str, object]
    state_snapshots: list[StateSnapshot]


@dataclass
class _SimulationCaches:
    gate_unitaries: dict[tuple[object, ...], Matrix]
    column_unitaries: dict[tuple[object, ...], Matrix]
    hamiltonians: dict[tuple[object, ...], Matrix]

    @classmethod
    def empty(cls) -> "_SimulationCaches":
        return cls(gate_unitaries={}, column_unitaries={}, hamiltonians={})


@dataclass
class _CoreProfilingStats:
    dimension: int = 0
    density_matrix_shape: str = ""
    segments_count: int = 0
    idle_segments_count: int = 0
    gate_segments_count: int = 0
    total_segment_duration_us: float = 0.0
    total_idle_duration_us: float = 0.0
    total_gate_duration_us: float = 0.0
    time_steps: int = 0
    total_rk4_substeps: int = 0
    total_rhs_evaluations: int = 0
    collapse_operator_count: int = 0
    lindblad_operator_build_ms: float = 0.0
    segment_setup_ms: float = 0.0
    idle_evolution_ms: float = 0.0
    gate_evolution_ms: float = 0.0
    output_probabilities_ms: float = 0.0
    diagnostics_build_ms: float = 0.0
    dense_execution_engine: str = "python_tuple_v1"
    zero_hamiltonian_fast_path_used: bool = False
    has_gate_segments: bool = False
    has_idle_after_circuit: bool = False
    idle_only: bool = False

    @property
    def total_evolution_ms(self) -> float:
        return self.idle_evolution_ms + self.gate_evolution_ms

    def to_diagnostics(self) -> dict[str, object]:
        rk4_step_count = int(self.total_rk4_substeps)
        rhs_call_count = 4 * rk4_step_count
        lindblad_term_evaluation_count = rhs_call_count * int(self.collapse_operator_count)
        return {
            "core_dimension": int(self.dimension),
            "core_density_matrix_shape": self.density_matrix_shape,
            "core_segments_count": int(self.segments_count),
            "core_idle_segments_count": int(self.idle_segments_count),
            "core_gate_segments_count": int(self.gate_segments_count),
            "core_total_segment_duration_us": float(self.total_segment_duration_us),
            "core_total_idle_duration_us": float(self.total_idle_duration_us),
            "core_total_gate_duration_us": float(self.total_gate_duration_us),
            "core_time_steps": int(self.time_steps),
            "core_total_rk4_substeps": rk4_step_count,
            "core_total_rhs_evaluations": int(self.total_rhs_evaluations),
            "core_collapse_operator_count": int(self.collapse_operator_count),
            "core_lindblad_operator_build_ms": float(self.lindblad_operator_build_ms),
            "core_segment_setup_ms": float(self.segment_setup_ms),
            "core_idle_evolution_ms": float(self.idle_evolution_ms),
            "core_gate_evolution_ms": float(self.gate_evolution_ms),
            "core_total_evolution_ms": float(self.total_evolution_ms),
            "core_output_probabilities_ms": float(self.output_probabilities_ms),
            "core_diagnostics_build_ms": float(self.diagnostics_build_ms),
            "core_dense_execution_engine": self.dense_execution_engine,
            "core_zero_hamiltonian_fast_path_used": bool(
                self.zero_hamiltonian_fast_path_used
            ),
            "core_rk4_step_count": rk4_step_count,
            "core_rhs_call_count": rhs_call_count,
            "core_commutator_evaluation_count": rhs_call_count,
            "core_lindblad_term_evaluation_count": lindblad_term_evaluation_count,
            "core_idle_only": bool(self.idle_only),
            "core_has_gate_segments": bool(self.has_gate_segments),
            "core_has_idle_after_circuit": bool(self.has_idle_after_circuit),
        }


@dataclass
class _KernelStats:
    requested_backend: str
    rust_kernel_used: bool = False
    rust_kernel_mode: str = "none"
    rust_kernel_fallback_used: bool = False
    rust_kernel_fallback_reason: str = ""
    rust_kernel_call_count: int = 0
    rust_kernel_segment_count: int = 0
    rust_kernel_substep_count: int = 0
    rust_kernel_batchable_interval_count: int = 0
    rust_kernel_actual_batch_count: int = 0
    rust_kernel_total_batch_substeps: int = 0
    rust_kernel_max_batch_substeps: int = 0
    rust_kernel_batch_blocked_by_sampling_count: int = 0
    rust_kernel_batch_blocked_by_boundary_count: int = 0
    rust_kernel_sampled_batch_count: int = 0
    rust_kernel_sampled_returned_state_count: int = 0
    rust_kernel_max_sampled_batch_outputs: int = 0
    rust_kernel_sampled_batch_fallback_count: int = 0
    rust_kernel_sampled_batch_fallback_reason: str = ""
    python_kernel_segment_count: int = 0
    python_kernel_substep_count: int = 0
    rust_kernel_session_count: int = 0
    rust_kernel_session_reuse_count: int = 0
    rust_kernel_piecewise_segment_count: int = 0
    rust_sessions: dict[tuple[object, ...], RustDenseSession] = field(
        default_factory=dict,
        repr=False,
    )

    @property
    def wants_rust(self) -> bool:
        return (
            self.requested_backend == RUST_DENSE_PREVIEW_BACKEND
            and not self.rust_kernel_fallback_used
        )

    def to_diagnostics(self) -> dict[str, object]:
        rust_kernel_mode = self.rust_kernel_mode
        if self.requested_backend != RUST_DENSE_PREVIEW_BACKEND:
            rust_kernel_mode = "none"
        elif self.rust_kernel_fallback_used and not self.rust_kernel_used:
            rust_kernel_mode = "fallback_python"
        mean_batch_substeps = (
            self.rust_kernel_total_batch_substeps / self.rust_kernel_actual_batch_count
            if self.rust_kernel_actual_batch_count
            else 0.0
        )
        mean_sampled_outputs = (
            self.rust_kernel_sampled_returned_state_count
            / self.rust_kernel_sampled_batch_count
            if self.rust_kernel_sampled_batch_count
            else 0.0
        )
        return {
            "rust_kernel_used": bool(
                self.rust_kernel_used and not self.rust_kernel_fallback_used
            ),
            "rust_kernel_mode": rust_kernel_mode,
            "rust_kernel_fallback_used": bool(self.rust_kernel_fallback_used),
            "rust_kernel_fallback_reason": self.rust_kernel_fallback_reason,
            "rust_kernel_call_count": float(self.rust_kernel_call_count),
            "rust_kernel_segment_count": float(self.rust_kernel_segment_count),
            "rust_kernel_substep_count": float(self.rust_kernel_substep_count),
            "rust_kernel_session_count": float(self.rust_kernel_session_count),
            "rust_kernel_session_reuse_count": float(self.rust_kernel_session_reuse_count),
            "rust_kernel_piecewise_segment_count": float(self.rust_kernel_piecewise_segment_count),
            "rust_kernel_batchable_interval_count": float(
                self.rust_kernel_batchable_interval_count
            ),
            "rust_kernel_actual_batch_count": float(self.rust_kernel_actual_batch_count),
            "rust_kernel_max_batch_substeps": float(self.rust_kernel_max_batch_substeps),
            "rust_kernel_mean_batch_substeps": float(mean_batch_substeps),
            "rust_kernel_batch_blocked_by_sampling_count": float(
                self.rust_kernel_batch_blocked_by_sampling_count
            ),
            "rust_kernel_batch_blocked_by_boundary_count": float(
                self.rust_kernel_batch_blocked_by_boundary_count
            ),
            "rust_kernel_sampled_batch_count": float(
                self.rust_kernel_sampled_batch_count
            ),
            "rust_kernel_sampled_returned_state_count": float(
                self.rust_kernel_sampled_returned_state_count
            ),
            "rust_kernel_max_sampled_batch_outputs": float(
                self.rust_kernel_max_sampled_batch_outputs
            ),
            "rust_kernel_mean_sampled_batch_outputs": float(mean_sampled_outputs),
            "rust_kernel_sampled_batch_fallback_count": float(
                self.rust_kernel_sampled_batch_fallback_count
            ),
            "rust_kernel_sampled_batch_fallback_reason": (
                self.rust_kernel_sampled_batch_fallback_reason
            ),
            "python_kernel_segment_count": float(self.python_kernel_segment_count),
            "python_kernel_substep_count": float(self.python_kernel_substep_count),
        }

    def record_rust_batch(
        self,
        substeps: int,
        *,
        blocked_by_sampling: bool,
        blocked_by_boundary: bool,
    ) -> None:
        self.rust_kernel_batchable_interval_count += 1
        self.rust_kernel_actual_batch_count += 1
        self.rust_kernel_total_batch_substeps += int(substeps)
        self.rust_kernel_max_batch_substeps = max(
            self.rust_kernel_max_batch_substeps,
            int(substeps),
        )
        if blocked_by_sampling:
            self.rust_kernel_batch_blocked_by_sampling_count += 1
        if blocked_by_boundary:
            self.rust_kernel_batch_blocked_by_boundary_count += 1

    def record_sampled_batch(
        self,
        returned_state_count: int,
        total_substeps: int,
        *,
        blocked_by_sampling: bool,
        blocked_by_boundary: bool,
    ) -> None:
        self.rust_kernel_used = True
        self.rust_kernel_mode = "sampled_cleaned_multi_output"
        self.rust_kernel_call_count += 1
        self.rust_kernel_segment_count += 1
        self.rust_kernel_substep_count += int(total_substeps)
        self.rust_kernel_sampled_batch_count += 1
        self.rust_kernel_sampled_returned_state_count += int(returned_state_count)
        self.rust_kernel_max_sampled_batch_outputs = max(
            self.rust_kernel_max_sampled_batch_outputs,
            int(returned_state_count),
        )
        self.record_rust_batch(
            int(total_substeps),
            blocked_by_sampling=blocked_by_sampling,
            blocked_by_boundary=blocked_by_boundary,
        )

    def record_sampled_batch_fallback(self, reason: str) -> None:
        self.rust_kernel_sampled_batch_fallback_count += 1
        self.rust_kernel_sampled_batch_fallback_reason = str(reason)


class _LazyEnvironmentCollapseOperators(Sequence[CachedCollapseOperator]):
    """Materialize dense Python operators only when a fallback actually uses them."""

    def __init__(self, n_qubits: int, rates: EnvironmentRates) -> None:
        self.n_qubits = int(n_qubits)
        self.rates = rates
        self._materialized: tuple[CachedCollapseOperator, ...] | None = None

    def __len__(self) -> int:
        active_rates = sum(
            rate > 0.0
            for rate in (
                self.rates.gamma_down_per_us,
                self.rates.gamma_up_per_us,
                self.rates.gamma_phi_per_us,
            )
        )
        return self.n_qubits * active_rates

    def __getitem__(self, index):
        if self._materialized is None:
            self._materialized = prepare_collapse_operators(
                multi_qubit_environment_collapse_operators(self.n_qubits, self.rates)
            )
        return self._materialized[index]

def run_simulation(config: SimulationConfig) -> SimulationResult:
    """Run a simulation through the stable Config -> Result core contract."""

    if isinstance(config, Mapping):
        config = SimulationConfig.from_dict(config)
    if not isinstance(config, SimulationConfig):
        raise TypeError("config must be a SimulationConfig")

    config_issues = validate_simulation_config(config)
    runtime_issues = (
        _runtime_issues(config)
        if not has_blocking_issues(config_issues)
        else []
    )
    blocking_issues = [*config_issues, *runtime_issues]
    if has_blocking_issues(blocking_issues):
        return _empty_result(config, blocking_issues)

    runner = get_simulation_backend(config.model)
    if runner is None:
        return _empty_result(config, [
            _runtime_error(
                "BACKEND_NOT_REGISTERED",
                "No simulation backend is registered for this model.",
                f"Received model={config.model!r}",
                (
                    "Register a backend runner or use one of "
                    f"{registered_simulation_models()}."
                ),
            )
        ])

    representation = select_execution_representation(
        logical_qubits=config.circuit.logical_qubits,
        environment_is_ideal=_environment_is_ideal(config),
        has_classical_conditions=_has_classical_conditions(config),
        has_measurements=_has_measurements(config),
        evolution_method=config.evolution_method,
    )
    try:
        compilation = (
            GateCompilationResult(
                circuit=config.circuit,
                diagnostics={"compilation_skipped_for_representation": representation},
            )
            if representation == "statevector"
            else compile_gate_aware_circuit(
                config.circuit,
                config.compilation_mode,
                config.native_gate_durations_us,
            )
        )
    except ValueError as exc:
        return _empty_result(config, [
            _runtime_error(
                "GATE_COMPILATION_FAILED",
                "Gate-aware circuit compilation failed.",
                str(exc),
                "Use logical direct mode or correct the advanced gate operands.",
            )
        ])

    execution_config = SimulationConfig.from_dict(config.to_dict())
    execution_config.circuit = compilation.circuit
    main_method_fallback = None
    if (
        config.evolution_method == EXPLICIT_CPTP
        and config.circuit.logical_qubits > 4
        and _has_classical_conditions(config)
        and representation != "statevector"
    ):
        execution_config.evolution_method = FIXED_STEP_RK4
        main_method_fallback = (
            "5-qubit conditional circuits use RK4 for the main trajectory because "
            "explicit CPTP Choi auditing is prohibitively large at dimension 32."
        )
    if representation == "statevector":
        result = SimulationResult(
            config=execution_config,
            times=[0.0, float(config.duration_us)],
            fidelity=[1.0, 1.0],
            purity=[1.0, 1.0],
            effective_operation_time_us=float(config.duration_us),
            diagnostics={"statevector_timeline_mode": "endpoint_only_v1"},
        )
    else:
        result = runner(execution_config)
    if main_method_fallback is not None:
        result.diagnostics["evolution_method_fallback"] = main_method_fallback
    result.diagnostics.update(
        representation_diagnostics(representation, config.circuit.logical_qubits)
    )
    if representation == "density_matrix" and config.circuit.logical_qubits > 5:
        result.diagnostics["large_density_matrix_execution"] = True
    if representation == "statevector":
        try:
            statevector = execute_statevector_branches(config.circuit)
            observed = _observed_probabilities(
                statevector.output_probabilities,
                config,
            )
            result.output_probabilities = observed
            result.purity[-1] = statevector_branch_purity(statevector)
            result.measurement_counts = _sample_measurement_counts(
                observed,
                config.measurement_shots,
                config.measurement_seed,
            )
            if _has_classical_conditions(config):
                result.classical_branch_records = statevector.branches
                result.classical_shot_preview = _sample_classical_shot_preview(
                    statevector.branches,
                    config.measurement_shots,
                    config.measurement_seed,
                )
                result.diagnostics.update({
                    "classical_branching_mode": "statevector_branching_v1",
                    "classical_branch_count": len(statevector.branches),
                    "classical_branching_noise_applied": False,
                })
        except ValueError as exc:
            return _empty_result(config, [
                _runtime_error(
                    "STATEVECTOR_EXECUTION_FAILED",
                    "Adaptive statevector execution failed.",
                    str(exc),
                    "Use a supported initial state and gate set.",
                )
            ])
    if _has_classical_conditions(config) and representation != "statevector":
        try:
            _, _, branch_rates = _resolved_environment_rates(config)
            branch_evolution_method = config.evolution_method
            branch_method_fallback = None
            if (
                config.evolution_method == EXPLICIT_CPTP
                and config.circuit.logical_qubits > 4
            ):
                branch_evolution_method = FIXED_STEP_RK4
                branch_method_fallback = (
                    "5-qubit branch CPTP was bounded to RK4 because Choi audit "
                    "scales as (2^n)^2; the requested main trajectory remains CPTP."
                )
            branching = execute_classical_branches(
                config.circuit,
                environment_rates=branch_rates,
                evolution_method=branch_evolution_method,
                simulation_backend=config.simulation_backend,
            )
            observed = _observed_probabilities(branching.output_probabilities, config)
            result.output_probabilities = observed
            result.measurement_counts = _sample_measurement_counts(
                observed,
                config.measurement_shots,
                config.measurement_seed,
            )
            result.classical_branch_records = branching.branches
            result.classical_shot_preview = _sample_classical_shot_preview(
                branching.branches,
                config.measurement_shots,
                config.measurement_seed,
            )
            result.diagnostics.update({
                "classical_branching_mode": "gate_aware_noisy_branching_v1",
                "classical_branch_count": len(branching.branches),
                "classical_branching_noise_applied": True,
                **branching.diagnostics,
            })
            if branch_method_fallback is not None:
                result.diagnostics["classical_branching_method_fallback"] = branch_method_fallback
        except ValueError as exc:
            return _empty_result(config, [
                _runtime_error(
                    "CLASSICAL_BRANCHING_FAILED",
                    "Classical branch execution failed.",
                    str(exc),
                    "Reduce mid-circuit measurement branching or use a circuit without conditions.",
                )
            ])
    result.physical_timeline = build_physical_timeline(
        execution_config.circuit,
        sampled_times_us=result.times,
        requested_duration_us=config.duration_us,
        source_map=compilation.diagnostics.get("source_map", []),
    )
    result.config = config
    result.diagnostics.update(compilation.diagnostics)
    _attach_backend_metadata(result, config)
    diagnostic_issues = diagnose_simulation_result(result)
    result.issues = diagnostic_issues
    result.warnings = _issue_warnings(diagnostic_issues)
    if representation == "density_matrix" and config.circuit.logical_qubits > 5:
        result.warnings.append(
            "6-8 qubit noisy simulation uses the exact dense density-matrix path; "
            "runtime and response size grow exponentially. Start with fixed-step "
            "RK4, a short duration, and a small time-step count."
        )
    if _has_classical_conditions(config):
        if representation == "statevector":
            result.warnings.append(
                "Ideal conditional circuit executed with the O(2^n) statevector representation."
            )
        else:
            result.warnings.append(
                "Conditional gates used bounded Gate-aware branch evolution; "
                "branch count is limited to protect runtime and memory."
            )
        if result.diagnostics.get("classical_branching_method_fallback"):
            result.warnings.append(str(result.diagnostics["classical_branching_method_fallback"]))
    return result


def environment_is_ideal(config: SimulationConfig) -> bool:
    """True when every local relaxation, excitation and dephasing rate is zero."""

    return _environment_is_ideal(config)


def _environment_is_ideal(config: SimulationConfig) -> bool:
    rates = compute_environment_rates_per_qubit(
        config.environment,
        config.circuit.logical_qubits,
    )
    return all(
        abs(float(getattr(local_rates, name))) <= 1e-15
        for local_rates in rates
        for name in ("gamma_down_per_us", "gamma_up_per_us", "gamma_phi_per_us")
    )


def _has_classical_conditions(config: SimulationConfig) -> bool:
    return any(
        gate.condition is not None
        for column in config.circuit.columns
        for gate in column.gates
    )


def _has_measurements(config: SimulationConfig) -> bool:
    return any(
        str(gate.type).upper() == "MEASURE"
        for column in config.circuit.columns
        for gate in column.gates
    )


def _observed_probabilities(
    probabilities: Mapping[str, float],
    config: SimulationConfig,
) -> dict[str, float]:
    """Apply the configured readout error to computational-basis probabilities.

    Readout error models the measurement apparatus, not the state, so it is
    applied to the observation stage only. Mid-circuit MEASURE projections and
    every state-level metric are left untouched.
    """

    readout_error = config.readout_error
    if readout_error is None or not readout_error.is_enabled:
        return dict(probabilities)
    return apply_readout_error(
        probabilities,
        config.circuit.logical_qubits,
        readout_error.assignment_errors(config.circuit.logical_qubits),
    )


def _run_weak_coupling_lindblad(config: SimulationConfig) -> SimulationResult:
    if config.duration_us < _total_gate_duration_us(config):
        return _run_post_circuit_degradation(config)
    return _run_gate_aware_hamiltonian_lindblad(config)


def _run_gate_aware_hamiltonian_lindblad(config: SimulationConfig) -> SimulationResult:
    """Current open-system gate-aware path.

    Future CPTP evolution should reuse the same gate/idle segment construction
    here rather than becoming a separate simulator branch.
    """
    profile = _core_profiling_stats(config)
    lindblad_started_at = perf_counter()
    rates, local_rates, solver_rates = _resolved_environment_rates(config)
    if (
        config.simulation_backend == RUST_DENSE_PREVIEW_BACKEND
        and config.evolution_method == FIXED_STEP_RK4
    ):
        cached_collapse_ops: Sequence[CachedCollapseOperator] = (
            _LazyEnvironmentCollapseOperators(config.circuit.logical_qubits, rates)
            if not config.environment.per_qubit
            else prepare_collapse_operators(
                multi_qubit_environment_collapse_operators(
                    config.circuit.logical_qubits, solver_rates
                )
            )
        )
    else:
        collapse_ops = multi_qubit_environment_collapse_operators(
            config.circuit.logical_qubits, solver_rates
        )
        cached_collapse_ops = prepare_collapse_operators(collapse_ops)
    profile.lindblad_operator_build_ms = (perf_counter() - lindblad_started_at) * 1000.0
    profile.collapse_operator_count = len(cached_collapse_ops)
    caches = _SimulationCaches.empty()
    kernel_stats = _KernelStats(config.simulation_backend)
    derived_parameters = environment_rates_to_derived_parameters(rates)
    derived_parameters["per_qubit"] = per_qubit_rates_to_derived_parameters(local_rates)
    derived_parameters["per_qubit_environment_enabled"] = bool(config.environment.per_qubit)

    try:
        simulation = _simulate_circuit_gate_aware_hamiltonian(
            config=config,
            duration_us=config.duration_us,
            time_steps=config.time_steps,
            collapse_ops=cached_collapse_ops,
            max_environment_rate_per_us=_max_environment_rate_per_us(solver_rates),
            caches=caches,
            kernel_stats=kernel_stats,
            profile=profile,
        )
    except ValueError as exc:
        return _empty_result(config, [
            _runtime_error(
                "GATE_AWARE_HAMILTONIAN_UNSUPPORTED",
                "Gate-aware effective Hamiltonian simulation could not run this circuit.",
                str(exc),
                (
                    "Use non-overlapping involutory gates per column or run "
                    f"the {POST_CIRCUIT_DEGRADATION_MODEL!r} comparison model."
                ),
            )
        ])
    times = simulation.times
    fidelities = simulation.fidelity
    purities = simulation.purity
    simulation_metadata = simulation.metadata
    # Explicit CPTP uses the Rust exponential kernel directly rather than the
    # RK4 helpers that normally update _KernelStats. Record that path here so
    # backend diagnostics do not report a false Python fallback.
    if simulation_metadata.get("cptp_backend") == "rust":
        kernel_stats.rust_kernel_used = True
        kernel_stats.rust_kernel_mode = "cptp_exponential"
        kernel_stats.rust_kernel_call_count = int(
            simulation_metadata.get("cptp_map_construction_count", 0)
        )
        kernel_stats.rust_kernel_segment_count = kernel_stats.rust_kernel_call_count
    derived_parameters.update(simulation_metadata)
    effective_operation_time_us = effective_time(
        times,
        fidelities,
        config.fidelity_threshold,
    )

    output_probabilities_started_at = perf_counter()
    output_distribution = _observed_probabilities(
        output_probabilities(
            simulation.final_noisy_state,
            config.circuit.logical_qubits,
        ),
        config,
    )
    profile.output_probabilities_ms = (
        perf_counter() - output_probabilities_started_at
    ) * 1000.0

    diagnostics_started_at = perf_counter()
    diagnostics = _diagnostics(
        times=times,
        fidelities=fidelities,
        purities=purities,
        fidelity_threshold=config.fidelity_threshold,
        integration_substeps=simulation_metadata["integration_substeps"],
        max_trace_error=simulation_metadata["max_trace_error"],
        extra={
            "backend_name": PYTHON_DENSE_BACKEND_NAME,
            "simulation_model": "gate_aware_open_system",
            "evolution_mode": GATE_AWARE_HAMILTONIAN_LINDBLAD_MODEL,
            "simulation_mode": simulation_metadata["simulation_mode"],
            "hamiltonian_mode": simulation_metadata["hamiltonian_mode"],
            "completion_time_us": simulation_metadata["completion_time_us"],
            "completion_fidelity": simulation_metadata["completion_fidelity"],
            "completion_purity": simulation_metadata["completion_purity"],
            "configured_duration_us": config.duration_us,
            "total_gate_duration_us": simulation_metadata["total_gate_duration_us"],
            "idle_duration_us": simulation_metadata["idle_duration_us"],
            "actual_duration_us": simulation_metadata["actual_duration_us"],
            "gate_duration_model": simulation_metadata["gate_duration_model"],
            "gate_aware_noise": 1.0,
            "post_circuit_degradation": 0.0,
            "evolution_method_requested": simulation_metadata[
                "evolution_method_requested"
            ],
            "evolution_method_resolved": simulation_metadata[
                "evolution_method_resolved"
            ],
            "evolution_method_id": simulation_metadata["evolution_method_id"],
            "cptp_guaranteed_by_construction": simulation_metadata[
                "cptp_guaranteed_by_construction"
            ],
            "cleanup_applied": simulation_metadata["cleanup_applied"],
            **{
                key: value
                for key, value in simulation_metadata.items()
                if key.startswith("cptp_")
            },
            "recorded_state_count": float(len(times)),
            "state_history_retained": 0.0,
            "state_history_storage_mode": "streaming_metrics_only",
            **_snapshot_diagnostics_from_metadata(simulation_metadata),
            **kernel_stats.to_diagnostics(),
        },
    )
    diagnostics.update(complexity_diagnostics(
        config,
        diagnostics=diagnostics,
        derived_parameters=derived_parameters,
    ))
    profile.diagnostics_build_ms = (perf_counter() - diagnostics_started_at) * 1000.0
    diagnostics.update(profile.to_diagnostics())
    internal_profile = active_internal_profile()
    if internal_profile is not None:
        diagnostics.update(internal_profile.to_diagnostics())

    result = SimulationResult(
        config=config,
        times=times,
        fidelity=fidelities,
        purity=purities,
        effective_operation_time_us=effective_operation_time_us,
        output_probabilities=output_distribution,
        measurement_counts=_sample_measurement_counts(
            output_distribution,
            config.measurement_shots,
            config.measurement_seed,
        ),
        derived_parameters=derived_parameters,
        diagnostics=diagnostics,
        warnings=[],
        issues=[],
        state_snapshots=simulation.state_snapshots,
    )
    return result


def _run_post_circuit_degradation(config: SimulationConfig) -> SimulationResult:
    profile = _core_profiling_stats(config)
    lindblad_started_at = perf_counter()
    rates, local_rates, solver_rates = _resolved_environment_rates(config)
    if (
        config.simulation_backend == RUST_DENSE_PREVIEW_BACKEND
        and config.evolution_method == FIXED_STEP_RK4
    ):
        cached_collapse_ops: Sequence[CachedCollapseOperator] = (
            _LazyEnvironmentCollapseOperators(config.circuit.logical_qubits, rates)
            if not config.environment.per_qubit
            else prepare_collapse_operators(
                multi_qubit_environment_collapse_operators(
                    config.circuit.logical_qubits, solver_rates
                )
            )
        )
    else:
        collapse_ops = multi_qubit_environment_collapse_operators(
            config.circuit.logical_qubits, solver_rates
        )
        cached_collapse_ops = prepare_collapse_operators(collapse_ops)
    profile.lindblad_operator_build_ms = (perf_counter() - lindblad_started_at) * 1000.0
    profile.collapse_operator_count = len(cached_collapse_ops)
    caches = _SimulationCaches.empty()
    kernel_stats = _KernelStats(config.simulation_backend)
    derived_parameters = environment_rates_to_derived_parameters(rates)
    derived_parameters["per_qubit"] = per_qubit_rates_to_derived_parameters(local_rates)
    derived_parameters["per_qubit_environment_enabled"] = bool(config.environment.per_qubit)
    derived_parameters.update({
        "backend_name": PYTHON_DENSE_BACKEND_NAME,
        "simulation_mode": POST_CIRCUIT_DEGRADATION_MODEL,
        "gate_aware_noise": False,
        "hamiltonian_mode": "none",
        "total_gate_duration_us": _total_gate_duration_us(config),
        "idle_duration_us": config.duration_us,
        "actual_duration_us": config.duration_us,
        "gate_duration_model": "default_gate_duration_us_with_params_override",
        "post_circuit_degradation": True,
    })

    simulation = _simulate_circuit_post_circuit(
        config=config,
        duration_us=config.duration_us,
        time_steps=config.time_steps,
        collapse_ops=cached_collapse_ops,
        max_environment_rate_per_us=_max_environment_rate_per_us(solver_rates),
        caches=caches,
        kernel_stats=kernel_stats,
        profile=profile,
    )
    times = simulation.times
    fidelities = simulation.fidelity
    purities = simulation.purity
    effective_operation_time_us = effective_time(
        times,
        fidelities,
        config.fidelity_threshold,
    )

    output_probabilities_started_at = perf_counter()
    output_distribution = _observed_probabilities(
        output_probabilities(
            simulation.final_noisy_state,
            config.circuit.logical_qubits,
        ),
        config,
    )
    profile.output_probabilities_ms = (
        perf_counter() - output_probabilities_started_at
    ) * 1000.0

    diagnostics_started_at = perf_counter()
    diagnostics = _diagnostics(
        times=times,
        fidelities=fidelities,
        purities=purities,
        fidelity_threshold=config.fidelity_threshold,
        integration_substeps=_integration_substeps(
            config.duration_us,
            config.time_steps,
            _max_environment_rate_per_us(rates),
        ),
        max_trace_error=simulation.metadata["max_trace_error"],
        extra={
        "backend_name": PYTHON_DENSE_BACKEND_NAME,
        "simulation_model": "gate_aware_open_system",
        "evolution_mode": GATE_AWARE_HAMILTONIAN_LINDBLAD_MODEL,
        "simulation_mode": derived_parameters["simulation_mode"],
            "hamiltonian_mode": derived_parameters["hamiltonian_mode"],
            "completion_time_us": 0.0,
            "completion_fidelity": fidelities[0],
            "completion_purity": purities[0],
            "configured_duration_us": config.duration_us,
            "total_gate_duration_us": derived_parameters["total_gate_duration_us"],
            "idle_duration_us": config.duration_us,
            "actual_duration_us": config.duration_us,
            "gate_duration_model": derived_parameters["gate_duration_model"],
            "gate_aware_noise": 0.0,
            "post_circuit_degradation": 1.0,
            "recorded_state_count": float(len(times)),
            "state_history_retained": 0.0,
            "state_history_storage_mode": "streaming_metrics_only",
            **_snapshot_diagnostics_from_metadata(simulation.metadata),
            **kernel_stats.to_diagnostics(),
        },
    )
    diagnostics.update(complexity_diagnostics(
        config,
        diagnostics=diagnostics,
        derived_parameters=derived_parameters,
    ))
    profile.diagnostics_build_ms = (perf_counter() - diagnostics_started_at) * 1000.0
    diagnostics.update(profile.to_diagnostics())
    internal_profile = active_internal_profile()
    if internal_profile is not None:
        diagnostics.update(internal_profile.to_diagnostics())

    result = SimulationResult(
        config=config,
        times=times,
        fidelity=fidelities,
        purity=purities,
        effective_operation_time_us=effective_operation_time_us,
        output_probabilities=output_distribution,
        measurement_counts=_sample_measurement_counts(
            output_distribution,
            config.measurement_shots,
            config.measurement_seed,
        ),
        derived_parameters=derived_parameters,
        diagnostics=diagnostics,
        warnings=[],
        issues=[],
        state_snapshots=simulation.state_snapshots,
    )
    return result


def _empty_result(
    config: SimulationConfig,
    issues: list[ValidationIssue],
) -> SimulationResult:
    return SimulationResult(
        config=config,
        times=[],
        fidelity=[],
        purity=[],
        effective_operation_time_us=None,
        output_probabilities={},
        derived_parameters={},
        diagnostics={
            **backend_metadata(config.simulation_backend),
            "simulation_model": "gate_aware_open_system",
            "evolution_mode": GATE_AWARE_HAMILTONIAN_LINDBLAD_MODEL,
        },
        warnings=_issue_warnings(issues),
        issues=issues,
    )


def _attach_backend_metadata(
    result: SimulationResult,
    config: SimulationConfig,
) -> None:
    result.diagnostics.update(backend_metadata(
        config.simulation_backend,
        rust_kernel_used=bool(result.diagnostics.get("rust_kernel_used", False)),
        rust_kernel_fallback_used=bool(
            result.diagnostics.get("rust_kernel_fallback_used", False)
        ),
        rust_kernel_fallback_reason=str(
            result.diagnostics.get("rust_kernel_fallback_reason", "")
        ),
    ))


def _runtime_issues(config: SimulationConfig) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if config.model != DEFAULT_SIMULATION_MODEL:
        return issues
    if config.environment.mode != "normalized":
        issues.append(_runtime_error(
            "UNSUPPORTED_ENVIRONMENT_MODE",
            "The current simulator supports only normalized environment mode.",
            f"Received mode={config.environment.mode!r}",
            "Use mode='normalized' for this simulation backend.",
        ))
    if config.environment.model not in SUPPORTED_ENVIRONMENT_MODELS:
        issues.append(_runtime_error(
            "UNSUPPORTED_ENVIRONMENT_MODEL",
            "The current simulator does not support this environment model.",
            f"Received model={config.environment.model!r}",
            f"Use {UNIFIED_ENVIRONMENT_MODEL!r}.",
        ))
    if (
        config.circuit.logical_qubits > MAX_DENSITY_MATRIX_QUBITS
        and not _environment_is_ideal(config)
    ):
        issues.append(_runtime_error(
            "UNSUPPORTED_QUBIT_COUNT",
            (
                "Noisy density-matrix simulation currently supports up to "
                f"{MAX_DENSITY_MATRIX_QUBITS} logical qubits."
            ),
            f"Received logical_qubits={config.circuit.logical_qubits}",
            (
                "Use an ideal statevector circuit or reduce the noisy circuit to "
                f"{MAX_DENSITY_MATRIX_QUBITS} qubits."
            ),
        ))
    if (
        config.circuit.logical_qubits > 5
        and config.evolution_method == EXPLICIT_CPTP
        and not _environment_is_ideal(config)
    ):
        issues.append(_runtime_error(
            "UNSUPPORTED_EVOLUTION_METHOD",
            "Noisy circuits above 5 qubits require fixed-step RK4.",
            (
                "Explicit CPTP materializes a superoperator whose dimension grows "
                "as 4**logical_qubits."
            ),
            "Select fixed_step_rk4 for 6-8 qubit noisy simulation.",
        ))
    return issues


def _runtime_error(
    code: str,
    message: str,
    detail: str | None = None,
    suggestion: str | None = None,
) -> ValidationIssue:
    return ValidationIssue(
        level="error",
        code=code,
        message=message,
        detail=detail,
        suggestion=suggestion,
    )


def _issue_warnings(issues: list[ValidationIssue]) -> list[str]:
    return [
        f"{issue.code}: {issue.message}"
        for issue in issues
        if issue.level in {"warning", "error", "fatal"}
    ]


def _simulate_circuit_post_circuit(
    config: SimulationConfig,
    duration_us: float,
    time_steps: int,
    collapse_ops: Sequence[CachedCollapseOperator],
    max_environment_rate_per_us: float = 0.0,
    caches: _SimulationCaches | None = None,
    kernel_stats: _KernelStats | None = None,
    profile: _CoreProfilingStats | None = None,
) -> _SimulationSeries:
    caches = caches or _SimulationCaches.empty()
    kernel_stats = kernel_stats or _KernelStats(PYTHON_DENSE_BACKEND)
    profile = profile or _core_profiling_stats(config)
    snapshot_plan = _snapshot_plan(config, duration_us)
    times = _simulation_times(duration_us, time_steps, snapshot_plan)
    n_qubits = config.circuit.logical_qubits
    dimension = 2 ** n_qubits
    hamiltonian = zero_hamiltonian(dimension)

    initial_state = initial_density_matrix(config.circuit.initial_states)
    collector = StateSnapshotCollector(
        actual_duration_us=duration_us,
        max_snapshots=100 if snapshot_plan.enabled else 10,
        plan=snapshot_plan,
    )
    collector.capture_event(
        time_us=0.0,
        event_kind="initial",
        density_matrix=initial_state,
    )
    noisy_state = _apply_circuit_operations(initial_state, config, n_qubits, caches)
    ideal_state = _apply_circuit_operations(initial_state, config, n_qubits, caches)
    collector.capture_event(
        time_us=0.0,
        event_kind="after_circuit",
        density_matrix=noisy_state,
    )
    planned_idle_samples = (
        set()
        if snapshot_plan.enabled
        else idle_sample_times(
            times,
            completion_time_us=0.0,
            final_time_us=duration_us,
        )
    )

    fidelities = [_state_fidelity(noisy_state, ideal_state)]
    purities = [_state_purity(noisy_state)]
    max_trace_error = _trace_error(noisy_state)
    for start_time, end_time in zip(times, times[1:]):
        dt = end_time - start_time
        idle_started_at = perf_counter()
        noisy_state = _evolve_stable(
            noisy_state,
            hamiltonian,
            collapse_ops,
            dt,
            max_environment_rate_per_us,
            kernel_stats,
            profile,
        )
        profile.idle_evolution_ms += (perf_counter() - idle_started_at) * 1000.0
        if is_planned_time(end_time, planned_idle_samples):
            collector.capture(
                time_us=end_time,
                kind="idle_sample",
                density_matrix=noisy_state,
            )
        collector.capture_requested_time(time_us=end_time, density_matrix=noisy_state)
        fidelities.append(_state_fidelity(noisy_state, ideal_state))
        purities.append(_state_purity(noisy_state))
        max_trace_error = max(max_trace_error, _trace_error(noisy_state))
    collector.capture_event(
        time_us=duration_us,
        event_kind="final",
        density_matrix=noisy_state,
    )
    state_snapshots, snapshot_diagnostics = collector.finalize()

    profile.segment_setup_ms = 0.0
    profile.gate_segments_count = len(config.circuit.columns)
    profile.idle_segments_count = 1 if duration_us > 0.0 else 0
    profile.segments_count = profile.gate_segments_count + profile.idle_segments_count
    profile.total_gate_duration_us = _total_gate_duration_us(config)
    profile.total_idle_duration_us = duration_us
    profile.total_segment_duration_us = profile.total_gate_duration_us + profile.total_idle_duration_us
    profile.total_rk4_substeps = _integration_substeps(
        duration_us,
        time_steps,
        max_environment_rate_per_us,
    )
    profile.total_rhs_evaluations = 4 * profile.total_rk4_substeps
    profile.has_gate_segments = profile.gate_segments_count > 0
    profile.has_idle_after_circuit = profile.idle_segments_count > 0
    profile.idle_only = not profile.has_gate_segments and profile.has_idle_after_circuit
    profile.time_steps = time_steps

    return _SimulationSeries(
        times=times,
        fidelity=fidelities,
        purity=purities,
        final_noisy_state=noisy_state,
        final_ideal_state=ideal_state,
        metadata={
            "max_trace_error": max_trace_error,
            "state_history_retained": False,
            "state_history_storage_mode": "streaming_metrics_only",
            **snapshot_diagnostics.to_dict(),
        },
        state_snapshots=state_snapshots,
    )


def _simulate_circuit_gate_aware_hamiltonian(
    config: SimulationConfig,
    duration_us: float,
    time_steps: int,
    collapse_ops: Sequence[CachedCollapseOperator],
    max_environment_rate_per_us: float = 0.0,
    caches: _SimulationCaches | None = None,
    kernel_stats: _KernelStats | None = None,
    profile: _CoreProfilingStats | None = None,
) -> _SimulationSeries:
    caches = caches or _SimulationCaches.empty()
    kernel_stats = kernel_stats or _KernelStats(PYTHON_DENSE_BACKEND)
    profile = profile or _core_profiling_stats(config)
    n_qubits = config.circuit.logical_qubits
    segment_setup_started_at = perf_counter()
    segments = _gate_aware_segments(config, n_qubits, caches)
    profile.segment_setup_ms = (perf_counter() - segment_setup_started_at) * 1000.0
    total_gate_duration = sum(segment["duration_us"] for segment in segments)
    actual_duration = max(duration_us, total_gate_duration)
    idle_duration = max(0.0, actual_duration - total_gate_duration)
    snapshot_plan = _snapshot_plan(config, actual_duration)
    times = _simulation_times(actual_duration, time_steps, snapshot_plan)

    noisy_state = initial_density_matrix(config.circuit.initial_states)
    ideal_state = initial_density_matrix(config.circuit.initial_states)
    collector = StateSnapshotCollector(
        actual_duration_us=actual_duration,
        max_snapshots=100 if snapshot_plan.enabled else 10,
        plan=snapshot_plan,
    )
    collector.capture_event(
        time_us=0.0,
        event_kind="initial",
        density_matrix=noisy_state,
    )
    planned_idle_samples = (
        set()
        if snapshot_plan.enabled
        else idle_sample_times(
            times,
            completion_time_us=total_gate_duration,
            final_time_us=actual_duration,
        )
    )
    fidelities = [_state_fidelity(noisy_state, ideal_state)]
    purities = [_state_purity(noisy_state)]
    max_trace_error = _trace_error(noisy_state)

    current_time = 0.0
    segment_index = 0
    segment_elapsed = 0.0
    segment_start_noisy = noisy_state
    segment_start_ideal = ideal_state
    max_substeps = 1
    uses_explicit_cptp = config.evolution_method == EXPLICIT_CPTP
    cptp_evolver = GateAwareCPTPEvolver(
        backend=(
            "rust"
            if config.simulation_backend == RUST_DENSE_PREVIEW_BACKEND
            else "python"
        )
    )
    completion_time = 0.0 if not segments else None
    completion_noisy_state = noisy_state if not segments else None
    completion_ideal_state = ideal_state if not segments else None

    target_index = 1
    while target_index < len(times):
        target_time = times[target_index]
        samples_appended_in_batch = False
        while current_time < target_time - 1e-15:
            while (
                segment_index < len(segments)
                and segments[segment_index]["duration_us"] == 0.0
            ):
                segment = segments[segment_index]
                unitary = segment["unitary"]
                noisy_state = _apply_zero_duration_unitary(
                    noisy_state,
                    unitary,
                    cleanup=not uses_explicit_cptp,
                )
                ideal_state = _apply_zero_duration_unitary(
                    ideal_state,
                    unitary,
                    cleanup=not uses_explicit_cptp,
                )
                noisy_state, ideal_state = _apply_segment_measurements(
                    noisy_state,
                    ideal_state,
                    segment,
                    n_qubits,
                    cleanup=not uses_explicit_cptp,
                )
                segment_index += 1
                segment_elapsed = 0.0
                segment_start_noisy = noisy_state
                segment_start_ideal = ideal_state
                collector.capture_event(
                    time_us=current_time,
                    event_kind=_segment_boundary_event_kind(segment),
                    column_index=int(segments[segment_index - 1]["column_index"]),
                    density_matrix=noisy_state,
                )
                if segment_index >= len(segments) and completion_time is None:
                    completion_time = current_time
                    completion_noisy_state = noisy_state
                    completion_ideal_state = ideal_state
                    collector.capture_event(
                        time_us=current_time,
                        event_kind="after_circuit",
                        density_matrix=noisy_state,
                    )

            if segment_index >= len(segments):
                idle_started_at = perf_counter()
                try:
                    sampled_batch = (
                        None
                        if uses_explicit_cptp
                        else _try_rust_paired_sampled_batch(
                            noisy_state,
                            ideal_state,
                            zero_hamiltonian(len(noisy_state)),
                            collapse_ops,
                            current_time,
                            times[target_index:],
                            actual_duration,
                            lambda interval: _substep_count(
                                interval,
                                max_environment_rate_per_us,
                            ),
                            kernel_stats,
                            include_boundary=True,
                            capture_time_predicate=lambda sample_time: (
                                is_planned_time(sample_time, planned_idle_samples)
                                or collector.request_for_time(sample_time) is not None
                            ),
                        )
                    )
                    if sampled_batch is not None:
                        captured_states, rust_metrics, sample_times, sample_substeps = sampled_batch
                        max_substeps = max(max_substeps, max(sample_substeps))
                        for sample_index, metric in enumerate(rust_metrics):
                            fidelity, purity, trace_error, ideal_purity = metric
                            if abs(ideal_purity - 1.0) > 1e-8:
                                sampled_state, sampled_ideal = captured_states[sample_index]
                                fidelity = _state_fidelity(sampled_state, sampled_ideal)
                            fidelities.append(fidelity)
                            purities.append(purity)
                            max_trace_error = max(max_trace_error, trace_error)
                        for sample_index, (sampled_state, sampled_ideal) in captured_states.items():
                            sample_time = sample_times[sample_index]
                            if sample_index == len(rust_metrics) - 1:
                                noisy_state = sampled_state
                                ideal_state = sampled_ideal
                            if is_planned_time(sample_time, planned_idle_samples):
                                collector.capture(
                                    time_us=sample_time,
                                    kind="idle_sample",
                                    density_matrix=sampled_state,
                                )
                            collector.capture_requested_time(
                                time_us=sample_time,
                                density_matrix=sampled_state,
                            )
                        current_time = sample_times[-1]
                        target_index += len(rust_metrics)
                        samples_appended_in_batch = True
                        continue

                    step_dt = target_time - current_time
                    if step_dt > 0.0:
                        substeps = _substep_count(step_dt, max_environment_rate_per_us)
                        max_substeps = max(max_substeps, substeps)
                        if uses_explicit_cptp:
                            noisy_state = cptp_evolver.evolve(
                                noisy_state,
                                zero_hamiltonian(len(noisy_state)),
                                collapse_ops,
                                step_dt,
                            )
                        else:
                            noisy_state = _evolve_stable_with_substeps(
                                noisy_state,
                                zero_hamiltonian(len(noisy_state)),
                                collapse_ops,
                                step_dt,
                                substeps,
                                kernel_stats,
                                profile,
                                blocked_by_sampling=True,
                                blocked_by_boundary=False,
                            )
                        current_time = target_time
                        if is_planned_time(current_time, planned_idle_samples):
                            collector.capture(
                                time_us=current_time,
                                kind="idle_sample",
                                density_matrix=noisy_state,
                            )
                        collector.capture_requested_time(
                            time_us=current_time,
                            density_matrix=noisy_state,
                        )
                    continue
                finally:
                    profile.idle_evolution_ms += (perf_counter() - idle_started_at) * 1000.0

            segment = segments[segment_index]
            duration = segment["duration_us"]
            remaining = duration - segment_elapsed
            step_dt = min(target_time - current_time, remaining)
            completes_segment = abs(step_dt - remaining) <= 1e-15
            hits_sample_time = abs(current_time + step_dt - target_time) <= 1e-15

            if segment_elapsed == 0.0:
                segment_start_noisy = noisy_state
                segment_start_ideal = ideal_state

            unitary = segment["unitary"]
            hamiltonian = segment["hamiltonian"]
            hamiltonian_scale = segment["hamiltonian_scale_per_us"]
            if step_dt > 0.0:
                gate_started_at = perf_counter()
                try:
                    substeps = _generator_substep_count(
                        step_dt,
                        max_environment_rate_per_us + hamiltonian_scale,
                    )
                    max_substeps = max(max_substeps, substeps)
                    if not completes_segment and not uses_explicit_cptp:
                        segment_stop_time = current_time + remaining
                        sample_targets = times[target_index:]
                        paired_batch = _try_rust_paired_sampled_batch(
                            noisy_state,
                            ideal_state,
                            hamiltonian,
                            collapse_ops,
                            current_time,
                            sample_targets,
                            segment_stop_time,
                            lambda interval: _generator_substep_count(
                                interval,
                                max_environment_rate_per_us + hamiltonian_scale,
                            ),
                            kernel_stats,
                            include_boundary=False,
                            capture_time_predicate=lambda sample_time: (
                                collector.request_for_time(sample_time) is not None
                            ),
                        )
                        if paired_batch is not None:
                            (
                                captured_states,
                                rust_metrics,
                                sample_times,
                                sample_substeps,
                            ) = paired_batch
                            max_substeps = max(
                                max_substeps,
                                max(sample_substeps),
                            )
                            for sample_index, metric in enumerate(rust_metrics):
                                fidelity, purity, trace_error, ideal_purity = metric
                                if abs(ideal_purity - 1.0) > 1e-8:
                                    noisy_sample, ideal_sample = captured_states[sample_index]
                                    fidelity = _state_fidelity(noisy_sample, ideal_sample)
                                fidelities.append(fidelity)
                                purities.append(purity)
                                max_trace_error = max(max_trace_error, trace_error)
                            for sample_index, (noisy_sample, ideal_sample) in captured_states.items():
                                if sample_index == len(rust_metrics) - 1:
                                    noisy_state = noisy_sample
                                    ideal_state = ideal_sample
                                if collector.request_for_time(sample_times[sample_index]) is not None:
                                    collector.capture_requested_time(
                                        time_us=sample_times[sample_index],
                                        density_matrix=noisy_sample,
                                    )
                            elapsed = sample_times[-1] - current_time
                            current_time = sample_times[-1]
                            segment_elapsed += elapsed
                            target_index += len(rust_metrics)
                            samples_appended_in_batch = True
                            continue

                    if uses_explicit_cptp:
                        noisy_state = cptp_evolver.evolve(
                            noisy_state,
                            hamiltonian,
                            collapse_ops,
                            step_dt,
                        )
                    elif collapse_ops or not completes_segment:
                        noisy_state = _evolve_stable_with_substeps(
                            noisy_state,
                            hamiltonian,
                            collapse_ops,
                            step_dt,
                            substeps,
                            kernel_stats,
                            profile,
                            blocked_by_sampling=hits_sample_time,
                            blocked_by_boundary=completes_segment,
                        )
                    else:
                        noisy_state = clean_density_matrix(
                            apply_unitary_to_density(segment_start_noisy, unitary)
                        )

                    if uses_explicit_cptp:
                        ideal_state = apply_unitary_to_density(
                            ideal_state,
                            unitary_from_hamiltonian(hamiltonian, step_dt),
                        )
                    elif completes_segment:
                        ideal_state = clean_density_matrix(
                            apply_unitary_to_density(segment_start_ideal, unitary)
                        )
                    else:
                        ideal_state = _evolve_stable_with_substeps(
                            ideal_state,
                            hamiltonian,
                            [],
                            step_dt,
                            substeps,
                            kernel_stats,
                            profile,
                            blocked_by_sampling=True,
                            blocked_by_boundary=False,
                        )
                finally:
                    profile.gate_evolution_ms += (perf_counter() - gate_started_at) * 1000.0

            current_time += step_dt
            segment_elapsed += step_dt
            if completes_segment:
                noisy_state, ideal_state = _apply_segment_measurements(
                    noisy_state,
                    ideal_state,
                    segment,
                    n_qubits,
                    cleanup=not uses_explicit_cptp,
                )
                segment_index += 1
                segment_elapsed = 0.0
                segment_start_noisy = noisy_state
                segment_start_ideal = ideal_state
                collector.capture_event(
                    time_us=current_time,
                    event_kind=_segment_boundary_event_kind(segment),
                    column_index=int(segment["column_index"]),
                    density_matrix=noisy_state,
                )
                if segment_index >= len(segments) and completion_time is None:
                    completion_time = current_time
                    completion_noisy_state = noisy_state
                    completion_ideal_state = ideal_state
                    collector.capture_event(
                        time_us=current_time,
                        event_kind="after_circuit",
                        density_matrix=noisy_state,
                    )

        while (
            segment_index < len(segments)
            and segments[segment_index]["duration_us"] == 0.0
        ):
            segment = segments[segment_index]
            unitary = segment["unitary"]
            noisy_state = _apply_zero_duration_unitary(
                noisy_state,
                unitary,
                cleanup=not uses_explicit_cptp,
            )
            ideal_state = _apply_zero_duration_unitary(
                ideal_state,
                unitary,
                cleanup=not uses_explicit_cptp,
            )
            noisy_state, ideal_state = _apply_segment_measurements(
                noisy_state,
                ideal_state,
                segment,
                n_qubits,
                cleanup=not uses_explicit_cptp,
            )
            segment_index += 1
            segment_elapsed = 0.0
            segment_start_noisy = noisy_state
            segment_start_ideal = ideal_state
            collector.capture_event(
                time_us=current_time,
                event_kind=_segment_boundary_event_kind(segment),
                column_index=int(segments[segment_index - 1]["column_index"]),
                density_matrix=noisy_state,
            )
            if segment_index >= len(segments) and completion_time is None:
                completion_time = current_time
                completion_noisy_state = noisy_state
                completion_ideal_state = ideal_state
                collector.capture_event(
                    time_us=current_time,
                    event_kind="after_circuit",
                    density_matrix=noisy_state,
                )

        if samples_appended_in_batch:
            continue

        fidelities.append(_state_fidelity(noisy_state, ideal_state))
        purities.append(_state_purity(noisy_state))
        max_trace_error = max(max_trace_error, _trace_error(noisy_state))
        collector.capture_requested_time(
            time_us=current_time,
            density_matrix=noisy_state,
        )
        target_index += 1

    if completion_time is None:
        completion_time = current_time
        completion_noisy_state = noisy_state
        completion_ideal_state = ideal_state
        collector.capture_event(
            time_us=current_time,
            event_kind="after_circuit",
            density_matrix=noisy_state,
        )

    collector.capture_event(
        time_us=actual_duration,
        event_kind="final",
        density_matrix=noisy_state,
    )
    state_snapshots, snapshot_diagnostics = collector.finalize()

    segment_complexity = _segment_complexity_metadata(
        segments,
        idle_duration,
        max_environment_rate_per_us,
    )
    profile.gate_segments_count = int(segment_complexity["gate_segment_count"])
    profile.idle_segments_count = int(segment_complexity["idle_segment_count"])
    profile.segments_count = int(segment_complexity["total_segment_count"])
    profile.total_rk4_substeps = int(segment_complexity["total_rk4_substeps"])
    profile.total_rhs_evaluations = int(segment_complexity["total_rhs_evaluations"])
    if uses_explicit_cptp:
        profile.total_rk4_substeps = 0
        profile.total_rhs_evaluations = 0
    profile.total_gate_duration_us = total_gate_duration
    profile.total_idle_duration_us = idle_duration
    profile.total_segment_duration_us = actual_duration
    profile.has_gate_segments = profile.gate_segments_count > 0
    profile.has_idle_after_circuit = profile.idle_segments_count > 0
    profile.idle_only = not profile.has_gate_segments and profile.has_idle_after_circuit
    profile.time_steps = time_steps
    metadata = {
        "backend_name": PYTHON_DENSE_BACKEND_NAME,
        "simulation_mode": GATE_AWARE_HAMILTONIAN_LINDBLAD_MODEL,
        "gate_aware_noise": True,
        "hamiltonian_mode": "effective_unitary_spectral_generator_v2",
        "involution_compatibility_branch": True,
        "completion_time_us": completion_time,
        "completion_fidelity": _state_fidelity(
            completion_noisy_state,
            completion_ideal_state,
        ),
        "completion_purity": _state_purity(completion_noisy_state),
        "total_gate_duration_us": total_gate_duration,
        "idle_duration_us": idle_duration,
        "actual_duration_us": actual_duration,
        "gate_duration_model": "default_gate_duration_us_with_params_override",
        "post_circuit_degradation": False,
        "integration_substeps": 0.0 if uses_explicit_cptp else float(max_substeps),
        "max_trace_error": max_trace_error,
        "evolution_method_requested": config.evolution_method,
        "evolution_method_resolved": config.evolution_method,
        "evolution_method_id": (
            GATE_AWARE_CPTP_EVOLUTION_ID
            if uses_explicit_cptp
            else "fixed_step_rk4_v1"
        ),
        "cptp_guaranteed_by_construction": uses_explicit_cptp,
        "cleanup_applied": not uses_explicit_cptp,
        "state_history_retained": False,
        "state_history_storage_mode": "streaming_metrics_only",
        **snapshot_diagnostics.to_dict(),
        **segment_complexity,
        **(cptp_evolver.diagnostics() if uses_explicit_cptp else {}),
    }
    if uses_explicit_cptp:
        metadata.update({
            "total_rk4_substeps": 0.0,
            "total_rhs_evaluations": 0.0,
            "gate_rk4_substeps": 0.0,
            "idle_rk4_substeps": 0.0,
        })
    return _SimulationSeries(
        times=times,
        fidelity=fidelities,
        purity=purities,
        final_noisy_state=noisy_state,
        final_ideal_state=ideal_state,
        metadata=metadata,
        state_snapshots=state_snapshots,
    )


def _apply_zero_duration_unitary(
    state: Matrix,
    unitary: Matrix,
    *,
    cleanup: bool,
) -> Matrix:
    evolved = apply_unitary_to_density(state, unitary)
    return clean_density_matrix(evolved) if cleanup else evolved


def _apply_segment_measurements(
    noisy_state: Matrix,
    ideal_state: Matrix,
    segment: Mapping[str, object],
    n_qubits: int,
    *,
    cleanup: bool,
) -> tuple[Matrix, Matrix]:
    targets = tuple(int(target) for target in segment.get("measurement_targets", ()))
    if not targets:
        return noisy_state, ideal_state
    measured_noisy = apply_non_selective_computational_measurement(
        noisy_state,
        targets,
        n_qubits,
    )
    measured_ideal = apply_non_selective_computational_measurement(
        ideal_state,
        targets,
        n_qubits,
    )
    if cleanup:
        return clean_density_matrix(measured_noisy), clean_density_matrix(measured_ideal)
    return measured_noisy, measured_ideal


def _segment_boundary_event_kind(segment: Mapping[str, object]) -> str:
    return "measurement" if segment.get("measurement_targets") else "column_boundary"


def _gate_aware_segments(
    config: SimulationConfig,
    n_qubits: int,
    caches: _SimulationCaches | None = None,
) -> list[dict[str, object]]:
    caches = caches or _SimulationCaches.empty()
    segments: list[dict[str, object]] = []
    for column_index, column in enumerate(
        sorted(config.circuit.columns, key=lambda column: column.step)
    ):
        unitary = _column_unitary_cached(column, n_qubits, caches)
        duration = column_duration_us(column)
        if duration == 0.0:
            hamiltonian = zero_hamiltonian(2 ** n_qubits)
        else:
            hamiltonian = _effective_hamiltonian_cached(
                unitary,
                duration,
                caches,
                known_involution=_column_is_known_hermitian_involution(column),
            )
            hamiltonian_scale = 2.0 * math.pi / duration
        segments.append({
            "segment_type": "gate",
            "column_index": column_index,
            "unitary": unitary,
            "measurement_targets": _column_measurement_targets(column),
            "duration_us": duration,
            "hamiltonian": hamiltonian,
            "hamiltonian_scale_per_us": hamiltonian_scale if duration > 0.0 else 0.0,
        })
    return segments


def _segment_complexity_metadata(
    segments: list[dict[str, object]],
    idle_duration_us: float,
    environment_rate_per_us: float,
) -> dict[str, float]:
    gate_segment_count = 0
    idle_segment_count = 1 if idle_duration_us > 0.0 else 0
    gate_substeps = 0
    idle_substeps = 0
    max_hamiltonian_scale = 0.0
    max_generator_scale = 0.0

    for segment in segments:
        duration = float(segment["duration_us"])
        if duration <= 0.0:
            continue
        gate_segment_count += 1
        hamiltonian_scale = float(segment["hamiltonian_scale_per_us"])
        generator_scale = environment_rate_per_us + hamiltonian_scale
        substeps = _generator_substep_count(duration, generator_scale)
        gate_substeps += substeps
        max_hamiltonian_scale = max(max_hamiltonian_scale, hamiltonian_scale)
        max_generator_scale = max(max_generator_scale, generator_scale)

    if idle_duration_us > 0.0:
        idle_substeps = _generator_substep_count(idle_duration_us, environment_rate_per_us)
        max_generator_scale = max(max_generator_scale, environment_rate_per_us)

    total_substeps = gate_substeps + idle_substeps
    return {
        "gate_segment_count": float(gate_segment_count),
        "idle_segment_count": float(idle_segment_count),
        "total_segment_count": float(gate_segment_count + idle_segment_count),
        "total_rk4_substeps": float(total_substeps),
        "total_rhs_evaluations": float(4 * total_substeps),
        "gate_rk4_substeps": float(gate_substeps),
        "idle_rk4_substeps": float(idle_substeps),
        "max_hamiltonian_scale_per_us": float(max_hamiltonian_scale),
        "max_environment_rate_per_us": float(environment_rate_per_us),
        "max_generator_scale_per_us": float(max_generator_scale),
    }


def _core_profiling_stats(config: SimulationConfig) -> _CoreProfilingStats:
    dimension = 2 ** config.circuit.logical_qubits
    return _CoreProfilingStats(
        dimension=dimension,
        density_matrix_shape=f"{dimension}x{dimension}",
        time_steps=int(config.time_steps),
    )


def _total_gate_duration_us(config: SimulationConfig) -> float:
    return sum(column_duration_us(column) for column in config.circuit.columns)


def _time_grid(duration_us: float, step_count: int) -> list[float]:
    if duration_us <= 0.0:
        raise ValueError("duration_us must be positive")
    if step_count < 2:
        raise ValueError("step_count must be at least 2")
    return [
        duration_us * step_index / (step_count - 1)
        for step_index in range(step_count)
    ]


def _snapshot_plan(config: SimulationConfig, actual_duration_us: float) -> SnapshotPlan:
    return build_snapshot_plan(
        config.snapshot_options,
        actual_duration_us=actual_duration_us,
    )


def _simulation_times(
    duration_us: float,
    step_count: int,
    snapshot_plan: SnapshotPlan,
) -> list[float]:
    times = _time_grid(duration_us, step_count)
    if not snapshot_plan.enabled:
        return times
    combined = [*times, *snapshot_plan.requested_times]
    combined.sort()
    deduped: list[float] = []
    for time_us in combined:
        if deduped and math.isclose(
            deduped[-1],
            time_us,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            continue
        deduped.append(float(time_us))
    return deduped


def _column_measurement_targets(column) -> tuple[int, ...]:
    return tuple(
        int(target)
        for gate in column.gates
        if str(gate.type).upper() == "MEASURE"
        for target in gate.targets
    )


def _apply_circuit_operations(
    state: Matrix,
    config: SimulationConfig,
    n_qubits: int,
    caches: _SimulationCaches | None = None,
) -> Matrix:
    caches = caches or _SimulationCaches.empty()
    for column in sorted(config.circuit.columns, key=lambda column: column.step):
        for gate in column.gates:
            state = apply_unitary_to_density(
                state,
                _gate_unitary_cached(gate, n_qubits, caches),
            )
            state = clean_density_matrix(state)
        measurement_targets = _column_measurement_targets(column)
        if measurement_targets:
            state = apply_non_selective_computational_measurement(
                state,
                measurement_targets,
                n_qubits,
            )
    return state


def _gate_unitary_cached(
    gate,
    n_qubits: int,
    caches: _SimulationCaches,
) -> Matrix:
    key = _gate_unitary_cache_key(gate, n_qubits)
    if key not in caches.gate_unitaries:
        caches.gate_unitaries[key] = gate_unitary(gate, n_qubits)
    return caches.gate_unitaries[key]


def _column_unitary_cached(
    column,
    n_qubits: int,
    caches: _SimulationCaches,
) -> Matrix:
    key = _column_unitary_cache_key(column, n_qubits)
    if key not in caches.column_unitaries:
        if len(column.gates) == 1:
            unitary = _gate_unitary_cached(column.gates[0], n_qubits, caches)
        else:
            unitary = identity_matrix(2 ** n_qubits)
            for gate in column.gates:
                unitary = matmul(_gate_unitary_cached(gate, n_qubits, caches), unitary)
        caches.column_unitaries[key] = unitary
    return caches.column_unitaries[key]


def _effective_hamiltonian_cached(
    unitary: Matrix,
    duration_us: float,
    caches: _SimulationCaches,
    *,
    known_involution: bool = False,
) -> Matrix:
    key = (float(duration_us), bool(known_involution), unitary)
    if key not in caches.hamiltonians:
        if known_involution:
            caches.hamiltonians[key] = effective_hamiltonian_from_involution(
                unitary, duration_us, validate=False
            )
        else:
            caches.hamiltonians[key] = effective_hamiltonian_from_unitary(
                unitary,
                duration_us,
            )
    return caches.hamiltonians[key]


def _column_is_known_hermitian_involution(column) -> bool:
    """Return whether audited, disjoint column gates guarantee U†=U and U²=I."""

    involutions = {"I", "H", "X", "Y", "Z", "CNOT", "CZ", "SWAP", "CCX", "ORACLE"}
    return bool(column.gates) and all(
        str(gate.type).upper() in involutions for gate in column.gates
    )


def _gate_unitary_cache_key(gate, n_qubits: int) -> tuple[object, ...]:
    return (
        int(n_qubits),
        str(gate.type).upper(),
        tuple(int(target) for target in gate.targets),
        tuple(int(control) for control in (gate.controls or [])),
        tuple(
            sorted(
                (str(name), float(value))
                for name, value in (gate.params or {}).items()
            )
        ),
    )


def _column_unitary_cache_key(column, n_qubits: int) -> tuple[object, ...]:
    return (
        int(n_qubits),
        tuple(_gate_unitary_cache_key(gate, n_qubits) for gate in column.gates),
    )


def _state_fidelity(state: Matrix, ideal_state: Matrix) -> float:
    ideal_purity = _trace_product_real_fast(ideal_state, ideal_state)
    # Unitary reference trajectories can accumulate backend-dependent roundoff
    # slightly above the stricter state-audit tolerance. Treat only that narrow
    # numerical neighborhood as pure; projective measurement produces a much
    # larger purity change and therefore takes the mixed-state Uhlmann branch.
    if abs(ideal_purity - 1.0) <= 1e-8:
        return _as_probability(_trace_product_real_fast(state, ideal_state))

    state_array = np.asarray(state, dtype=np.complex128)
    ideal_array = np.asarray(ideal_state, dtype=np.complex128)
    ideal_array = 0.5 * (ideal_array + ideal_array.conj().T)
    ideal_eigenvalues, ideal_eigenvectors = np.linalg.eigh(ideal_array)
    ideal_sqrt = (
        ideal_eigenvectors
        @ np.diag(np.sqrt(np.clip(ideal_eigenvalues, 0.0, None)))
        @ ideal_eigenvectors.conj().T
    )
    sandwiched = ideal_sqrt @ state_array @ ideal_sqrt
    sandwiched = 0.5 * (sandwiched + sandwiched.conj().T)
    eigenvalues = np.linalg.eigvalsh(sandwiched)
    fidelity = float(np.square(np.sum(np.sqrt(np.clip(eigenvalues, 0.0, None)))))
    return _as_probability(fidelity)


def _state_purity(state: Matrix) -> float:
    return _as_probability(_trace_product_real_fast(state, state))


def _trace_product_real_fast(left: Matrix, right: Matrix) -> float:
    """Compute Re Tr(left @ right) without materializing the matrix product."""

    left_array = np.asarray(left, dtype=np.complex128)
    right_array = np.asarray(right, dtype=np.complex128)
    return float(np.sum(left_array * right_array.T).real)


def _sample_measurement_counts(
    probabilities: Mapping[str, float],
    shots: int,
    seed: int,
) -> dict[str, int]:
    labels = list(probabilities)
    weights = [max(0.0, float(probabilities[label])) for label in labels]
    total = sum(weights)
    if not labels or total <= 0.0:
        return {}

    cumulative: list[float] = []
    running = 0.0
    for weight in weights:
        running += weight
        cumulative.append(running)

    counts = {label: 0 for label in labels}
    random_source = Random(int(seed))
    for _ in range(int(shots)):
        sample = random_source.random() * total
        index = min(bisect_right(cumulative, sample), len(labels) - 1)
        counts[labels[index]] += 1
    return counts


def _sample_classical_shot_preview(
    branches: Sequence[Mapping[str, object]],
    shots: int,
    seed: int,
    *,
    limit: int = 64,
) -> list[dict[str, object]]:
    """Return a bounded, seeded preview of branch-level shot trajectories."""

    if not branches:
        return []
    weights = [max(0.0, float(branch.get("probability", 0.0))) for branch in branches]
    total = sum(weights)
    if total <= 0.0:
        return []
    cumulative: list[float] = []
    running = 0.0
    for weight in weights:
        running += weight
        cumulative.append(running)

    random_source = Random(int(seed) + 1)
    preview: list[dict[str, object]] = []
    for shot_index in range(min(int(shots), int(limit))):
        sample = random_source.random() * total
        branch_index = min(
            bisect_right(cumulative, sample),
            len(branches) - 1,
        )
        branch = branches[branch_index]
        preview.append({
            "shot_index": shot_index,
            "branch_index": branch_index,
            "classical_bits": list(branch.get("classical_bits", [])),
            "measurements": list(branch.get("measurements", [])),
        })
    return preview


def _diagnostics(
    times: Sequence[float],
    fidelities: Sequence[float],
    purities: Sequence[float],
    fidelity_threshold: float,
    integration_substeps: int = 1,
    max_trace_error: float = 0.0,
    extra: Mapping[str, object] | None = None,
) -> dict[str, object]:
    diagnostics = {
        "final_time_us": times[-1],
        "final_fidelity": fidelities[-1],
        "final_purity": purities[-1],
        "min_fidelity": min(fidelities),
        "min_purity": min(purities),
        "time_step_us": times[1] - times[0] if len(times) > 1 else 0.0,
        "threshold_crossed": 1.0 if min(fidelities) < fidelity_threshold else 0.0,
        "max_trace_error": float(max_trace_error),
        "integration_substeps": float(integration_substeps),
    }
    diagnostics.update(dict(extra or {}))
    return diagnostics


def _snapshot_diagnostics_from_metadata(
    metadata: Mapping[str, object],
) -> dict[str, object]:
    return {
        str(key): value
        for key, value in metadata.items()
        if str(key).startswith("state_snapshot_")
    }


def _evolve_stable(
    state: Matrix,
    hamiltonian: Matrix,
    collapse_ops: Sequence[CachedCollapseOperator],
    dt: float,
    max_environment_rate_per_us: float,
    kernel_stats: _KernelStats | None = None,
    profile: _CoreProfilingStats | None = None,
    blocked_by_sampling: bool = True,
    blocked_by_boundary: bool = False,
) -> Matrix:
    substeps = _substep_count(dt, max_environment_rate_per_us)
    return _evolve_stable_with_substeps(
        state,
        hamiltonian,
        collapse_ops,
        dt,
        substeps,
        kernel_stats,
        profile,
        blocked_by_sampling=blocked_by_sampling,
        blocked_by_boundary=blocked_by_boundary,
    )


def _evolve_stable_with_substeps(
    state: Matrix,
    hamiltonian: Matrix,
    collapse_ops: Sequence[CachedCollapseOperator],
    dt: float,
    substeps: int,
    kernel_stats: _KernelStats | None = None,
    profile: _CoreProfilingStats | None = None,
    blocked_by_sampling: bool = True,
    blocked_by_boundary: bool = False,
) -> Matrix:
    substeps = max(1, int(substeps))
    sub_dt = dt / substeps
    if kernel_stats is not None and kernel_stats.wants_rust:
        try:
            session = _rust_dense_session(state, collapse_ops, kernel_stats)
            evolved = session.evolve_cleaned(state, hamiltonian, sub_dt, substeps)
            kernel_stats.rust_kernel_used = True
            if kernel_stats.rust_kernel_sampled_batch_count == 0:
                kernel_stats.rust_kernel_mode = "cleaned_multi_substep"
            kernel_stats.rust_kernel_call_count += 1
            kernel_stats.rust_kernel_segment_count += 1
            kernel_stats.rust_kernel_substep_count += substeps
            kernel_stats.rust_kernel_piecewise_segment_count += 1
            kernel_stats.record_rust_batch(
                substeps,
                blocked_by_sampling=blocked_by_sampling,
                blocked_by_boundary=blocked_by_boundary,
            )
            return evolved
        except Exception as exc:
            kernel_stats.rust_kernel_fallback_used = True
            kernel_stats.rust_kernel_fallback_reason = str(exc)
            kernel_stats.rust_kernel_mode = "fallback_python"

    if should_use_numpy_dense():
        evolved = evolve_segment_numpy(
            state,
            hamiltonian,
            collapse_ops,
            dt,
            substeps,
        )
        if profile is not None:
            profile.dense_execution_engine = "numpy_dense_v1"
            profile.zero_hamiltonian_fast_path_used = (
                profile.zero_hamiltonian_fast_path_used
                or evolved.zero_hamiltonian_fast_path_used
            )
        if kernel_stats is not None:
            kernel_stats.python_kernel_segment_count += substeps
            kernel_stats.python_kernel_substep_count += substeps
        return evolved.state

    if profile is not None and profile.dense_execution_engine != "numpy_dense_v1":
        profile.dense_execution_engine = "python_tuple_v1"

    evolved = state
    for _ in range(substeps):
        evolved = _evolve_one_substep(
            evolved,
            hamiltonian,
            collapse_ops,
            sub_dt,
            kernel_stats,
            profile,
        )
    return evolved


def _try_rust_paired_sampled_batch(
    state: Matrix,
    ideal_state: Matrix,
    hamiltonian: Matrix,
    collapse_ops: Sequence[CachedCollapseOperator],
    current_time: float,
    target_times: Sequence[float],
    stop_time: float,
    substep_counter,
    kernel_stats: _KernelStats | None,
    include_boundary: bool,
    capture_time_predicate=None,
) -> tuple[
    dict[int, tuple[Matrix, Matrix]],
    tuple[tuple[float, float, float, float], ...],
    list[float],
    list[int],
] | None:
    if kernel_stats is None or not kernel_stats.wants_rust:
        return None
    plan = _sampled_batch_plan(
        current_time,
        target_times,
        stop_time,
        substep_counter,
        include_boundary,
    )
    if plan is None:
        return None
    sample_times, sample_substeps, sub_dt, blocked_by_boundary = plan
    try:
        session = _rust_dense_session(state, collapse_ops, kernel_stats)
        capture_flags = [
            bool(capture_time_predicate(sample_time))
            if capture_time_predicate is not None else False
            for sample_time in sample_times
        ]
        captured_states, metrics = session.evolve_paired_registered_compact(
            state,
            ideal_state,
            [hamiltonian] * len(sample_substeps),
            [sub_dt] * len(sample_substeps),
            sample_substeps,
            capture_flags,
        )
    except Exception as exc:
        kernel_stats.record_sampled_batch_fallback(str(exc))
        return None
    kernel_stats.record_sampled_batch(
        len(captured_states),
        2 * sum(sample_substeps),
        blocked_by_sampling=True,
        blocked_by_boundary=blocked_by_boundary,
    )
    kernel_stats.rust_kernel_piecewise_segment_count += 2 * len(sample_substeps)
    return captured_states, metrics, sample_times, sample_substeps


def _rust_dense_session(
    state: Matrix,
    collapse_ops: Sequence[CachedCollapseOperator],
    kernel_stats: _KernelStats,
) -> RustDenseSession:
    if isinstance(collapse_ops, _LazyEnvironmentCollapseOperators):
        rates = collapse_ops.rates
        key: tuple[object, ...] = (
            "local_rates",
            len(state),
            collapse_ops.n_qubits,
            rates.gamma_down_per_us,
            rates.gamma_up_per_us,
            rates.gamma_phi_per_us,
        )
    else:
        key = (len(state), *(id(item.operator) for item in collapse_ops))
    session = kernel_stats.rust_sessions.get(key)
    if session is not None:
        kernel_stats.rust_kernel_session_reuse_count += 1
        return session
    shared_cache: OrderedDict[tuple[object, ...], RustDenseSession] | None = None
    if isinstance(collapse_ops, _LazyEnvironmentCollapseOperators):
        shared_cache = getattr(_RUST_SESSION_CACHE_LOCAL, "sessions", None)
        if shared_cache is None:
            shared_cache = OrderedDict()
            _RUST_SESSION_CACHE_LOCAL.sessions = shared_cache
        session = shared_cache.get(key)
        if session is not None:
            shared_cache.move_to_end(key)
            kernel_stats.rust_kernel_session_reuse_count += 1
        else:
            rates = collapse_ops.rates
            session = RustDenseSession.from_local_rates(
                state,
                collapse_ops.n_qubits,
                rates.gamma_down_per_us,
                rates.gamma_up_per_us,
                rates.gamma_phi_per_us,
            )
            shared_cache[key] = session
            while len(shared_cache) > _RUST_SESSION_CACHE_LIMIT:
                shared_cache.popitem(last=False)
            kernel_stats.rust_kernel_session_count += 1
    else:
        session = RustDenseSession(
            state,
            [collapse_op.operator for collapse_op in collapse_ops],
        )
        kernel_stats.rust_kernel_session_count += 1
    kernel_stats.rust_sessions[key] = session
    return session


def _sampled_batch_plan(
    current_time: float,
    target_times: Sequence[float],
    stop_time: float,
    substep_counter,
    include_boundary: bool,
) -> tuple[list[float], list[int], float, bool] | None:
    sample_times: list[float] = []
    sample_substeps: list[int] = []
    common_sub_dt: float | None = None
    cursor = current_time
    blocked_by_boundary = False

    for target_time in target_times:
        if include_boundary:
            if target_time > stop_time + 1e-15:
                break
            reaches_boundary = abs(target_time - stop_time) <= 1e-15
        else:
            if target_time >= stop_time - 1e-15:
                blocked_by_boundary = True
                break
            reaches_boundary = False

        interval = target_time - cursor
        if interval <= 1e-15:
            break
        substeps = max(1, int(substep_counter(interval)))
        sub_dt = interval / substeps
        if common_sub_dt is None:
            common_sub_dt = sub_dt
        elif not math.isclose(sub_dt, common_sub_dt, rel_tol=1e-12, abs_tol=1e-15):
            break

        sample_times.append(target_time)
        sample_substeps.append(substeps)
        cursor = target_time
        if reaches_boundary:
            blocked_by_boundary = True
            break

    if not sample_times or common_sub_dt is None:
        return None
    return sample_times, sample_substeps, common_sub_dt, blocked_by_boundary


def _evolve_one_substep(
    state: Matrix,
    hamiltonian: Matrix,
    collapse_ops: Sequence[CachedCollapseOperator],
    dt: float,
    kernel_stats: _KernelStats | None,
    profile: _CoreProfilingStats | None = None,
) -> Matrix:
    if kernel_stats is not None and kernel_stats.wants_rust:
        try:
            session = _rust_dense_session(state, collapse_ops, kernel_stats)
            evolved = session.evolve_cleaned(state, hamiltonian, dt, 1)
            kernel_stats.rust_kernel_used = True
            if kernel_stats.rust_kernel_sampled_batch_count == 0:
                kernel_stats.rust_kernel_mode = "per_substep"
            kernel_stats.rust_kernel_call_count += 1
            kernel_stats.rust_kernel_segment_count += 1
            kernel_stats.rust_kernel_substep_count += 1
            kernel_stats.rust_kernel_piecewise_segment_count += 1
            return clean_density_matrix(evolved)
        except Exception as exc:
            kernel_stats.rust_kernel_fallback_used = True
            kernel_stats.rust_kernel_fallback_reason = str(exc)
            kernel_stats.rust_kernel_mode = "fallback_python"

    if profile is not None and profile.dense_execution_engine != "numpy_dense_v1":
        profile.dense_execution_engine = "python_tuple_v1"
        profile.zero_hamiltonian_fast_path_used = (
            profile.zero_hamiltonian_fast_path_used
            or _is_zero_hamiltonian(hamiltonian)
        )
    if kernel_stats is not None:
        kernel_stats.python_kernel_segment_count += 1
        kernel_stats.python_kernel_substep_count += 1
    return clean_density_matrix(
        rk4_step_cached(state, hamiltonian, collapse_ops, dt)
    )


def _integration_substeps(
    duration_us: float,
    time_steps: int,
    max_environment_rate_per_us: float,
) -> int:
    if time_steps < 2:
        return 1
    return _substep_count(duration_us / (time_steps - 1), max_environment_rate_per_us)


def _substep_count(dt: float, max_environment_rate_per_us: float) -> int:
    rate_step = abs(dt) * max(0.0, max_environment_rate_per_us)
    if rate_step <= MAX_RK4_RATE_STEP_PRODUCT:
        return 1
    return max(1, int(math.ceil(rate_step / MAX_RK4_RATE_STEP_PRODUCT)))


def _is_zero_hamiltonian(hamiltonian: Matrix) -> bool:
    return all(entry == 0.0 for row in hamiltonian for entry in row)


def _generator_substep_count(dt: float, generator_scale_per_us: float) -> int:
    rate_step = abs(dt) * max(0.0, generator_scale_per_us)
    if rate_step <= MAX_GENERATOR_STEP_PRODUCT:
        return 1
    return max(1, int(math.ceil(rate_step / MAX_GENERATOR_STEP_PRODUCT)))


def _max_environment_rate_per_us(rates) -> float:
    if isinstance(rates, Sequence):
        return max((_max_environment_rate_per_us(rate) for rate in rates), default=0.0)
    return (
        abs(float(rates.gamma_down_per_us))
        + abs(float(rates.gamma_up_per_us))
        + abs(float(rates.gamma_phi_per_us))
    )


def _resolved_environment_rates(config: SimulationConfig):
    """Return shared metadata rates, local rates, and solver input rates."""

    shared = compute_environment_rates(config.environment)
    local = compute_environment_rates_per_qubit(
        config.environment,
        config.circuit.logical_qubits,
    )
    solver_rates = local if config.environment.per_qubit else shared
    return shared, local, solver_rates


def _trace_error(state: Matrix) -> float:
    return abs(trace(state) - 1.0)


def _as_probability(value: float) -> float:
    # Five-qubit density evolution can accumulate a few ulps more roundoff
    # than the one/two-qubit paths; keep diagnostics physical without masking
    # meaningful (>1e-7) violations.
    if value < 0.0 and value > -1e-7:
        return 0.0
    if value > 1.0 and value < 1.0 + 1e-7:
        return 1.0
    return value


register_simulation_backend(
    DEFAULT_SIMULATION_MODEL,
    _run_weak_coupling_lindblad,
)
register_simulation_backend(
    GATE_AWARE_HAMILTONIAN_LINDBLAD_MODEL,
    _run_gate_aware_hamiltonian_lindblad,
)
register_simulation_backend(
    GATE_AWARE_SPLIT_STEP_MODEL,
    _run_gate_aware_hamiltonian_lindblad,
)
register_simulation_backend(
    POST_CIRCUIT_DEGRADATION_MODEL,
    _run_post_circuit_degradation,
)
