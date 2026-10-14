"""Bounded API service for scheduled 1-4 transmon pulse networks."""

from __future__ import annotations

import math
from copy import deepcopy
from time import perf_counter

from api.pulse_models import (
    CoupledTransmonNetworkPulseSimulateRequest,
    CoupledTransmonNetworkPulseSimulateResponse,
    QutritPulseEnvelopeRequest,
)
from api.pulse_readout import pulse_readout_response
from core.cptp_evolution import evolve_cptp_segment
from core.gates import Matrix
from core.pulse_envelopes import (
    GaussianPulseEnvelope,
    PulseEnvelope,
    SquarePulseEnvelope,
)
from core.pulse_evolution import (
    TimeDependentCheckpoint,
    evolve_dense_time_dependent_segment,
    physicality_metrics,
    resolve_time_dependent_backend,
)
from core.pulse_qutrit_contract import transmon_anharmonicity_rad_per_us
from core.pulse_qutrit_open_system import (
    QutritDissipationRates,
    qutrit_dissipation_rates,
)
from core.pulse_step_policy import recommended_qutrit_step_policy
from core.pulse_transmon_network import (
    CoupledTransmonNetworkHamiltonian,
    ScheduledTransmonDrive,
    TransmonExchangeCoupling,
    computational_basis_labels,
    network_basis_labels,
    network_collapse_operators,
    network_initial_density_matrix,
    network_joint_populations,
    network_leakage_probability,
    network_site_local_dissipator,
)
from core.quasi_static_noise import correlated_gaussian_detuning_chain_samples
from core.results import EnvironmentConfig


NETWORK_CONTRACT_VERSION = "pulse-transmon-network-v1"
NETWORK_DENSE_KERNEL_ID = "numpy_dense"
# Explicit CPTP composes an audited GKSL exponential per output interval, which
# is far heavier than one RK4 step, so it runs on a tighter interval budget.
NETWORK_CPTP_MAX_INTERVALS = 500
# How much coarser than the RK4 accuracy cap one CPTP interval may be.  The
# midpoint-frozen exponential converges second order in the interval, so this
# trades a factor of NETWORK_CPTP_STEP_RELAXATION^2 in accuracy for the same
# factor fewer matrix exponentials.  Three keeps the QuTiP comparison near
# 2e-5 with room to spare against the 5e-5 audit tolerance, and leaves the
# busiest audited case at roughly 380 of the 500 permitted intervals.
NETWORK_CPTP_STEP_RELAXATION = 3.0
# Conservative ceiling on the quasi-static ensemble: order**count single
# trajectories weight-averaged.  order 5 with four transmons would be 625
# evolutions, so the ensemble work is checked against this before running.
NETWORK_ENSEMBLE_MAX_INTERNAL_STEPS = 400_000
# One internal step costs a fixed per-step setup plus dense work that grows
# with hilbert_dimension^3, so the budget charges both.  The overhead term is
# expressed in the same units as the dense term: on the reference machine a
# step at Hilbert dimension 9 costs about as much as 12000 units of dense work.
NETWORK_STEP_OVERHEAD_UNITS = 12_000
# Roughly 94,000 two-transmon steps, 37,000 three-transmon steps, or 2,200
# four-transmon steps, which keeps the slowest accepted request near 20 s on
# the reference machine and inside the 90 s API timeout on slower hosts.
NETWORK_MAX_DENSE_WORK_UNITS = 1_200_000_000
NETWORK_MAX_RESPONSE_MATRIX_ELEMENTS = 250_000


def network_step_work_units(dimension: int) -> int:
    """Return the budgeted cost of one internal step at this dimension."""

    return dimension ** 3 + NETWORK_STEP_OVERHEAD_UNITS


def run_coupled_transmon_network_request(
    request: CoupledTransmonNetworkPulseSimulateRequest,
) -> dict[str, object]:
    """Dispatch a network request, averaging over quasi-static noise if asked."""

    sigmas = tuple(request.quasi_static_detuning_sigmas_rad_per_us)
    if not sigmas or all(value == 0.0 for value in sigmas):
        return _run_single_network_request(request)

    from api.pulse_service import PulseExecutionLimitError

    started = perf_counter()
    count = request.transmon_count
    samples = correlated_gaussian_detuning_chain_samples(
        sigmas,
        request.quasi_static_detuning_adjacent_correlation,
        request.quasi_static_quadrature_order,
    )
    samples = tuple(sorted(
        samples,
        key=lambda item: max(
            abs(request.detunings_rad_per_us[index] + item[0][index])
            for index in range(count)
        ),
        reverse=True,
    ))
    responses: list[dict[str, object]] = []
    for index, (offsets, _) in enumerate(samples):
        sample_request = request.model_copy(update={
            "detunings_rad_per_us": [
                request.detunings_rad_per_us[subsystem] + offsets[subsystem]
                for subsystem in range(count)
            ],
            "quasi_static_detuning_sigmas_rad_per_us": [],
        })
        response = _run_single_network_request(sample_request)
        responses.append(response)
        if index == 0:
            worst_steps = int(
                response["step_policy"]["estimated_internal_step_count"]
            )
            if worst_steps * len(samples) > NETWORK_ENSEMBLE_MAX_INTERNAL_STEPS:
                raise PulseExecutionLimitError(
                    "Network quasi-static ensemble exceeds the work limit: "
                    f"conservative estimate {worst_steps * len(samples)}, "
                    f"maximum {NETWORK_ENSEMBLE_MAX_INTERNAL_STEPS}."
                )
    return _average_network_ensemble_response(
        request,
        responses,
        samples,
        (perf_counter() - started) * 1000.0,
    )


def _run_single_network_request(
    request: CoupledTransmonNetworkPulseSimulateRequest,
) -> dict[str, object]:
    from api.pulse_service import PulseExecutionLimitError

    started = perf_counter()
    count = request.transmon_count
    local_dimension = request.local_levels
    dimension = local_dimension ** count
    is_cptp = request.evolution_method == "explicit_cptp"
    resolved_backend = resolve_time_dependent_backend(request.backend)
    envelopes = tuple(_build_envelope(item.pulse) for item in request.drives)
    rates = _build_rates(request)
    # A transmon always has a physical negative anharmonicity.  At two local
    # levels the network Hamiltonian slices off the |2> row the term lives in,
    # so it only shapes the integration-step policy there; at three levels it
    # is the leakage detuning.
    alphas = tuple(
        transmon_anharmonicity_rad_per_us(value)
        for value in request.anharmonicities_mhz
    )
    detunings = tuple(float(value) for value in request.detunings_rad_per_us)
    scheduled_drives = tuple(
        ScheduledTransmonDrive(
            target=item.target,
            start_time_us=item.start_time_us,
            envelope=envelope,
            phase_rad=item.pulse.phase_rad,
            detuning_rad_per_us=item.pulse.detuning_rad_per_us,
            drag_beta_us=item.pulse.drag_beta_us,
        )
        for item, envelope in zip(request.drives, envelopes, strict=True)
    )
    couplings = tuple(
        TransmonExchangeCoupling(
            left=item.left,
            right=item.right,
            strength_rad_per_us=item.exchange_coupling_rad_per_us,
        )
        for item in request.couplings
    )

    boundary_times = [
        value
        for drive in scheduled_drives
        for value in (drive.start_time_us, drive.end_time_us)
    ]
    sample_times = _sample_times(
        request.total_simulation_time_us,
        request.snapshot_options.uniform_count,
        request.snapshot_options.custom_times_us,
        boundary_times,
    )
    response_elements = len(sample_times) * dimension * dimension
    if response_elements > NETWORK_MAX_RESPONSE_MATRIX_ELEMENTS:
        raise PulseExecutionLimitError(
            "Transmon network response exceeds the density-matrix element "
            f"limit: estimated {response_elements}, maximum "
            f"{NETWORK_MAX_RESPONSE_MATRIX_ELEMENTS}. Reduce snapshot count."
        )

    reference_policies = [
        recommended_qutrit_step_policy(
            envelope,
            abs(detunings[item.target]) + abs(item.pulse.detuning_rad_per_us),
            min(alphas),
            rates[item.target],
            request.total_simulation_time_us,
            maximum_internal_step_count=NETWORK_MAX_DENSE_WORK_UNITS,
            drag_beta_us=item.pulse.drag_beta_us,
        )
        for item, envelope in zip(request.drives, envelopes, strict=True)
    ]
    maximum_coupling = max(
        (coupling.strength_rad_per_us for coupling in couplings),
        default=0.0,
    )
    coupling_step_limit = (
        None if maximum_coupling == 0.0 else 0.02 / (4.0 * maximum_coupling)
    )
    rk4_step = min(
        min(policy.selected_internal_step_cap_us for policy in reference_policies),
        coupling_step_limit or math.inf,
    )
    total_time = request.total_simulation_time_us
    latest_drive_end = max(drive.end_time_us for drive in scheduled_drives)
    # The GKSL exponential is unconditionally stable, so CPTP survives steps
    # that would break RK4.  Stability is not accuracy though: each interval
    # freezes the Hamiltonian at its midpoint, so a step that does not resolve
    # the drive envelope still integrates the wrong pulse.  The error falls
    # second order in the interval, so the cap stays tied to the RK4 accuracy
    # limit and only relaxes it by a small constant factor.
    cptp_step = min(
        rk4_step * NETWORK_CPTP_STEP_RELAXATION,
        max(latest_drive_end, total_time) / 8.0,
        coupling_step_limit or math.inf,
    )
    integration_step = cptp_step if is_cptp else rk4_step
    estimated_steps = _estimated_steps(sample_times, integration_step)
    step_work_units = network_step_work_units(dimension)
    work_units = estimated_steps * step_work_units
    maximum_steps_for_dimension = (
        NETWORK_MAX_DENSE_WORK_UNITS // step_work_units
    )
    if is_cptp:
        if estimated_steps > NETWORK_CPTP_MAX_INTERVALS:
            raise PulseExecutionLimitError(
                "Transmon network explicit-CPTP request exceeds the interval "
                f"limit: estimated {estimated_steps}, maximum "
                f"{NETWORK_CPTP_MAX_INTERVALS}. Each interval composes an "
                "audited channel, and the interval width is set by the drive "
                "and anharmonicity scales, so a large |anharmonicity| or a "
                "long observation window needs many of them. Shorten the "
                "simulation time, widen the pulse, or switch to "
                "fixed_step_rk4."
            )
    elif work_units > NETWORK_MAX_DENSE_WORK_UNITS:
        raise PulseExecutionLimitError(
            "Transmon network request exceeds the dimension-aware dense-work "
            f"limit: {estimated_steps} steps at Hilbert dimension {dimension} "
            f"({work_units} units), maximum {NETWORK_MAX_DENSE_WORK_UNITS}. "
            f"At this dimension the request must stay within "
            f"{maximum_steps_for_dimension} internal steps."
        )

    hamiltonian = CoupledTransmonNetworkHamiltonian(
        anharmonicities_rad_per_us=alphas,
        detunings_rad_per_us=detunings,
        couplings=couplings,
        drives=scheduled_drives,
        local_dimension=local_dimension,
    )
    initial = network_initial_density_matrix(
        request.initial_state, count, local_dimension
    )
    if is_cptp:
        cptp = evolve_cptp_segment(
            initial,
            hamiltonian,
            network_collapse_operators(rates, local_dimension),
            total_time,
            integration_step,
            checkpoint_times_us=sample_times,
            backend=resolved_backend,
        )
        evolution = cptp.evolution
        cptp_audit = cptp.audit
    else:
        evolution = evolve_dense_time_dependent_segment(
            initial,
            hamiltonian,
            network_site_local_dissipator(rates, local_dimension),
            total_time,
            integration_step,
            checkpoint_times_us=sample_times,
        )
        cptp_audit = None

    trajectory = [
        _trajectory_point(point.time_us, point, request, scheduled_drives)
        for point in evolution.checkpoints
    ]
    pulse_end = min(
        trajectory,
        key=lambda point: abs(float(point["time_us"]) - latest_drive_end),
    )
    final = trajectory[-1]
    response = {
        "contract_version": NETWORK_CONTRACT_VERSION,
        "model": {
            "model_id": request.model_id,
            "description": (
                f"scheduled {count}-transmon rotating-frame RWA network"
                if count == 1
                else f"scheduled coupled {count}-transmon rotating-frame RWA network"
            ),
            "logical_qubits": count,
            "local_levels": local_dimension,
            "hilbert_dimension": dimension,
            "density_matrix_dimension": dimension,
            "basis_order": list(network_basis_labels(count, local_dimension)),
            "subsystem_dimensions": [local_dimension] * count,
            "frame": "local rotating frames",
            "approximation": "RWA",
            "hardware_calibrated": False,
            "experimental": True,
        },
        "input": {
            "initial_state": request.initial_state,
            "transmon_count": count,
            "local_levels": local_dimension,
            "frequencies_ghz": request.frequencies_ghz,
            "anharmonicities_mhz": request.anharmonicities_mhz,
            "detunings_rad_per_us": request.detunings_rad_per_us,
            "effective_detunings_rad_per_us": list(detunings),
            "couplings": [item.model_dump() for item in request.couplings],
            "drives": [item.model_dump() for item in request.drives],
            "drive_count": len(request.drives),
            "quasi_static_detuning_sigmas_rad_per_us": [0.0] * count,
            "total_simulation_time_us": request.total_simulation_time_us,
            "sample_count": len(trajectory),
        },
        "rates": [rate.to_dict() for rate in rates],
        "step_policy": {
            "policy_id": (
                "coupled_transmon_network_explicit_cptp_v1"
                if is_cptp
                else "coupled_transmon_network_dense_work_v2"
            ),
            "selected_internal_step_cap_us": integration_step,
            "single_qutrit_step_cap_us": min(
                policy.selected_internal_step_cap_us
                for policy in reference_policies
            ),
            "coupling_step_limit_us": coupling_step_limit,
            "estimated_internal_step_count": estimated_steps,
            "maximum_internal_step_count_for_dimension": maximum_steps_for_dimension,
            "maximum_cptp_interval_count": (
                NETWORK_CPTP_MAX_INTERVALS if is_cptp else None
            ),
            "dense_work_units_per_step": step_work_units,
            "step_overhead_work_units": NETWORK_STEP_OVERHEAD_UNITS,
            "estimated_dense_work_units": work_units,
            "maximum_dense_work_units": NETWORK_MAX_DENSE_WORK_UNITS,
            "estimated_response_matrix_elements": response_elements,
            "maximum_response_matrix_elements": NETWORK_MAX_RESPONSE_MATRIX_ELEMENTS,
            "within_work_budget": True,
        },
        "sample_times_us": [point["time_us"] for point in trajectory],
        "trajectory": trajectory,
        "leakage": {
            "maximum_recorded_leakage_probability": max(
                float(point["leakage_probability"]) for point in trajectory
            ),
            "leakage_at_pulse_end": pulse_end["leakage_probability"],
            "leakage_at_final_time": final["leakage_probability"],
        },
        "pulse_end": pulse_end,
        "final": final,
        "readout": pulse_readout_response(
            request.readout_error,
            final["joint_populations"],
            count,
            local_dimension,
        ),
        "diagnostics": {
            "api_runtime_ms": (perf_counter() - started) * 1000.0,
            "backend": {
                "requested": request.backend,
                "resolved": NETWORK_DENSE_KERNEL_ID,
                "fallback_used": False,
                "note": (
                    "the network path always uses the NumPy dense kernel; the "
                    "python and rust selection applies to other pulse models"
                ),
            },
            "evolution": {
                "requested": request.evolution_method,
                "resolved": request.evolution_method,
                "method_id": (
                    "coupled_transmon_network_explicit_cptp_midpoint_v1"
                    if is_cptp
                    else "coupled_transmon_network_fixed_step_rk4_v2"
                ),
                "cptp_guaranteed_by_construction": is_cptp,
                "cleanup_applied": not is_cptp,
                "open_pulse_audit": (
                    None if cptp_audit is None else cptp_audit.to_dict()
                ),
                "open_idle_audit": None,
            },
            "open_pulse": evolution.diagnostics.to_dict(),
            "open_idle": None,
            "hamiltonian": {
                "local_model": "Duffing qutrit truncation",
                "coupling_model": "sum J_ij(a_i^dagger a_j + a_i a_j^dagger)",
                "drive_model": "independently scheduled local rotating-frame I/Q envelopes",
                "pulse_detuning_model": "phase ramp in each local rotating frame",
            },
            "dissipator": {
                "model": "site-local qutrit jump operators",
                "application": (
                    "one 9x9 sum_j l_j (x) conj(l_j) kernel per transmon on the "
                    "paired row and column axes"
                ),
                "coherent_term": (
                    "non-Hermitian effective Hamiltonian H - 0.5j sum_j "
                    "l_j^dagger l_j"
                ),
            },
        },
        "warnings": [
            "Experimental educational transmon-network model; not calibrated hardware.",
            "Coherent RK4 cost grows as 3^(3N) per matrix multiplication proxy.",
        ],
        "limitations": [
            "One to four transmons with two or three local levels each.",
            "Exchange coupling and local rotating-wave approximation only.",
            "Fixed-step RK4 with cleanup, or audited explicit CPTP up to Hilbert "
            "dimension 9.",
            "No crosstalk, transfer function, tunable-coupler dynamics, or calibration model.",
        ],
    }
    return CoupledTransmonNetworkPulseSimulateResponse.model_validate(
        response
    ).model_dump()


def _average_network_ensemble_response(
    request: CoupledTransmonNetworkPulseSimulateRequest,
    responses: list[dict[str, object]],
    samples: tuple[tuple[tuple[float, ...], float], ...],
    runtime_ms: float,
) -> dict[str, object]:
    """Weight-average complete trajectories over the quasi-static ensemble."""

    count = request.transmon_count
    local_dimension = request.local_levels
    weights = [weight for _, weight in samples]
    averaged = deepcopy(responses[0])
    trajectories = [response["trajectory"] for response in responses]
    averaged["trajectory"] = [
        _average_network_point(
            [trajectory[index] for trajectory in trajectories],
            weights,
            count,
            local_dimension,
        )
        for index in range(len(trajectories[0]))
    ]
    averaged["pulse_end"] = _average_network_point(
        [response["pulse_end"] for response in responses],
        weights,
        count,
        local_dimension,
    )
    averaged["final"] = _average_network_point(
        [response["final"] for response in responses],
        weights,
        count,
        local_dimension,
    )
    trajectory = averaged["trajectory"]
    averaged["leakage"] = {
        "maximum_recorded_leakage_probability": max(
            float(point["leakage_probability"]) for point in trajectory
        ),
        "leakage_at_pulse_end": averaged["pulse_end"]["leakage_probability"],
        "leakage_at_final_time": averaged["final"]["leakage_probability"],
    }
    averaged["readout"] = pulse_readout_response(
        request.readout_error,
        averaged["final"]["joint_populations"],
        count,
        local_dimension,
    )
    averaged["input"].update({
        "detunings_rad_per_us": request.detunings_rad_per_us,
        "quasi_static_detuning_sigmas_rad_per_us": (
            request.quasi_static_detuning_sigmas_rad_per_us
        ),
        "quasi_static_detuning_adjacent_correlation": (
            request.quasi_static_detuning_adjacent_correlation
        ),
        "quasi_static_quadrature_order": request.quasi_static_quadrature_order,
    })
    averaged["step_policy"]["estimated_internal_step_count"] = sum(
        int(response["step_policy"]["estimated_internal_step_count"])
        for response in responses
    )
    diagnostics = averaged["diagnostics"]
    diagnostics["api_runtime_ms"] = runtime_ms
    diagnostics["quasi_static_noise"] = {
        "enabled": True,
        "model_id": "correlated_gaussian_chain_detuning_v1",
        "distribution": "delta ~ Normal(0, tridiagonal covariance)",
        "sigmas_rad_per_us": list(
            request.quasi_static_detuning_sigmas_rad_per_us
        ),
        "adjacent_correlation": (
            request.quasi_static_detuning_adjacent_correlation
        ),
        "quadrature_method": "tensor-product Gauss-Hermite",
        "quadrature_order_per_axis": request.quasi_static_quadrature_order,
        "sample_count": len(samples),
        "samples": [
            {"offsets_rad_per_us": list(offsets), "weight": weight}
            for offsets, weight in samples
        ],
        "covariance_model": (
            "Sigma_ij = sigma_i sigma_j * (1 if i==j else r if |i-j|==1 "
            "else 0)"
        ),
    }
    averaged["warnings"] = [
        *averaged["warnings"],
        "Correlated Gaussian quasi-static detuning is averaged over complete "
        f"{count}-transmon trajectories.",
    ]
    return CoupledTransmonNetworkPulseSimulateResponse.model_validate(
        averaged
    ).model_dump()


def _average_network_point(
    points: list[dict[str, object]],
    weights: list[float],
    count: int,
    local_dimension: int,
) -> dict[str, object]:
    averaged = deepcopy(points[0])
    dimension = local_dimension ** count
    state = _weighted_network_density_matrix(
        [point["density_matrix"] for point in points],
        weights,
        dimension,
    )
    populations = network_joint_populations(state, count, local_dimension)
    metrics = physicality_metrics(state)
    averaged.update({
        "joint_populations": populations,
        "computational_population": sum(
            populations[label]
            for label in computational_basis_labels(count)
        ),
        "leakage_probability": network_leakage_probability(
            state, count, local_dimension
        ),
        "population_sum_error": abs(sum(populations.values()) - 1.0),
        "purity": float(sum(
            abs(value) ** 2 for row in state for value in row
        )),
        "density_matrix": _matrix_response(state),
        "raw_physicality": {
            "trace_error": max(
                point["raw_physicality"]["trace_error"] for point in points
            ),
            "hermiticity_error": max(
                point["raw_physicality"]["hermiticity_error"]
                for point in points
            ),
            "minimum_eigenvalue": min(
                point["raw_physicality"]["minimum_eigenvalue"]
                for point in points
            ),
        },
        "cleaned_physicality": _physicality_response(metrics),
        "cleanup_correction_norm": sum(
            weight * point["cleanup_correction_norm"]
            for point, weight in zip(points, weights, strict=True)
        ),
    })
    return averaged


def _weighted_network_density_matrix(
    matrices: list[list[list[dict[str, float]]]],
    weights: list[float],
    dimension: int,
) -> Matrix:
    return tuple(
        tuple(
            sum(
                weight * complex(
                    matrix[row][column]["real"],
                    matrix[row][column]["imag"],
                )
                for matrix, weight in zip(matrices, weights, strict=True)
            )
            for column in range(dimension)
        )
        for row in range(dimension)
    )


def _trajectory_point(
    time_us: float,
    point: TimeDependentCheckpoint,
    request: CoupledTransmonNetworkPulseSimulateRequest,
    drives: tuple[ScheduledTransmonDrive, ...],
) -> dict[str, object]:
    state = point.cleaned_state
    count = request.transmon_count
    local_dimension = request.local_levels
    populations = network_joint_populations(state, count, local_dimension)
    metrics = physicality_metrics(state)
    segment = "pulse" if any(
        drive.start_time_us - 1e-14 <= time_us <= drive.end_time_us + 1e-14
        for drive in drives
    ) else "idle"
    return {
        "time_us": time_us,
        "segment": segment,
        "joint_populations": populations,
        "computational_population": sum(
            populations[label] for label in computational_basis_labels(count)
        ),
        "leakage_probability": network_leakage_probability(
            state, count, local_dimension
        ),
        "population_sum_error": abs(sum(populations.values()) - 1.0),
        "purity": float(sum(
            abs(value) ** 2 for row in state for value in row
        )),
        "density_matrix": _matrix_response(state),
        "raw_physicality": _physicality_response(point.raw_physicality),
        "cleaned_physicality": _physicality_response(metrics),
        "cleanup_correction_norm": point.cleanup_correction_norm,
    }


def _build_envelope(pulse: QutritPulseEnvelopeRequest) -> PulseEnvelope:
    if pulse.shape == "square":
        assert pulse.pulse_duration_us is not None
        if pulse.amplitude_mode == "target_rotation_angle":
            assert pulse.target_rotation_angle_rad is not None
            return SquarePulseEnvelope.from_target_rotation_angle(
                pulse.target_rotation_angle_rad,
                pulse.pulse_duration_us,
            )
        assert pulse.peak_amplitude_rad_per_us is not None
        return SquarePulseEnvelope(
            pulse.peak_amplitude_rad_per_us,
            pulse.pulse_duration_us,
        )
    assert pulse.sigma_us is not None
    assert pulse.truncation_sigma is not None
    if pulse.amplitude_mode == "target_rotation_angle":
        assert pulse.target_rotation_angle_rad is not None
        return GaussianPulseEnvelope.from_target_rotation_angle(
            pulse.target_rotation_angle_rad,
            pulse.sigma_us,
            pulse.truncation_sigma,
        )
    assert pulse.peak_amplitude_rad_per_us is not None
    return GaussianPulseEnvelope(
        pulse.peak_amplitude_rad_per_us,
        pulse.sigma_us,
        pulse.truncation_sigma,
    )


def _build_rates(
    request: CoupledTransmonNetworkPulseSimulateRequest,
) -> tuple[QutritDissipationRates, ...]:
    environment = request.environment
    if environment.input_mode == "direct_rates":
        return tuple(
            qutrit_dissipation_rates(environment, anharmonicity)
            for anharmonicity in request.anharmonicities_mhz
        )
    local_calibrations = environment.per_qubit
    return tuple(
        qutrit_dissipation_rates(
            EnvironmentConfig(
                input_mode="physical",
                device_quality=environment.device_quality,
                temperature_mk=environment.temperature_mk,
                flux_noise_phi0=environment.flux_noise_phi0,
                qubit_frequency_ghz=frequency,
                t1_max_us=(
                    local_calibrations[index].t1_max_us
                    if local_calibrations else environment.t1_max_us
                ),
                tphi_max_us=(
                    local_calibrations[index].tphi_max_us
                    if local_calibrations else environment.tphi_max_us
                ),
                ideal_reference=environment.ideal_reference,
            ),
            anharmonicity,
        )
        for index, (frequency, anharmonicity) in enumerate(zip(
            request.frequencies_ghz,
            request.anharmonicities_mhz,
            strict=True,
        ))
    )


def _sample_times(
    total: float,
    count: int,
    custom: list[float],
    boundaries: list[float],
) -> list[float]:
    values = {0.0, float(total), *map(float, custom), *map(float, boundaries)}
    if count >= 2:
        values.update(total * index / (count - 1) for index in range(count))
    ordered = sorted(values)
    normalized: list[float] = []
    for value in ordered:
        if normalized and abs(value - normalized[-1]) <= 1e-14:
            continue
        normalized.append(value)
    return normalized


def _estimated_steps(boundaries: list[float], max_step: float) -> int:
    total = 0
    previous = 0.0
    for boundary in boundaries:
        interval = boundary - previous
        if interval > 1e-15:
            total += math.ceil(interval / max_step)
        previous = boundary
    return total


def _physicality_response(metrics) -> dict[str, float]:
    return {
        "trace_error": metrics.trace_error,
        "hermiticity_error": metrics.hermiticity_error,
        "minimum_eigenvalue": metrics.minimum_eigenvalue,
    }


def _matrix_response(matrix: Matrix) -> list[list[dict[str, float]]]:
    return [[
        {"real": float(value.real), "imag": float(value.imag)}
        for value in row
    ] for row in matrix]
