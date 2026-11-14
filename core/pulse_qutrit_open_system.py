"""Open-system qutrit pulse evolution for Pulse Extension B."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from core.constants import BOLTZMANN_CONSTANT, PLANCK_CONSTANT
from core.cptp_evolution import CPTPSegmentAudit, evolve_cptp_segment
from core.gates import (
    CachedCollapseOperator,
    Matrix,
    prepare_collapse_operators,
    scale,
)
from core.physical_environment import (
    compute_environment_rates,
    compute_thermal_occupation,
)
from core.pulse_envelopes import PulseEnvelope
from core.pulse_evolution import (
    ConstantHamiltonian,
    ResolvedTimeDependentEvolutionBackend,
    TimeDependentEvolutionResult,
    evolve_time_dependent_segment,
)
from core.pulse_open_system import DIRECT_RATES_INPUT_MODE, INPUT_MODE_PHYSICAL
from core.pulse_qutrit import (
    QutritLeakageSummary,
    QutritPopulationPoint,
    QutritPulseHamiltonian,
    qutrit_populations,
)
from core.pulse_qutrit_contract import (
    NUMBER_QUTRIT,
    qutrit_rotating_frame_hamiltonian,
    transition_12_frequency_ghz,
)


QUTRIT_DEPHASING_MODEL = "number_operator_adjacent_rate_v1"
TRANSITION_10: Matrix = (
    (0.0 + 0.0j, 1.0 + 0.0j, 0.0 + 0.0j),
    (0.0 + 0.0j, 0.0 + 0.0j, 0.0 + 0.0j),
    (0.0 + 0.0j, 0.0 + 0.0j, 0.0 + 0.0j),
)
TRANSITION_01: Matrix = (
    (0.0 + 0.0j, 0.0 + 0.0j, 0.0 + 0.0j),
    (1.0 + 0.0j, 0.0 + 0.0j, 0.0 + 0.0j),
    (0.0 + 0.0j, 0.0 + 0.0j, 0.0 + 0.0j),
)
TRANSITION_21: Matrix = (
    (0.0 + 0.0j, 0.0 + 0.0j, 0.0 + 0.0j),
    (0.0 + 0.0j, 0.0 + 0.0j, 1.0 + 0.0j),
    (0.0 + 0.0j, 0.0 + 0.0j, 0.0 + 0.0j),
)
TRANSITION_12: Matrix = (
    (0.0 + 0.0j, 0.0 + 0.0j, 0.0 + 0.0j),
    (0.0 + 0.0j, 0.0 + 0.0j, 0.0 + 0.0j),
    (0.0 + 0.0j, 1.0 + 0.0j, 0.0 + 0.0j),
)


@dataclass(frozen=True)
class QutritDissipationRates:
    """Canonical transition-specific qutrit rates in inverse microseconds."""

    input_mode: str
    gamma_10_down_per_us: float
    gamma_01_up_per_us: float
    gamma_21_down_per_us: float
    gamma_12_up_per_us: float
    gamma_phi_adjacent_per_us: float
    transition_01_frequency_ghz: float | None = None
    transition_12_frequency_ghz: float | None = None
    n_01: float | None = None
    n_12: float | None = None
    gamma_10_zero_temperature_per_us: float | None = None
    gamma_21_zero_temperature_per_us: float | None = None
    dephasing_model: str = QUTRIT_DEPHASING_MODEL

    def __post_init__(self) -> None:
        if self.input_mode not in {
            INPUT_MODE_PHYSICAL,
            DIRECT_RATES_INPUT_MODE,
        }:
            raise ValueError(
                "qutrit environment input_mode must be physical or "
                "direct_rates"
            )
        for field_name in (
            "gamma_10_down_per_us",
            "gamma_01_up_per_us",
            "gamma_21_down_per_us",
            "gamma_12_up_per_us",
            "gamma_phi_adjacent_per_us",
        ):
            object.__setattr__(
                self,
                field_name,
                _nonnegative_finite(getattr(self, field_name), field_name),
            )
        for field_name in (
            "transition_01_frequency_ghz",
            "transition_12_frequency_ghz",
            "n_01",
            "n_12",
            "gamma_10_zero_temperature_per_us",
            "gamma_21_zero_temperature_per_us",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _nonnegative_finite(value, field_name),
                )
        if self.dephasing_model != QUTRIT_DEPHASING_MODEL:
            raise ValueError(
                f"dephasing_model must be {QUTRIT_DEPHASING_MODEL}"
            )

    @property
    def collapse_operator_count(self) -> int:
        return sum(
            rate > 0.0
            for rate in (
                self.gamma_10_down_per_us,
                self.gamma_01_up_per_us,
                self.gamma_21_down_per_us,
                self.gamma_12_up_per_us,
                self.gamma_phi_adjacent_per_us,
            )
        )

    def population_outflow_rate_per_us(self, level: int) -> float:
        """Return the sum of incoherent transition rates leaving one level."""

        if level == 0:
            return self.gamma_01_up_per_us
        if level == 1:
            return (
                self.gamma_10_down_per_us
                + self.gamma_12_up_per_us
            )
        if level == 2:
            return self.gamma_21_down_per_us
        raise ValueError("qutrit level must be 0, 1, or 2")

    def population_induced_coherence_decay_per_us(
        self,
        level_a: int,
        level_b: int,
    ) -> float:
        """Return half the summed population outflow for one coherence."""

        if level_a == level_b:
            return 0.0
        return 0.5 * (
            self.population_outflow_rate_per_us(level_a)
            + self.population_outflow_rate_per_us(level_b)
        )

    def pure_dephasing_coherence_decay_per_us(
        self,
        level_a: int,
        level_b: int,
    ) -> float:
        """Return gamma_phi_adjacent * (level_a - level_b)^2."""

        if level_a not in (0, 1, 2) or level_b not in (0, 1, 2):
            raise ValueError("qutrit level must be 0, 1, or 2")
        return (
            self.gamma_phi_adjacent_per_us
            * float((level_a - level_b) ** 2)
        )

    def to_dict(self) -> dict[str, float | str | None]:
        return {
            "input_mode": self.input_mode,
            "gamma_10_down_per_us": self.gamma_10_down_per_us,
            "gamma_01_up_per_us": self.gamma_01_up_per_us,
            "gamma_21_down_per_us": self.gamma_21_down_per_us,
            "gamma_12_up_per_us": self.gamma_12_up_per_us,
            "gamma_phi_adjacent_per_us": (
                self.gamma_phi_adjacent_per_us
            ),
            "transition_01_frequency_ghz": (
                self.transition_01_frequency_ghz
            ),
            "transition_12_frequency_ghz": (
                self.transition_12_frequency_ghz
            ),
            "n_01": self.n_01,
            "n_12": self.n_12,
            "gamma_10_zero_temperature_per_us": (
                self.gamma_10_zero_temperature_per_us
            ),
            "gamma_21_zero_temperature_per_us": (
                self.gamma_21_zero_temperature_per_us
            ),
            "dephasing_model": self.dephasing_model,
        }


@dataclass(frozen=True)
class OpenQutritSequenceResult:
    rates: QutritDissipationRates
    pulse_result: TimeDependentEvolutionResult
    idle_result: TimeDependentEvolutionResult | None
    pulse_duration_us: float
    total_simulation_time_us: float
    trajectory: tuple[QutritPopulationPoint, ...]
    leakage: QutritLeakageSummary

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


def qutrit_dissipation_rates(
    environment: Any,
    anharmonicity_mhz: float,
) -> QutritDissipationRates:
    """Normalize physical or explicit qutrit-rate inputs."""

    # An EnvironmentConfig has no input_mode: it always carries physical times.
    input_mode = str(getattr(environment, "input_mode", INPUT_MODE_PHYSICAL))
    if input_mode == INPUT_MODE_PHYSICAL:
        two_level_profile_rates = compute_environment_rates(environment)
        frequency_01 = _positive_finite(
            getattr(environment, "qubit_frequency_ghz"),
            "qubit_frequency_ghz",
        )
        frequency_12 = transition_12_frequency_ghz(
            frequency_01,
            anharmonicity_mhz,
        )
        if two_level_profile_rates.ideal_reference:
            n_01 = 0.0
            n_12 = 0.0
        else:
            n_01 = compute_thermal_occupation(
                two_level_profile_rates.temperature_mk,
                frequency_01,
            )
            n_12 = compute_thermal_occupation(
                two_level_profile_rates.temperature_mk,
                frequency_12,
            )
        gamma_10_zero = two_level_profile_rates.gamma0_per_us
        gamma_21_zero = 2.0 * gamma_10_zero
        return QutritDissipationRates(
            input_mode=INPUT_MODE_PHYSICAL,
            gamma_10_down_per_us=gamma_10_zero * (n_01 + 1.0),
            gamma_01_up_per_us=gamma_10_zero * n_01,
            gamma_21_down_per_us=gamma_21_zero * (n_12 + 1.0),
            gamma_12_up_per_us=gamma_21_zero * n_12,
            gamma_phi_adjacent_per_us=(
                two_level_profile_rates.gamma_phi_per_us
            ),
            transition_01_frequency_ghz=frequency_01,
            transition_12_frequency_ghz=frequency_12,
            n_01=n_01,
            n_12=n_12,
            gamma_10_zero_temperature_per_us=gamma_10_zero,
            gamma_21_zero_temperature_per_us=gamma_21_zero,
        )
    if input_mode == DIRECT_RATES_INPUT_MODE:
        return QutritDissipationRates(
            input_mode=DIRECT_RATES_INPUT_MODE,
            gamma_10_down_per_us=getattr(
                environment,
                "gamma_10_down_per_us",
            ),
            gamma_01_up_per_us=getattr(
                environment,
                "gamma_01_up_per_us",
            ),
            gamma_21_down_per_us=getattr(
                environment,
                "gamma_21_down_per_us",
            ),
            gamma_12_up_per_us=getattr(
                environment,
                "gamma_12_up_per_us",
            ),
            gamma_phi_adjacent_per_us=getattr(
                environment,
                "gamma_phi_adjacent_per_us",
            ),
        )
    raise ValueError(
        "qutrit environment input_mode must be physical or direct_rates"
    )


def qutrit_collapse_operator_matrices(
    rates: QutritDissipationRates,
) -> tuple[Matrix, ...]:
    """Build transition-specific and number-noise collapse operators."""

    operators: list[Matrix] = []
    for rate, operator in (
        (rates.gamma_10_down_per_us, TRANSITION_10),
        (rates.gamma_01_up_per_us, TRANSITION_01),
        (rates.gamma_21_down_per_us, TRANSITION_21),
        (rates.gamma_12_up_per_us, TRANSITION_12),
    ):
        if rate > 0.0:
            operators.append(scale(math.sqrt(rate), operator))
    if rates.gamma_phi_adjacent_per_us > 0.0:
        operators.append(scale(
            math.sqrt(2.0 * rates.gamma_phi_adjacent_per_us),
            NUMBER_QUTRIT,
        ))
    return tuple(operators)


def prepared_qutrit_collapse_operators(
    rates: QutritDissipationRates,
) -> tuple[CachedCollapseOperator, ...]:
    """Return cached products for the qutrit Lindblad RHS."""

    return prepare_collapse_operators(
        qutrit_collapse_operator_matrices(rates)
    )


def evolve_open_qutrit_sequence(
    state: Matrix,
    envelope: PulseEnvelope,
    anharmonicity_rad_per_us: float,
    rates: QutritDissipationRates,
    total_simulation_time_us: float,
    max_step_us: float,
    *,
    phase_rad: float = 0.0,
    detuning_rad_per_us: float = 0.0,
    drag_beta_us: float = 0.0,
    pulse_checkpoint_times_us: Sequence[float] = (),
    idle_checkpoint_times_us: Sequence[float] = (),
    backend: ResolvedTimeDependentEvolutionBackend = "python",
) -> OpenQutritSequenceResult:
    """Run a dissipative qutrit pulse and optional free idle segment."""

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

    collapse_ops = prepared_qutrit_collapse_operators(rates)
    pulse_result = evolve_time_dependent_segment(
        state,
        QutritPulseHamiltonian(
            envelope=envelope,
            anharmonicity_rad_per_us=anharmonicity_rad_per_us,
            phase_rad=phase_rad,
            detuning_rad_per_us=detuning_rad_per_us,
            drag_beta_us=drag_beta_us,
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
        idle_hamiltonian = qutrit_rotating_frame_hamiltonian(
            detuning_rad_per_us,
            anharmonicity_rad_per_us,
            0.0,
            0.0,
        )
        idle_result = evolve_time_dependent_segment(
            pulse_result.state,
            ConstantHamiltonian(idle_hamiltonian),
            collapse_ops,
            duration_us=idle_duration,
            max_step_us=max_step_us,
            checkpoint_times_us=idle_checkpoint_times_us,
            backend=backend,
        )

    trajectory = _population_trajectory(
        pulse_result,
        idle_result,
        pulse_duration,
    )
    pulse_end_leakage = float(pulse_result.state[2][2].real)
    final_state = pulse_result.state if idle_result is None else idle_result.state
    final_leakage = float(final_state[2][2].real)
    recorded_leakage = [
        point.leakage_probability for point in trajectory
    ]
    recorded_leakage.extend((pulse_end_leakage, final_leakage))
    return OpenQutritSequenceResult(
        rates=rates,
        pulse_result=pulse_result,
        idle_result=idle_result,
        pulse_duration_us=pulse_duration,
        total_simulation_time_us=total_duration,
        trajectory=trajectory,
        leakage=QutritLeakageSummary(
            maximum_recorded_leakage_probability=max(recorded_leakage),
            leakage_at_pulse_end=pulse_end_leakage,
            leakage_at_final_time=final_leakage,
        ),
    )


def evolve_cptp_open_qutrit_sequence(
    state: Matrix,
    envelope: PulseEnvelope,
    anharmonicity_rad_per_us: float,
    rates: QutritDissipationRates,
    total_simulation_time_us: float,
    max_step_us: float,
    *,
    phase_rad: float = 0.0,
    detuning_rad_per_us: float = 0.0,
    drag_beta_us: float = 0.0,
    pulse_checkpoint_times_us: Sequence[float] = (),
    idle_checkpoint_times_us: Sequence[float] = (),
    backend: ResolvedTimeDependentEvolutionBackend = "python",
) -> tuple[
    OpenQutritSequenceResult,
    CPTPSegmentAudit,
    CPTPSegmentAudit | None,
]:
    """Run the qutrit sequence through audited exponential CPTP maps."""

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
    collapse_ops = prepared_qutrit_collapse_operators(rates)
    pulse_cptp = evolve_cptp_segment(
        state,
        QutritPulseHamiltonian(
            envelope=envelope,
            anharmonicity_rad_per_us=anharmonicity_rad_per_us,
            phase_rad=phase_rad,
            detuning_rad_per_us=detuning_rad_per_us,
            drag_beta_us=drag_beta_us,
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
            ConstantHamiltonian(qutrit_rotating_frame_hamiltonian(
                detuning_rad_per_us,
                anharmonicity_rad_per_us,
                0.0,
                0.0,
            )),
            collapse_ops,
            idle_duration,
            max_step_us,
            checkpoint_times_us=idle_checkpoint_times_us,
            backend=backend,
        )
    idle_result = None if idle_cptp is None else idle_cptp.evolution
    trajectory = _population_trajectory(
        pulse_cptp.evolution,
        idle_result,
        pulse_duration,
    )
    pulse_end_leakage = float(
        pulse_cptp.evolution.state[2][2].real
    )
    final_state = (
        pulse_cptp.evolution.state
        if idle_result is None
        else idle_result.state
    )
    final_leakage = float(final_state[2][2].real)
    recorded_leakage = [
        point.leakage_probability for point in trajectory
    ]
    recorded_leakage.extend((pulse_end_leakage, final_leakage))
    result = OpenQutritSequenceResult(
        rates=rates,
        pulse_result=pulse_cptp.evolution,
        idle_result=idle_result,
        pulse_duration_us=pulse_duration,
        total_simulation_time_us=total_duration,
        trajectory=trajectory,
        leakage=QutritLeakageSummary(
            maximum_recorded_leakage_probability=max(recorded_leakage),
            leakage_at_pulse_end=pulse_end_leakage,
            leakage_at_final_time=final_leakage,
        ),
    )
    return (
        result,
        pulse_cptp.audit,
        None if idle_cptp is None else idle_cptp.audit,
    )


def qutrit_gibbs_populations(
    temperature_mk: float,
    transition_01_frequency_ghz: float,
    transition_12_frequency_ghz: float,
) -> tuple[float, float, float]:
    """Return Gibbs populations for energies 0, hf01, h(f01 + f12)."""

    temperature = _nonnegative_finite(temperature_mk, "temperature_mk")
    frequency_01 = _positive_finite(
        transition_01_frequency_ghz,
        "transition_01_frequency_ghz",
    )
    frequency_12 = _positive_finite(
        transition_12_frequency_ghz,
        "transition_12_frequency_ghz",
    )
    if temperature == 0.0:
        return (1.0, 0.0, 0.0)

    beta = 1.0 / (BOLTZMANN_CONSTANT * temperature * 1e-3)
    energy_1 = PLANCK_CONSTANT * frequency_01 * 1e9
    energy_2 = energy_1 + PLANCK_CONSTANT * frequency_12 * 1e9
    weight_1 = math.exp(-beta * energy_1)
    weight_2 = math.exp(-beta * energy_2)
    partition = 1.0 + weight_1 + weight_2
    return (
        1.0 / partition,
        weight_1 / partition,
        weight_2 / partition,
    )


def _population_trajectory(
    pulse_result: TimeDependentEvolutionResult,
    idle_result: TimeDependentEvolutionResult | None,
    pulse_duration_us: float,
) -> tuple[QutritPopulationPoint, ...]:
    points = [
        qutrit_populations(
            checkpoint.cleaned_state,
            checkpoint.time_us,
            "pulse",
        )
        for checkpoint in pulse_result.checkpoints
    ]
    if idle_result is not None:
        for checkpoint in idle_result.checkpoints:
            if checkpoint.time_us == 0.0:
                continue
            points.append(qutrit_populations(
                checkpoint.cleaned_state,
                pulse_duration_us + checkpoint.time_us,
                "idle",
            ))
    return tuple(points)


def _positive_finite(value: float, field_name: str) -> float:
    converted = float(value)
    if not math.isfinite(converted) or converted <= 0.0:
        raise ValueError(f"{field_name} must be finite and greater than 0")
    return converted


def _nonnegative_finite(value: float, field_name: str) -> float:
    converted = float(value)
    if not math.isfinite(converted) or converted < 0.0:
        raise ValueError(
            f"{field_name} must be finite and non-negative"
        )
    return converted
