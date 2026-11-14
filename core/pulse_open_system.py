"""Open-system segment orchestration for the Pulse Baseline A reference path."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from core.gates import (
    Matrix,
    multi_qubit_physical_collapse_operators,
    prepare_collapse_operators,
    zero_hamiltonian,
)
from core.cptp_evolution import CPTPSegmentAudit, evolve_cptp_segment
from core.physical_environment import compute_environment_rates
from core.pulse_envelopes import PulseEnvelope, TwoLevelPulseHamiltonian
from core.pulse_evolution import (
    ConstantHamiltonian,
    ResolvedTimeDependentEvolutionBackend,
    TimeDependentEvolutionResult,
    evolve_time_dependent_segment,
)


INPUT_MODE_PHYSICAL = "physical"
DIRECT_RATES_INPUT_MODE = "direct_rates"


@dataclass(frozen=True)
class PulseDissipationRates:
    input_mode: str
    gamma_down_per_us: float
    gamma_up_per_us: float
    gamma_phi_per_us: float
    n_th: float | None = None

    def __post_init__(self) -> None:
        if self.input_mode not in {
            INPUT_MODE_PHYSICAL,
            DIRECT_RATES_INPUT_MODE,
        }:
            raise ValueError(
                "pulse environment input_mode must be physical or direct_rates"
            )
        for field_name in (
            "gamma_down_per_us",
            "gamma_up_per_us",
            "gamma_phi_per_us",
        ):
            value = float(getattr(self, field_name))
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(
                    f"{field_name} must be finite and non-negative"
                )
            object.__setattr__(self, field_name, value)
        if self.n_th is not None:
            occupation = float(self.n_th)
            if not math.isfinite(occupation) or occupation < 0.0:
                raise ValueError("n_th must be finite and non-negative")
            object.__setattr__(self, "n_th", occupation)

    @property
    def gamma_population_relaxation_per_us(self) -> float:
        return self.gamma_down_per_us + self.gamma_up_per_us

    @property
    def collapse_operator_count(self) -> int:
        return sum(
            rate > 0.0
            for rate in (
                self.gamma_down_per_us,
                self.gamma_up_per_us,
                self.gamma_phi_per_us,
            )
        )

    def to_dict(self) -> dict[str, float | str | None]:
        return {
            "input_mode": self.input_mode,
            "gamma_down_per_us": self.gamma_down_per_us,
            "gamma_up_per_us": self.gamma_up_per_us,
            "gamma_phi_per_us": self.gamma_phi_per_us,
            "gamma_population_relaxation_per_us": (
                self.gamma_population_relaxation_per_us
            ),
            "n_th": self.n_th,
        }


@dataclass(frozen=True)
class OpenPulseSequenceResult:
    rates: PulseDissipationRates
    pulse_result: TimeDependentEvolutionResult
    idle_result: TimeDependentEvolutionResult | None
    pulse_duration_us: float
    total_simulation_time_us: float

    @property
    def pulse_end_state(self) -> Matrix:
        return self.pulse_result.state

    @property
    def final_state(self) -> Matrix:
        if self.idle_result is None:
            return self.pulse_result.state
        return self.idle_result.state

    @property
    def idle_duration_us(self) -> float:
        return self.total_simulation_time_us - self.pulse_duration_us


def pulse_dissipation_rates(environment: Any) -> PulseDissipationRates:
    """Normalize physical or direct-rate pulse inputs to canonical rates."""

    # An EnvironmentConfig has no input_mode: it always carries physical times.
    input_mode = str(getattr(environment, "input_mode", INPUT_MODE_PHYSICAL))
    if input_mode == INPUT_MODE_PHYSICAL:
        rates = compute_environment_rates(environment)
        return PulseDissipationRates(
            input_mode=INPUT_MODE_PHYSICAL,
            gamma_down_per_us=rates.gamma_down_per_us,
            gamma_up_per_us=rates.gamma_up_per_us,
            gamma_phi_per_us=rates.gamma_phi_per_us,
            n_th=rates.n_th,
        )
    if input_mode == DIRECT_RATES_INPUT_MODE:
        return PulseDissipationRates(
            input_mode=DIRECT_RATES_INPUT_MODE,
            gamma_down_per_us=getattr(
                environment,
                "gamma_down_per_us",
            ),
            gamma_up_per_us=getattr(
                environment,
                "gamma_up_per_us",
            ),
            gamma_phi_per_us=getattr(
                environment,
                "gamma_phi_per_us",
            ),
        )
    raise ValueError(
        "pulse environment input_mode must be physical or direct_rates"
    )


def evolve_open_pulse_sequence(
    state: Matrix,
    envelope: PulseEnvelope,
    rates: PulseDissipationRates,
    total_simulation_time_us: float,
    max_step_us: float,
    *,
    phase_rad: float = 0.0,
    detuning_rad_per_us: float = 0.0,
    pulse_checkpoint_times_us: Sequence[float] = (),
    idle_checkpoint_times_us: Sequence[float] = (),
    backend: ResolvedTimeDependentEvolutionBackend = "python",
) -> OpenPulseSequenceResult:
    """Run a dissipative pulse followed by an optional zero-H idle segment."""

    total_duration = _positive_finite(
        total_simulation_time_us,
        "total_simulation_time_us",
    )
    pulse_duration = _positive_finite(
        envelope.duration_us,
        "pulse_duration_us",
    )
    if pulse_duration > total_duration:
        raise ValueError(
            "pulse duration must not exceed total_simulation_time_us"
        )

    collapse_ops = prepare_collapse_operators(
        multi_qubit_physical_collapse_operators(
            1,
            rates.gamma_down_per_us,
            rates.gamma_up_per_us,
            rates.gamma_phi_per_us,
        )
    )
    pulse_result = evolve_time_dependent_segment(
        state,
        TwoLevelPulseHamiltonian(
            envelope=envelope,
            phase_rad=phase_rad,
            detuning_rad_per_us=detuning_rad_per_us,
        ),
        collapse_ops,
        duration_us=pulse_duration,
        max_step_us=max_step_us,
        checkpoint_times_us=pulse_checkpoint_times_us,
        backend=backend,
    )

    idle_duration = total_duration - pulse_duration
    idle_result = None
    if idle_duration > 0.0:
        idle_result = evolve_time_dependent_segment(
            pulse_result.state,
            ConstantHamiltonian(zero_hamiltonian(2)),
            collapse_ops,
            duration_us=idle_duration,
            max_step_us=max_step_us,
            checkpoint_times_us=idle_checkpoint_times_us,
            backend=backend,
        )

    return OpenPulseSequenceResult(
        rates=rates,
        pulse_result=pulse_result,
        idle_result=idle_result,
        pulse_duration_us=pulse_duration,
        total_simulation_time_us=total_duration,
    )


def evolve_cptp_open_pulse_sequence(
    state: Matrix,
    envelope: PulseEnvelope,
    rates: PulseDissipationRates,
    total_simulation_time_us: float,
    max_step_us: float,
    *,
    phase_rad: float = 0.0,
    detuning_rad_per_us: float = 0.0,
    pulse_checkpoint_times_us: Sequence[float] = (),
    idle_checkpoint_times_us: Sequence[float] = (),
    backend: ResolvedTimeDependentEvolutionBackend = "python",
) -> tuple[
    OpenPulseSequenceResult,
    CPTPSegmentAudit,
    CPTPSegmentAudit | None,
]:
    """Run the Pulse sequence through audited exponential CPTP maps."""

    total_duration = _positive_finite(
        total_simulation_time_us,
        "total_simulation_time_us",
    )
    pulse_duration = _positive_finite(
        envelope.duration_us,
        "pulse_duration_us",
    )
    if pulse_duration > total_duration:
        raise ValueError(
            "pulse duration must not exceed total_simulation_time_us"
        )
    collapse_ops = prepare_collapse_operators(
        multi_qubit_physical_collapse_operators(
            1,
            rates.gamma_down_per_us,
            rates.gamma_up_per_us,
            rates.gamma_phi_per_us,
        )
    )
    pulse_cptp = evolve_cptp_segment(
        state,
        TwoLevelPulseHamiltonian(
            envelope=envelope,
            phase_rad=phase_rad,
            detuning_rad_per_us=detuning_rad_per_us,
        ),
        collapse_ops,
        pulse_duration,
        max_step_us,
        checkpoint_times_us=pulse_checkpoint_times_us,
        backend=backend,
    )
    idle_duration = total_duration - pulse_duration
    idle_cptp = None
    if idle_duration > 0.0:
        idle_cptp = evolve_cptp_segment(
            pulse_cptp.evolution.state,
            ConstantHamiltonian(zero_hamiltonian(2)),
            collapse_ops,
            idle_duration,
            max_step_us,
            checkpoint_times_us=idle_checkpoint_times_us,
            backend=backend,
        )
    result = OpenPulseSequenceResult(
        rates=rates,
        pulse_result=pulse_cptp.evolution,
        idle_result=None if idle_cptp is None else idle_cptp.evolution,
        pulse_duration_us=pulse_duration,
        total_simulation_time_us=total_duration,
    )
    return (
        result,
        pulse_cptp.audit,
        None if idle_cptp is None else idle_cptp.audit,
    )


def _positive_finite(value: float, field_name: str) -> float:
    converted = float(value)
    if not math.isfinite(converted) or converted <= 0.0:
        raise ValueError(f"{field_name} must be finite and greater than 0")
    return converted
