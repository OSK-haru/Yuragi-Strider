"""Strict request and response models for the experimental pulse API."""

from __future__ import annotations

import math
from typing import Annotated, ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from core.capabilities import (
    DRIVEN_COUPLED_TRANSMON_NETWORK_RWA_EXPERIMENTAL_MODEL,
    DRIVEN_TRANSMON_QUTRIT_RWA_EXPERIMENTAL_MODEL,
    DRIVEN_TWO_LEVEL_RWA_EXPERIMENTAL_MODEL,
)
from core.pulse_qutrit_contract import transition_12_frequency_ghz


class StrictPulseModel(BaseModel):
    """Reject unknown fields so inactive input modes cannot leak through."""

    model_config = ConfigDict(extra="forbid")


class PulsePhysicalEnvironmentRequest(StrictPulseModel):
    input_mode: Literal["physical"]
    device_quality: float = Field(ge=0.0, le=1.0)
    temperature_mk: float = Field(ge=0.0)
    flux_noise_phi0: float = Field(ge=0.0)
    qubit_frequency_ghz: float = Field(gt=0.0)
    t1_max_us: float = Field(gt=0.0)
    tphi_max_us: float = Field(gt=0.0)
    ideal_reference: bool = False

    @field_validator(
        "device_quality",
        "temperature_mk",
        "flux_noise_phi0",
        "qubit_frequency_ghz",
        "t1_max_us",
        "tphi_max_us",
    )
    @classmethod
    def validate_finite_environment_value(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("physical environment fields must be finite")
        return value


class PulseDirectRatesEnvironmentRequest(StrictPulseModel):
    input_mode: Literal["direct_rates"]
    gamma_down_per_us: float = Field(ge=0.0)
    gamma_up_per_us: float = Field(ge=0.0)
    gamma_phi_per_us: float = Field(ge=0.0)

    @field_validator(
        "gamma_down_per_us",
        "gamma_up_per_us",
        "gamma_phi_per_us",
    )
    @classmethod
    def validate_finite_rate(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("direct rates must be finite")
        return value


PulseEnvironmentRequest = Annotated[
    PulsePhysicalEnvironmentRequest | PulseDirectRatesEnvironmentRequest,
    Field(discriminator="input_mode"),
]


class PulseEnvelopeRequest(StrictPulseModel):
    shape: Literal["square", "gaussian"]
    amplitude_mode: Literal["target_rotation_angle", "peak_amplitude"]
    target_rotation_angle_rad: float | None = None
    peak_amplitude_rad_per_us: float | None = None
    pulse_duration_us: float | None = Field(default=None, gt=0.0)
    sigma_us: float | None = Field(default=None, gt=0.0)
    truncation_sigma: float | None = Field(default=None, gt=0.0)
    phase_rad: float = 0.0
    detuning_rad_per_us: float = 0.0
    drag_beta_us: float = 0.0

    @field_validator(
        "target_rotation_angle_rad",
        "peak_amplitude_rad_per_us",
        "phase_rad",
        "detuning_rad_per_us",
        "drag_beta_us",
    )
    @classmethod
    def validate_finite_value(
        cls,
        value: float | None,
    ) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("pulse numeric fields must be finite")
        return value

    @model_validator(mode="after")
    def validate_shape_and_amplitude(self) -> "PulseEnvelopeRequest":
        if self.amplitude_mode == "target_rotation_angle":
            if self.target_rotation_angle_rad is None:
                raise ValueError(
                    "target_rotation_angle_rad is required for "
                    "target_rotation_angle amplitude_mode"
                )
            if self.peak_amplitude_rad_per_us is not None:
                raise ValueError(
                    "peak_amplitude_rad_per_us is not allowed for "
                    "target_rotation_angle amplitude_mode"
                )
        else:
            if self.peak_amplitude_rad_per_us is None:
                raise ValueError(
                    "peak_amplitude_rad_per_us is required for "
                    "peak_amplitude amplitude_mode"
                )
            if self.target_rotation_angle_rad is not None:
                raise ValueError(
                    "target_rotation_angle_rad is not allowed for "
                    "peak_amplitude amplitude_mode"
                )

        if self.shape == "square":
            if self.pulse_duration_us is None:
                raise ValueError(
                    "pulse_duration_us is required for a square pulse"
                )
            if self.sigma_us is not None or self.truncation_sigma is not None:
                raise ValueError(
                    "sigma_us and truncation_sigma are not allowed for "
                    "a square pulse"
                )
        else:
            if self.sigma_us is None or self.truncation_sigma is None:
                raise ValueError(
                    "sigma_us and truncation_sigma are required for "
                    "a gaussian pulse"
                )
            if self.pulse_duration_us is not None:
                raise ValueError(
                    "pulse_duration_us is derived for a gaussian pulse"
                )

        if self.drag_beta_us != 0.0:
            raise ValueError(
                "drag_beta_us must be 0 for Pulse Baseline A"
            )
        return self

    @property
    def derived_pulse_duration_us(self) -> float:
        if self.shape == "square":
            assert self.pulse_duration_us is not None
            return self.pulse_duration_us
        assert self.sigma_us is not None
        assert self.truncation_sigma is not None
        return 2.0 * self.sigma_us * self.truncation_sigma


class PulseSnapshotOptionsRequest(StrictPulseModel):
    uniform_count: int = Field(default=101, ge=0, le=1001)
    custom_times_us: list[float] = Field(default_factory=list, max_length=100)

    @field_validator("uniform_count")
    @classmethod
    def validate_uniform_count(cls, value: int) -> int:
        if value == 1:
            raise ValueError(
                "uniform_count must be 0 or an integer from 2 to 1001"
            )
        return value

    @field_validator("custom_times_us")
    @classmethod
    def validate_custom_times(cls, values: list[float]) -> list[float]:
        if any(not math.isfinite(value) or value < 0.0 for value in values):
            raise ValueError(
                "custom_times_us must contain finite, non-negative times"
            )
        return values


class PulseReadoutErrorQubitRequest(StrictPulseModel):
    p10: float = Field(default=0.0, ge=0.0, le=1.0)
    p01: float = Field(default=0.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_assignment_span(self) -> "PulseReadoutErrorQubitRequest":
        if self.p10 + self.p01 >= 1.0:
            raise ValueError("readout assignment span must be positive")
        return self


class PulseReadoutErrorRequest(PulseReadoutErrorQubitRequest):
    """Independent affine readout errors applied only to observed probabilities."""

    per_qubit: list[PulseReadoutErrorQubitRequest] = Field(
        default_factory=list,
        max_length=4,
    )

    @property
    def is_enabled(self) -> bool:
        pairs = self.per_qubit or [self]
        return any(item.p10 > 0.0 or item.p01 > 0.0 for item in pairs)

    def assignment_errors(self, count: int) -> list[tuple[float, float]]:
        if not self.per_qubit:
            return [(self.p10, self.p01)] * count
        if len(self.per_qubit) != count:
            raise ValueError(
                "readout_error.per_qubit must match the transmon count "
                f"({count}), got {len(self.per_qubit)}"
            )
        return [(item.p10, item.p01) for item in self.per_qubit]


class PulseSimulateRequest(StrictPulseModel):
    model_id: Literal["driven_two_level_rwa_experimental_v1"] = (
        DRIVEN_TWO_LEVEL_RWA_EXPERIMENTAL_MODEL
    )
    initial_state: Literal["0", "1"] = "0"
    pulse: PulseEnvelopeRequest
    total_simulation_time_us: float = Field(gt=0.0)
    backend: Literal["python", "rust", "auto"] = "python"
    evolution_method: Literal["fixed_step_rk4", "explicit_cptp"] = (
        "fixed_step_rk4"
    )
    environment: PulseEnvironmentRequest
    readout_error: PulseReadoutErrorRequest | None = None
    snapshot_options: PulseSnapshotOptionsRequest = Field(
        default_factory=PulseSnapshotOptionsRequest
    )

    @model_validator(mode="after")
    def validate_timing(self) -> "PulseSimulateRequest":
        if not math.isfinite(self.total_simulation_time_us):
            raise ValueError("total_simulation_time_us must be finite")
        pulse_duration = self.pulse.derived_pulse_duration_us
        if pulse_duration > self.total_simulation_time_us:
            raise ValueError(
                "pulse duration must not exceed total_simulation_time_us"
            )
        if any(
            time_us > self.total_simulation_time_us
            for time_us in self.snapshot_options.custom_times_us
        ):
            raise ValueError(
                "snapshot_options.custom_times_us must not exceed "
                "total_simulation_time_us"
            )
        if self.readout_error is not None:
            self.readout_error.assignment_errors(1)
        return self


class PulseQubitCoherenceRequest(StrictPulseModel):
    t1_max_us: float = Field(gt=0.0)
    tphi_max_us: float = Field(gt=0.0)


class QutritPulsePhysicalEnvironmentRequest(
    PulsePhysicalEnvironmentRequest
):
    """Physical environment fields shared with the educational profile."""

    per_qubit: list[PulseQubitCoherenceRequest] = Field(
        default_factory=list,
        max_length=4,
    )


class QutritPulseDirectRatesEnvironmentRequest(StrictPulseModel):
    input_mode: Literal["direct_rates"]
    gamma_10_down_per_us: float = Field(ge=0.0)
    gamma_01_up_per_us: float = Field(ge=0.0)
    gamma_21_down_per_us: float = Field(ge=0.0)
    gamma_12_up_per_us: float = Field(ge=0.0)
    gamma_phi_adjacent_per_us: float = Field(
        ge=0.0,
        description=(
            "Single number-operator pure-dephasing rate. It gives the same "
            "pure-dephasing rate to rho_01 and rho_12, while rho_02 decays "
            "at four times this rate; the three rates are not independent."
        ),
    )

    @field_validator(
        "gamma_10_down_per_us",
        "gamma_01_up_per_us",
        "gamma_21_down_per_us",
        "gamma_12_up_per_us",
        "gamma_phi_adjacent_per_us",
    )
    @classmethod
    def validate_finite_qutrit_rate(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("qutrit direct rates must be finite")
        return value


QutritPulseEnvironmentRequest = Annotated[
    QutritPulsePhysicalEnvironmentRequest
    | QutritPulseDirectRatesEnvironmentRequest,
    Field(discriminator="input_mode"),
]


class QutritPulseEnvelopeRequest(StrictPulseModel):
    """Provisional qutrit envelope contract with Gaussian DRAG support."""

    shape: Literal["square", "gaussian"]
    amplitude_mode: Literal["target_rotation_angle", "peak_amplitude"]
    target_rotation_angle_rad: float | None = None
    peak_amplitude_rad_per_us: float | None = None
    pulse_duration_us: float | None = Field(default=None, gt=0.0)
    sigma_us: float | None = Field(default=None, gt=0.0)
    truncation_sigma: float | None = Field(default=None, gt=0.0)
    phase_rad: float = 0.0
    detuning_rad_per_us: float = 0.0
    drag_beta_us: float = 0.0

    @field_validator(
        "target_rotation_angle_rad",
        "peak_amplitude_rad_per_us",
        "phase_rad",
        "detuning_rad_per_us",
        "drag_beta_us",
    )
    @classmethod
    def validate_finite_qutrit_pulse_value(
        cls,
        value: float | None,
    ) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("qutrit pulse numeric fields must be finite")
        return value

    @model_validator(mode="after")
    def validate_qutrit_shape_and_amplitude(
        self,
    ) -> "QutritPulseEnvelopeRequest":
        if self.amplitude_mode == "target_rotation_angle":
            if self.target_rotation_angle_rad is None:
                raise ValueError(
                    "target_rotation_angle_rad is required for "
                    "target_rotation_angle amplitude_mode"
                )
            if self.peak_amplitude_rad_per_us is not None:
                raise ValueError(
                    "peak_amplitude_rad_per_us is not allowed for "
                    "target_rotation_angle amplitude_mode"
                )
        else:
            if self.peak_amplitude_rad_per_us is None:
                raise ValueError(
                    "peak_amplitude_rad_per_us is required for "
                    "peak_amplitude amplitude_mode"
                )
            if self.target_rotation_angle_rad is not None:
                raise ValueError(
                    "target_rotation_angle_rad is not allowed for "
                    "peak_amplitude amplitude_mode"
                )

        if self.shape == "square":
            if self.pulse_duration_us is None:
                raise ValueError(
                    "pulse_duration_us is required for a square pulse"
                )
            if self.sigma_us is not None or self.truncation_sigma is not None:
                raise ValueError(
                    "sigma_us and truncation_sigma are not allowed for "
                    "a square pulse"
                )
        else:
            if self.sigma_us is None or self.truncation_sigma is None:
                raise ValueError(
                    "sigma_us and truncation_sigma are required for "
                    "a gaussian pulse"
                )
            if self.pulse_duration_us is not None:
                raise ValueError(
                    "pulse_duration_us is derived for a gaussian pulse"
                )

        if self.shape != "gaussian" and self.drag_beta_us != 0.0:
            raise ValueError(
                "nonzero drag_beta_us requires a gaussian qutrit pulse"
            )
        return self

    @property
    def derived_pulse_duration_us(self) -> float:
        if self.shape == "square":
            assert self.pulse_duration_us is not None
            return self.pulse_duration_us
        assert self.sigma_us is not None
        assert self.truncation_sigma is not None
        return 2.0 * self.sigma_us * self.truncation_sigma


class PulseComplexInputValue(StrictPulseModel):
    real: float
    imag: float

    @field_validator("real", "imag")
    @classmethod
    def validate_finite_complex_part(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("density-matrix values must be finite")
        return value


class QuasiStaticDetuningNoiseRequest(StrictPulseModel):
    """Shot-to-shot Gaussian detuning that stays fixed within each shot."""

    enabled: bool = False
    sigma_detuning_rad_per_us: float = Field(default=0.0, ge=0.0)
    quadrature_order: Literal[3, 5, 7, 9] = 5

    @field_validator("sigma_detuning_rad_per_us")
    @classmethod
    def validate_finite_sigma(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("quasi-static detuning sigma must be finite")
        return value

    @model_validator(mode="after")
    def validate_enabled_sigma(self) -> "QuasiStaticDetuningNoiseRequest":
        if self.enabled and self.sigma_detuning_rad_per_us <= 0.0:
            raise ValueError(
                "enabled quasi-static noise requires a positive detuning sigma"
            )
        return self


class QutritPulseSimulateRequest(StrictPulseModel):
    """Validated request contract for the experimental qutrit path."""

    model_id: Literal["driven_transmon_qutrit_rwa_experimental_v1"] = (
        DRIVEN_TRANSMON_QUTRIT_RWA_EXPERIMENTAL_MODEL
    )
    initial_state: Literal["0", "1", "2"] = "0"
    initial_density_matrix: list[list[PulseComplexInputValue]] | None = None
    anharmonicity_mhz: float = Field(lt=0.0)
    pulse: QutritPulseEnvelopeRequest
    total_simulation_time_us: float = Field(gt=0.0)
    backend: Literal["python", "rust", "auto"] = "python"
    evolution_method: Literal["fixed_step_rk4", "explicit_cptp"] = (
        "fixed_step_rk4"
    )
    environment: QutritPulseEnvironmentRequest
    readout_error: PulseReadoutErrorRequest | None = None
    quasi_static_noise: QuasiStaticDetuningNoiseRequest = Field(
        default_factory=QuasiStaticDetuningNoiseRequest
    )
    snapshot_options: PulseSnapshotOptionsRequest = Field(
        default_factory=PulseSnapshotOptionsRequest
    )

    @field_validator("anharmonicity_mhz")
    @classmethod
    def validate_finite_anharmonicity(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("anharmonicity_mhz must be finite")
        return value

    @model_validator(mode="after")
    def validate_qutrit_contract(self) -> "QutritPulseSimulateRequest":
        if not math.isfinite(self.total_simulation_time_us):
            raise ValueError("total_simulation_time_us must be finite")
        pulse_duration = self.pulse.derived_pulse_duration_us
        if pulse_duration > self.total_simulation_time_us:
            raise ValueError(
                "pulse duration must not exceed total_simulation_time_us"
            )
        if any(
            time_us > self.total_simulation_time_us
            for time_us in self.snapshot_options.custom_times_us
        ):
            raise ValueError(
                "snapshot_options.custom_times_us must not exceed "
                "total_simulation_time_us"
            )
        if isinstance(
            self.environment,
            QutritPulsePhysicalEnvironmentRequest,
        ):
            if self.environment.per_qubit and len(self.environment.per_qubit) != 1:
                raise ValueError("environment.per_qubit must contain one qutrit entry")
            transition_12_frequency_ghz(
                self.environment.qubit_frequency_ghz,
                self.anharmonicity_mhz,
            )
        if self.initial_density_matrix is not None:
            matrix = self.initial_density_matrix
            if len(matrix) != 3 or any(len(row) != 3 for row in matrix):
                raise ValueError("initial_density_matrix must be a 3x3 matrix")
            trace_real = sum(matrix[index][index].real for index in range(3))
            trace_imag = sum(matrix[index][index].imag for index in range(3))
            if abs(trace_real - 1.0) > 1e-7 or abs(trace_imag) > 1e-7:
                raise ValueError("initial_density_matrix must have unit trace")
            for row in range(3):
                for column in range(3):
                    left = matrix[row][column]
                    right = matrix[column][row]
                    if (
                        abs(left.real - right.real) > 1e-7
                        or abs(left.imag + right.imag) > 1e-7
                    ):
                        raise ValueError("initial_density_matrix must be Hermitian")
        if self.readout_error is not None:
            self.readout_error.assignment_errors(1)
        return self


class TransmonNetworkCouplingRequest(StrictPulseModel):
    left: int = Field(ge=0, le=3)
    right: int = Field(ge=0, le=3)
    exchange_coupling_rad_per_us: float = Field(ge=0.0)

    @model_validator(mode="after")
    def validate_coupling(self) -> "TransmonNetworkCouplingRequest":
        if self.left == self.right:
            raise ValueError("network coupling endpoints must be different")
        if not math.isfinite(self.exchange_coupling_rad_per_us):
            raise ValueError("network coupling strength must be finite")
        return self


class ScheduledTransmonPulseRequest(StrictPulseModel):
    target: int = Field(ge=0, le=3)
    start_time_us: float = Field(ge=0.0)
    pulse: QutritPulseEnvelopeRequest

    @field_validator("start_time_us")
    @classmethod
    def validate_start_time(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("network drive start_time_us must be finite")
        return value

    @property
    def end_time_us(self) -> float:
        return self.start_time_us + self.pulse.derived_pulse_duration_us


class CoupledTransmonNetworkPulseSimulateRequest(StrictPulseModel):
    """Bounded 1-4 transmon request with scheduled local drives.

    A single transmon is the degenerate network with no exchange edges; it
    shares this contract so the pulse lab can offer one "transmon x count"
    model instead of separate single- and multi-transmon paths.
    """

    model_id: Literal[
        "driven_coupled_transmon_network_rwa_experimental_v1"
    ] = DRIVEN_COUPLED_TRANSMON_NETWORK_RWA_EXPERIMENTAL_MODEL
    transmon_count: int = Field(ge=1, le=4)
    local_levels: Literal[2, 3] = 3
    initial_state: str = Field(pattern=r"^[01]{1,4}$")
    frequencies_ghz: list[float] = Field(min_length=1, max_length=4)
    anharmonicities_mhz: list[float] = Field(min_length=1, max_length=4)
    detunings_rad_per_us: list[float] = Field(min_length=1, max_length=4)
    couplings: list[TransmonNetworkCouplingRequest] = Field(
        default_factory=list,
        max_length=6,
    )
    drives: list[ScheduledTransmonPulseRequest] = Field(
        min_length=1,
        max_length=32,
    )
    quasi_static_detuning_sigmas_rad_per_us: list[float] = Field(
        default_factory=list,
        max_length=4,
    )
    quasi_static_detuning_adjacent_correlation: float = Field(
        default=0.0,
        ge=-1.0,
        le=1.0,
    )
    quasi_static_quadrature_order: Literal[3, 5] = 3
    total_simulation_time_us: float = Field(gt=0.0)
    backend: Literal["python", "rust", "auto"] = "auto"
    evolution_method: Literal["fixed_step_rk4", "explicit_cptp"] = (
        "fixed_step_rk4"
    )
    environment: QutritPulseEnvironmentRequest
    readout_error: PulseReadoutErrorRequest | None = None
    snapshot_options: PulseSnapshotOptionsRequest = Field(
        default_factory=PulseSnapshotOptionsRequest
    )

    @field_validator(
        "frequencies_ghz",
        "anharmonicities_mhz",
        "detunings_rad_per_us",
    )
    @classmethod
    def validate_network_values(cls, values: list[float]) -> list[float]:
        if any(not math.isfinite(value) for value in values):
            raise ValueError("network transmon values must be finite")
        return values

    @field_validator("frequencies_ghz")
    @classmethod
    def validate_network_frequencies(cls, values: list[float]) -> list[float]:
        if any(value <= 0.0 for value in values):
            raise ValueError("network frequencies must be positive")
        return values

    @field_validator("anharmonicities_mhz")
    @classmethod
    def validate_network_anharmonicities(cls, values: list[float]) -> list[float]:
        # A transmon always has a negative anharmonicity; the step policy uses
        # it to bound the integration step even for a two-level network, where
        # it otherwise never enters the qubit-subspace dynamics.
        if any(value >= 0.0 for value in values):
            raise ValueError("network anharmonicities must be negative")
        return values

    @field_validator("quasi_static_detuning_sigmas_rad_per_us")
    @classmethod
    def validate_network_noise_sigmas(cls, values: list[float]) -> list[float]:
        if any(not math.isfinite(value) or value < 0.0 for value in values):
            raise ValueError(
                "network quasi-static sigmas must be finite and non-negative"
            )
        return values

    # Explicit CPTP builds a dim**2 x dim**2 Choi matrix per interval, so it is
    # only offered where that stays small; three levels caps at two transmons.
    _CPTP_MAX_HILBERT_DIMENSION: ClassVar[int] = 9

    @model_validator(mode="after")
    def validate_network(self) -> "CoupledTransmonNetworkPulseSimulateRequest":
        count = self.transmon_count
        for field_name in (
            "frequencies_ghz",
            "anharmonicities_mhz",
            "detunings_rad_per_us",
        ):
            if len(getattr(self, field_name)) != count:
                raise ValueError(f"{field_name} must match transmon_count")
        if len(self.initial_state) != count:
            raise ValueError("initial_state length must match transmon_count")
        if not math.isfinite(self.total_simulation_time_us):
            raise ValueError("total_simulation_time_us must be finite")

        if count == 1 and self.couplings:
            raise ValueError("a single-transmon network cannot have exchange couplings")
        coupling_edges: set[tuple[int, int]] = set()
        for coupling in self.couplings:
            if coupling.left >= count or coupling.right >= count:
                raise ValueError("network coupling endpoint is outside the register")
            edge = tuple(sorted((coupling.left, coupling.right)))
            if edge in coupling_edges:
                raise ValueError("network coupling edges must be unique")
            coupling_edges.add(edge)
        for drive in self.drives:
            if drive.target >= count:
                raise ValueError("network drive target is outside the register")
            if drive.end_time_us > self.total_simulation_time_us + 1e-14:
                raise ValueError("network drive must end within total simulation time")
        if any(
            time_us > self.total_simulation_time_us
            for time_us in self.snapshot_options.custom_times_us
        ):
            raise ValueError("snapshot times must not exceed total time")

        sigmas = self.quasi_static_detuning_sigmas_rad_per_us
        if sigmas and len(sigmas) != count:
            raise ValueError(
                "quasi_static_detuning_sigmas_rad_per_us must be empty or "
                "match transmon_count"
            )
        if self.readout_error is not None:
            self.readout_error.assignment_errors(count)
        if isinstance(self.environment, QutritPulsePhysicalEnvironmentRequest):
            if self.environment.per_qubit and len(self.environment.per_qubit) != count:
                raise ValueError(
                    "environment.per_qubit must match transmon_count "
                    f"({count})"
                )

        if self.evolution_method == "explicit_cptp":
            dimension = self.local_levels ** count
            if dimension > self._CPTP_MAX_HILBERT_DIMENSION:
                raise ValueError(
                    "explicit CPTP is limited to Hilbert dimension "
                    f"{self._CPTP_MAX_HILBERT_DIMENSION}; this register is "
                    f"{dimension}-dimensional. Use fixed_step_rk4."
                )

        if self.local_levels == 3:
            # The |1>-|2> transition only exists at three levels; validate its
            # frequency there.  Two-level networks still carry a negative
            # anharmonicity (checked above) but never populate |2>.
            for frequency, anharmonicity in zip(
                self.frequencies_ghz,
                self.anharmonicities_mhz,
                strict=True,
            ):
                transition_12_frequency_ghz(frequency, anharmonicity)
        return self


PulseApiRequest = Annotated[
    PulseSimulateRequest
    | QutritPulseSimulateRequest
    | CoupledTransmonNetworkPulseSimulateRequest,
    Field(discriminator="model_id"),
]


class PulseComplexValue(StrictPulseModel):
    real: float
    imag: float


class PulseReadoutAssignmentResponse(StrictPulseModel):
    p10: float
    p01: float


class PulseReadoutResponse(StrictPulseModel):
    enabled: bool
    model_id: Literal["independent_affine_binary_readout_v1"]
    assignment_errors: list[PulseReadoutAssignmentResponse]
    true_probabilities: dict[str, float]
    observed_probabilities: dict[str, float]
    preserves_state_metrics: Literal[True]
    noncomputational_level_policy: Literal["preserve"]


class PulsePhysicalityResponse(StrictPulseModel):
    trace_error: float
    hermiticity_error: float
    minimum_eigenvalue: float


class PulseUnitsResponse(StrictPulseModel):
    time: Literal["us"]
    angular_frequency: Literal["rad/us"]
    rate: Literal["1/us"]


class PulseModelIdentityResponse(StrictPulseModel):
    model_id: Literal["driven_two_level_rwa_experimental_v1"]
    description: str
    frame: Literal["rotating"]
    approximation: Literal["RWA"]
    logical_qubits: Literal[1]
    state_levels: Literal[2]
    experimental: Literal[True]
    hardware_calibrated: Literal[False]
    internal_units: PulseUnitsResponse


class PulseInputSummaryResponse(StrictPulseModel):
    initial_state: Literal["0", "1"]
    shape: Literal["square", "gaussian"]
    amplitude_mode: Literal["target_rotation_angle", "peak_amplitude"]
    target_rotation_angle_rad: float | None
    peak_amplitude_rad_per_us: float
    pulse_area_rad: float
    pulse_duration_us: float
    total_simulation_time_us: float
    idle_duration_us: float
    phase_rad: float
    detuning_rad_per_us: float
    sample_count: int


class PulseRatesResponse(StrictPulseModel):
    input_mode: Literal["physical", "direct_rates"]
    gamma_down_per_us: float
    gamma_up_per_us: float
    gamma_population_relaxation_per_us: float
    gamma_phi_per_us: float
    n_th: float | None
    t1_effective_us: float | None
    tphi_effective_us: float | None
    t2_effective_us: float | None


class PulseStepPolicyResponse(StrictPulseModel):
    policy_id: Literal["pulse_baseline_a_step_policy_v1"]
    epsilon_h: float
    epsilon_d: float
    samples_per_sigma: int
    max_internal_step_us: float
    hamiltonian_gap_rad_per_us: float
    dissipative_scale_per_us: float
    h_times_hamiltonian_gap: float
    h_times_dissipative_scale: float
    h_over_sigma: float | None
    estimated_internal_steps: int
    maximum_allowed_internal_steps: int


class PulseTrajectoryPointResponse(StrictPulseModel):
    time_us: float
    segment: Literal["pulse", "idle"]
    open_population_0: float
    open_population_1: float
    closed_population_0: float
    closed_population_1: float
    fidelity_to_closed: float
    purity: float
    raw_physicality: PulsePhysicalityResponse
    cleaned_physicality: PulsePhysicalityResponse
    cleanup_correction_norm: float


class PulseStateResponse(PulseTrajectoryPointResponse):
    open_density_matrix: list[list[PulseComplexValue]]
    closed_density_matrix: list[list[PulseComplexValue]]


class PulseEvolutionDiagnosticsResponse(StrictPulseModel):
    internal_step_count: int
    rhs_evaluation_count: int
    hamiltonian_evaluation_count: int
    minimum_internal_step_us: float
    maximum_internal_step_us: float
    raw_trace_error: float
    raw_hermiticity_error: float
    raw_minimum_eigenvalue: float
    cleanup_correction_norm: float
    actual_duration_us: float


class PulseBackendDiagnosticsResponse(StrictPulseModel):
    requested: Literal["python", "rust", "auto"]
    resolved: Literal["python", "rust"]
    fallback_used: bool


class PulseCPTPSegmentAuditResponse(StrictPulseModel):
    map_count: int
    interval_count: int
    minimum_choi_eigenvalue: float
    maximum_trace_preservation_frobenius_error: float
    maximum_trace_preservation_max_abs_error: float
    all_maps_cptp: bool
    cleanup_applied: Literal[False]
    sampling_id: Literal["midpoint_piecewise_constant_v1"]


class PulseEvolutionMethodDiagnosticsResponse(StrictPulseModel):
    requested: Literal["fixed_step_rk4", "explicit_cptp"]
    resolved: Literal["fixed_step_rk4", "explicit_cptp"]
    method_id: Literal[
        "fixed_step_rk4_v1",
        "explicit_cptp_midpoint_gksl_v1",
    ]
    cptp_guaranteed_by_construction: bool
    cleanup_applied: bool
    open_pulse_audit: PulseCPTPSegmentAuditResponse | None
    open_idle_audit: PulseCPTPSegmentAuditResponse | None
    closed_pulse_audit: PulseCPTPSegmentAuditResponse | None
    closed_idle_audit: PulseCPTPSegmentAuditResponse | None


class PulseDiagnosticsResponse(StrictPulseModel):
    api_runtime_ms: float
    backend: PulseBackendDiagnosticsResponse
    evolution: PulseEvolutionMethodDiagnosticsResponse
    open_pulse: PulseEvolutionDiagnosticsResponse
    open_idle: PulseEvolutionDiagnosticsResponse | None
    closed_pulse: PulseEvolutionDiagnosticsResponse
    closed_idle: PulseEvolutionDiagnosticsResponse | None
    maximum_cleaned_trace_error: float
    maximum_cleaned_hermiticity_error: float
    minimum_cleaned_eigenvalue: float


class PulseSimulateResponse(StrictPulseModel):
    contract_version: Literal["pulse-baseline-a-v1"]
    model: PulseModelIdentityResponse
    input: PulseInputSummaryResponse
    rates: PulseRatesResponse
    step_policy: PulseStepPolicyResponse
    sample_times_us: list[float]
    trajectory: list[PulseTrajectoryPointResponse]
    pulse_end: PulseStateResponse
    final: PulseStateResponse
    readout: PulseReadoutResponse
    diagnostics: PulseDiagnosticsResponse
    warnings: list[str]
    limitations: list[str]


class QutritPulseModelIdentityResponse(StrictPulseModel):
    model_id: Literal["driven_transmon_qutrit_rwa_experimental_v1"]
    description: str
    frame: Literal["rotating"]
    approximation: Literal["RWA"]
    logical_qubits: Literal[1]
    state_levels: Literal[3]
    basis_order: list[Literal["0", "1", "2"]]
    subsystem_dimensions: list[Literal[3]]
    experimental: Literal[True]
    hardware_calibrated: Literal[False]
    internal_units: PulseUnitsResponse


class QutritPulseInputSummaryResponse(StrictPulseModel):
    initial_state: Literal["0", "1", "2"]
    initial_state_source: Literal["basis_state", "density_matrix"]
    anharmonicity_mhz: float
    shape: Literal["square", "gaussian"]
    amplitude_mode: Literal["target_rotation_angle", "peak_amplitude"]
    target_rotation_angle_rad: float | None
    peak_amplitude_rad_per_us: float
    pulse_area_rad: float
    pulse_duration_us: float
    total_simulation_time_us: float
    idle_duration_us: float
    phase_rad: float
    detuning_rad_per_us: float
    drag_beta_us: float
    quasi_static_noise_enabled: bool
    quasi_static_detuning_sigma_rad_per_us: float
    quasi_static_quadrature_order: int
    sample_count: int


class QutritPulseRatesResponse(StrictPulseModel):
    input_mode: Literal["physical", "direct_rates"]
    gamma_10_down_per_us: float
    gamma_01_up_per_us: float
    gamma_21_down_per_us: float
    gamma_12_up_per_us: float
    gamma_phi_adjacent_per_us: float
    transition_01_frequency_ghz: float | None
    transition_12_frequency_ghz: float | None
    n_01: float | None
    n_12: float | None
    gamma_10_zero_temperature_per_us: float | None
    gamma_21_zero_temperature_per_us: float | None
    dephasing_model: Literal["number_operator_adjacent_rate_v1"]


class QutritPulseStepPolicyResponse(StrictPulseModel):
    policy_id: Literal["qutrit_fixed_rk4_v1"]
    hamiltonian_scale_max_rad_per_us: float
    dissipation_scale_per_us: float
    hamiltonian_step_limit_us: float | None
    dissipation_step_limit_us: float | None
    envelope_step_limit_us: float | None
    duration_step_limit_us: float
    selected_internal_step_cap_us: float
    step_limit_reason: str
    h_times_hamiltonian_scale: float
    h_times_dissipation_scale: float
    h_over_sigma: float | None
    drag_beta_us: float
    maximum_drive_magnitude_rad_per_us: float
    maximum_drag_derivative_rad_per_us2: float
    estimated_internal_step_count: int
    maximum_internal_step_count: int
    within_work_budget: bool


class QutritPulseTrajectoryPointResponse(StrictPulseModel):
    time_us: float
    segment: Literal["pulse", "idle"]
    population_0: float
    population_1: float
    population_2: float
    computational_population: float
    leakage_probability: float
    population_sum_error: float
    purity: float
    density_matrix: list[list[PulseComplexValue]]
    raw_physicality: PulsePhysicalityResponse
    cleaned_physicality: PulsePhysicalityResponse
    cleanup_correction_norm: float


class QutritLeakageResponse(StrictPulseModel):
    maximum_recorded_leakage_probability: float
    leakage_at_pulse_end: float
    leakage_at_final_time: float


class QutritPulseDiagnosticsResponse(StrictPulseModel):
    api_runtime_ms: float
    backend: PulseBackendDiagnosticsResponse
    evolution: PulseEvolutionMethodDiagnosticsResponse
    open_pulse: PulseEvolutionDiagnosticsResponse
    open_idle: PulseEvolutionDiagnosticsResponse | None
    maximum_cleaned_trace_error: float
    maximum_cleaned_hermiticity_error: float
    minimum_cleaned_eigenvalue: float
    quasi_static_noise: dict[str, object]


class QutritPulseSimulateResponse(StrictPulseModel):
    contract_version: Literal["pulse-extension-b-v1"]
    model: QutritPulseModelIdentityResponse
    input: QutritPulseInputSummaryResponse
    rates: QutritPulseRatesResponse
    step_policy: QutritPulseStepPolicyResponse
    sample_times_us: list[float]
    trajectory: list[QutritPulseTrajectoryPointResponse]
    leakage: QutritLeakageResponse
    pulse_end: QutritPulseTrajectoryPointResponse
    final: QutritPulseTrajectoryPointResponse
    readout: PulseReadoutResponse
    diagnostics: QutritPulseDiagnosticsResponse
    warnings: list[str]
    limitations: list[str]


class TransmonNetworkTrajectoryPointResponse(StrictPulseModel):
    time_us: float
    segment: Literal["pulse", "idle"]
    joint_populations: dict[str, float]
    computational_population: float
    leakage_probability: float
    population_sum_error: float
    purity: float
    density_matrix: list[list[PulseComplexValue]]
    raw_physicality: PulsePhysicalityResponse
    cleaned_physicality: PulsePhysicalityResponse
    cleanup_correction_norm: float


class CoupledTransmonNetworkPulseSimulateResponse(StrictPulseModel):
    contract_version: Literal["pulse-transmon-network-v1"]
    model: dict[str, object]
    input: dict[str, object]
    rates: list[QutritPulseRatesResponse]
    step_policy: dict[str, object]
    sample_times_us: list[float]
    trajectory: list[TransmonNetworkTrajectoryPointResponse]
    leakage: QutritLeakageResponse
    pulse_end: TransmonNetworkTrajectoryPointResponse
    final: TransmonNetworkTrajectoryPointResponse
    readout: PulseReadoutResponse
    diagnostics: dict[str, object]
    warnings: list[str]
    limitations: list[str]


PulseApiResponse = (
    PulseSimulateResponse
    | QutritPulseSimulateResponse
    | CoupledTransmonNetworkPulseSimulateResponse
)
