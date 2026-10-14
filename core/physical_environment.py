"""Unified environment helpers for open-system simulations."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

from core.constants import BOLTZMANN_CONSTANT, PLANCK_CONSTANT
from core.device_profiles import GenericTransmonProfile


UNIFIED_ENVIRONMENT_MODEL = "generic_superconducting_open_system_v1"
INPUT_MODE_NORMALIZED = "normalized"
INPUT_MODE_PHYSICAL = "physical"

NORMALIZED_ENVIRONMENT_MODEL = "normalized_phenomenological_v1"  # deprecated alias
PHYSICAL_ENVIRONMENT_MODEL = "superconducting_qubit_profile_v1"  # deprecated alias
LEGACY_ENVIRONMENT_MODELS = frozenset({
    NORMALIZED_ENVIRONMENT_MODEL,
    PHYSICAL_ENVIRONMENT_MODEL,
})
SUPPORTED_ENVIRONMENT_MODELS = frozenset({UNIFIED_ENVIRONMENT_MODEL})
SUPPORTED_INPUT_MODES = frozenset({
    INPUT_MODE_NORMALIZED,
    INPUT_MODE_PHYSICAL,
})
BASELINE_PROFILE_NOISE_INFO = (
    "Thermal excitation and flux-noise dephasing are zero, but baseline "
    "relaxation/dephasing remain finite due to the device profile."
)
DEVICE_QUALITY_MAPPING_INFO = (
    "Device Quality maps from the profile minimum to maximum coherence time: "
    "0.0 uses the minimum, 1.0 uses the maximum. Increasing T1/Tphi max does "
    "not create an ideal device when Device Quality is near 0."
)


@dataclass(frozen=True)
class PhysicalEnvironmentInputs:
    """Physical inputs after UI-level input mode normalization."""

    device_quality: float
    temperature_mk: float
    flux_noise_phi0: float
    qubit_frequency_ghz: float
    ideal_reference: bool = False


@dataclass(frozen=True)
class EnvironmentRates:
    """Unified rates consumed by the Lindblad solver."""

    model: str
    input_mode: str
    n_th: float
    gamma0_per_us: float
    gamma_down_per_us: float
    gamma_up_per_us: float
    gamma_population_relaxation_per_us: float
    gamma_phi_per_us: float
    gamma_phi_base_per_us: float
    gamma_phi_flux_per_us: float
    t1_zero_temperature_us: float
    t1_base_us: float
    tphi_zero_temperature_us: float
    tphi_base_us: float
    tphi_effective_us: float
    t1_effective_us: float
    t2_effective_us: float
    device_quality: float
    temperature_mk: float
    flux_noise_phi0: float
    qubit_frequency_ghz: float
    t1_max_us: float
    tphi_max_us: float
    ideal_reference: bool = False

    @property
    def gamma1_per_us(self) -> float:
        """Deprecated alias for gamma_down_per_us, not total 1/T1."""

        return self.gamma_down_per_us

    @property
    def gammaphi_per_us(self) -> float:
        """Deprecated spelling alias for gamma_phi_per_us."""

        return self.gamma_phi_per_us


def normalize_environment_model(model: str | None) -> str:
    """Return the canonical model id, accepting legacy ids for migration."""

    if model in LEGACY_ENVIRONMENT_MODELS:
        return UNIFIED_ENVIRONMENT_MODEL
    return str(model or UNIFIED_ENVIRONMENT_MODEL)


def input_mode_from_legacy_model(model: str | None) -> str:
    """Infer input mode from deprecated environment_model values."""

    if model == PHYSICAL_ENVIRONMENT_MODEL:
        return INPUT_MODE_PHYSICAL
    return INPUT_MODE_NORMALIZED


def compute_device_quality_times(
    device_quality: float,
    profile: GenericTransmonProfile | None = None,
) -> dict[str, float]:
    """Return base T1/Tphi values from a normalized device quality parameter."""

    profile = profile or GenericTransmonProfile()
    quality = _clamp(float(device_quality), 0.0, 1.0)
    t1_base = profile.t1_min_us * (
        profile.t1_max_us / profile.t1_min_us
    ) ** quality
    tphi_base = profile.tphi_min_us * (
        profile.tphi_max_us / profile.tphi_min_us
    ) ** quality
    return {
        "t1_zero_temperature_us": t1_base,
        "t1_base_us": t1_base,
        "tphi_zero_temperature_us": tphi_base,
        "tphi_base_us": tphi_base,
    }


def compute_thermal_occupation(
    temperature_mk: float,
    qubit_frequency_ghz: float,
) -> float:
    """Return Bose thermal occupation for a qubit frequency and temperature."""

    temperature_k = float(temperature_mk) * 1e-3
    frequency_hz = float(qubit_frequency_ghz) * 1e9
    if temperature_k <= 0.0 or frequency_hz <= 0.0:
        return 0.0

    exponent = PLANCK_CONSTANT * frequency_hz / (
        BOLTZMANN_CONSTANT * temperature_k
    )
    if exponent > 700.0:
        return 0.0
    denominator = math.expm1(exponent)
    if denominator <= 0.0:
        return math.inf
    return 1.0 / denominator


def map_normalized_to_physical(
    environment: Any,
    profile: GenericTransmonProfile | None = None,
) -> PhysicalEnvironmentInputs:
    """Map beginner normalized inputs into finite physical inputs."""

    profile = profile or GenericTransmonProfile()
    temperature = _clamp(float(getattr(environment, "temperature", 0.0)), 0.0, 1.0)
    magnetic_field = _clamp(float(getattr(environment, "magnetic_field", 0.0)), 0.0, 1.0)
    noise_level = _clamp(float(getattr(environment, "noise_level", 0.0)), 0.0, 1.0)
    flux_min = max(profile.flux_noise_min_phi0, 1e-30)
    flux_max = max(profile.flux_noise_max_phi0, flux_min)
    return PhysicalEnvironmentInputs(
        device_quality=1.0 - noise_level,
        temperature_mk=10.0 + 90.0 * temperature,
        flux_noise_phi0=flux_min * (flux_max / flux_min) ** magnetic_field,
        qubit_frequency_ghz=float(
            getattr(environment, "qubit_frequency_ghz", profile.qubit_frequency_ghz)
        ),
        ideal_reference=bool(getattr(environment, "ideal_reference", False)),
    )


def compute_environment_rates(
    environment: Any,
    profile: GenericTransmonProfile | None = None,
) -> EnvironmentRates:
    """Compute unified Lindblad rates for normalized or physical input modes."""

    profile = _profile_from_environment(environment, profile)
    return _compute_environment_rates_with_profile(environment, profile)


def compute_environment_rates_per_qubit(
    environment: Any,
    n_qubits: int,
    profile: GenericTransmonProfile | None = None,
) -> tuple[EnvironmentRates, ...]:
    """Resolve one local rate set per qubit, using shared values as defaults."""

    if n_qubits < 1:
        raise ValueError("n_qubits must be positive")
    overrides = list(getattr(environment, "per_qubit", []) or [])
    if not overrides:
        shared = compute_environment_rates(environment, profile)
        return (shared,) * n_qubits
    if len(overrides) != n_qubits:
        raise ValueError(
            "environment.per_qubit must provide one entry per qubit "
            f"({n_qubits}), got {len(overrides)}"
        )
    base_profile = _profile_from_environment(environment, profile)
    return tuple(
        _compute_environment_rates_with_profile(
            environment,
            replace(
                base_profile,
                t1_max_us=float(entry.get("t1_max_us", base_profile.t1_max_us)),
                tphi_max_us=float(entry.get("tphi_max_us", base_profile.tphi_max_us)),
            ),
        )
        for entry in overrides
    )


def _compute_environment_rates_with_profile(
    environment: Any,
    profile: GenericTransmonProfile,
) -> EnvironmentRates:
    input_mode = str(getattr(environment, "input_mode", INPUT_MODE_NORMALIZED))
    if input_mode == INPUT_MODE_NORMALIZED:
        inputs = map_normalized_to_physical(environment, profile)
    elif input_mode == INPUT_MODE_PHYSICAL:
        inputs = PhysicalEnvironmentInputs(
            device_quality=float(getattr(environment, "device_quality", 0.5)),
            temperature_mk=float(
                getattr(environment, "temperature_mk", profile.default_temperature_mk)
            ),
            flux_noise_phi0=float(
                getattr(environment, "flux_noise_phi0", profile.flux_noise_min_phi0)
            ),
            qubit_frequency_ghz=float(
                getattr(environment, "qubit_frequency_ghz", profile.qubit_frequency_ghz)
            ),
            ideal_reference=bool(getattr(environment, "ideal_reference", False)),
        )
    else:
        raise ValueError(f"unsupported environment input_mode: {input_mode}")

    rates = _compute_rates_from_physical_inputs(inputs, profile)
    return EnvironmentRates(
        model=UNIFIED_ENVIRONMENT_MODEL,
        input_mode=input_mode,
        **rates,
    )


def per_qubit_rates_to_derived_parameters(
    rates: tuple[EnvironmentRates, ...],
) -> list[dict[str, Any]]:
    """Serialize local rates without changing the legacy shared-rate fields."""

    return [
        {"qubit": index, **environment_rates_to_derived_parameters(local_rates)}
        for index, local_rates in enumerate(rates)
    ]


def compute_physical_rates(
    config: Any,
    profile: GenericTransmonProfile | None = None,
) -> dict[str, float]:
    """Compute physical-mode rates in per-microsecond units.

    Deprecated compatibility wrapper. New code should call
    compute_environment_rates() and environment_rates_to_derived_parameters().
    """

    profile = _profile_from_environment(config, profile)
    inputs = PhysicalEnvironmentInputs(
        device_quality=float(getattr(config, "device_quality", 0.5)),
        temperature_mk=float(
            getattr(config, "temperature_mk", profile.default_temperature_mk)
        ),
        flux_noise_phi0=float(
            getattr(config, "flux_noise_phi0", profile.flux_noise_min_phi0)
        ),
        qubit_frequency_ghz=float(
            getattr(config, "qubit_frequency_ghz", profile.qubit_frequency_ghz)
        ),
        ideal_reference=bool(getattr(config, "ideal_reference", False)),
    )
    rates = _compute_rates_from_physical_inputs(inputs, profile)
    return {
        **rates,
        "gamma_phi_total_per_us": rates["gamma_phi_per_us"],
    }


def environment_rates_to_derived_parameters(rates: EnvironmentRates) -> dict[str, Any]:
    """Return JSON-safe result metadata with legacy aliases."""

    return {
        "environment_model": rates.model,
        "input_mode": rates.input_mode,
        "n_th": rates.n_th,
        "gamma0_per_us": rates.gamma0_per_us,
        "t1_base_us": rates.t1_base_us,
        "t1_zero_temperature_us": rates.t1_zero_temperature_us,
        "tphi_base_us": rates.tphi_base_us,
        "gamma_down_per_us": rates.gamma_down_per_us,
        "gamma_up_per_us": rates.gamma_up_per_us,
        "gamma_population_relaxation_per_us": rates.gamma_population_relaxation_per_us,
        "gamma_phi_per_us": rates.gamma_phi_per_us,
        "gamma_phi_total_per_us": rates.gamma_phi_per_us,
        "gamma_phi_base_per_us": rates.gamma_phi_base_per_us,
        "gamma_phi_flux_per_us": rates.gamma_phi_flux_per_us,
        "tphi_effective_us": rates.tphi_effective_us,
        "t1_effective_us": rates.t1_effective_us,
        "t2_effective_us": rates.t2_effective_us,
        "device_quality": rates.device_quality,
        "temperature_mk": rates.temperature_mk,
        "flux_noise_phi0": rates.flux_noise_phi0,
        "qubit_frequency_ghz": rates.qubit_frequency_ghz,
        "t1_max_us": rates.t1_max_us,
        "tphi_max_us": rates.tphi_max_us,
        "ideal_reference": rates.ideal_reference,
        "t1_us": rates.t1_effective_us,
        "t2_us": rates.t2_effective_us,
        "gamma1_per_us": rates.gamma_down_per_us,
        "gammaphi_per_us": rates.gamma_phi_per_us,
    }


def baseline_profile_noise_info(rates: EnvironmentRates) -> str | None:
    """Return explanatory info for zero thermal/flux inputs with finite baseline rates."""

    if rates.ideal_reference:
        return None
    if (
        rates.temperature_mk == 0.0
        and rates.flux_noise_phi0 == 0.0
        and (rates.gamma_down_per_us > 0.0 or rates.gamma_phi_per_us > 0.0)
    ):
        return BASELINE_PROFILE_NOISE_INFO
    return None


def device_quality_mapping_info(rates: EnvironmentRates) -> str | None:
    """Return explanatory info for the device-quality interpolation."""

    if rates.ideal_reference:
        return None
    if rates.device_quality <= 0.05:
        return DEVICE_QUALITY_MAPPING_INFO
    return None


def _compute_rates_from_physical_inputs(
    inputs: PhysicalEnvironmentInputs,
    profile: GenericTransmonProfile,
) -> dict[str, float]:
    device_quality = float(inputs.device_quality)
    temperature_mk = float(inputs.temperature_mk)
    flux_noise_phi0 = float(inputs.flux_noise_phi0)
    qubit_frequency_ghz = float(inputs.qubit_frequency_ghz)
    if inputs.ideal_reference:
        return {
            "device_quality": device_quality,
            "temperature_mk": temperature_mk,
            "flux_noise_phi0": flux_noise_phi0,
            "qubit_frequency_ghz": qubit_frequency_ghz,
            "t1_max_us": profile.t1_max_us,
            "tphi_max_us": profile.tphi_max_us,
            "ideal_reference": True,
            "n_th": 0.0,
            "gamma0_per_us": 0.0,
            "t1_base_us": math.inf,
            "t1_zero_temperature_us": math.inf,
            "tphi_base_us": math.inf,
            "tphi_zero_temperature_us": math.inf,
            "gamma_down_per_us": 0.0,
            "gamma_up_per_us": 0.0,
            "gamma_population_relaxation_per_us": 0.0,
            "gamma_phi_base_per_us": 0.0,
            "gamma_phi_flux_per_us": 0.0,
            "gamma_phi_per_us": 0.0,
            "tphi_effective_us": math.inf,
            "t1_effective_us": math.inf,
            "t2_effective_us": math.inf,
        }

    quality_times = compute_device_quality_times(device_quality, profile)
    t1_zero_temperature_us = quality_times["t1_zero_temperature_us"]
    tphi_zero_temperature_us = quality_times["tphi_zero_temperature_us"]
    gamma0 = 1.0 / t1_zero_temperature_us
    gamma_phi_base = 1.0 / tphi_zero_temperature_us
    n_th = compute_thermal_occupation(temperature_mk, qubit_frequency_ghz)
    gamma_down = gamma0 * (1.0 + n_th)
    gamma_up = gamma0 * n_th

    flux_scale = 0.0
    if profile.flux_noise_max_phi0 > 0.0:
        flux_scale = max(0.0, flux_noise_phi0) / profile.flux_noise_max_phi0
    gamma_phi_flux = profile.flux_noise_gamma_phi_per_us_at_max * flux_scale
    gamma_phi_total = gamma_phi_base + gamma_phi_flux
    population_relaxation = gamma_down + gamma_up

    return {
        "device_quality": device_quality,
        "temperature_mk": temperature_mk,
        "flux_noise_phi0": flux_noise_phi0,
        "qubit_frequency_ghz": qubit_frequency_ghz,
        "t1_max_us": profile.t1_max_us,
        "tphi_max_us": profile.tphi_max_us,
        "ideal_reference": False,
        "n_th": n_th,
        "gamma0_per_us": gamma0,
        "t1_zero_temperature_us": t1_zero_temperature_us,
        "t1_base_us": t1_zero_temperature_us,
        "tphi_zero_temperature_us": tphi_zero_temperature_us,
        "tphi_base_us": tphi_zero_temperature_us,
        "gamma_down_per_us": gamma_down,
        "gamma_up_per_us": gamma_up,
        "gamma_population_relaxation_per_us": population_relaxation,
        "gamma_phi_base_per_us": gamma_phi_base,
        "gamma_phi_flux_per_us": gamma_phi_flux,
        "gamma_phi_per_us": gamma_phi_total,
        "tphi_effective_us": _inverse_or_inf(gamma_phi_total),
        "t1_effective_us": _inverse_or_inf(population_relaxation),
        "t2_effective_us": _inverse_or_inf(0.5 * population_relaxation + gamma_phi_total),
    }


def _inverse_or_inf(value: float) -> float:
    return math.inf if value <= 0.0 else 1.0 / value


def _profile_from_environment(
    environment: Any,
    profile: GenericTransmonProfile | None,
) -> GenericTransmonProfile:
    profile = profile or GenericTransmonProfile()
    return replace(
        profile,
        t1_max_us=float(getattr(environment, "t1_max_us", profile.t1_max_us)),
        tphi_max_us=float(getattr(environment, "tphi_max_us", profile.tphi_max_us)),
    )


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
