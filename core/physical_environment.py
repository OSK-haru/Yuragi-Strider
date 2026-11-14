"""Environment inputs and the Lindblad rates they determine.

The inputs are physical quantities that can be measured on a device:

* ``t1_zero_temperature_us``: the energy-relaxation time the qubit would have at zero
  temperature, ``1/gamma_0``. It fixes the coupling to the bosonic bath.
* ``tphi_us``: the pure-dephasing time ``1/gamma_phi``.
* ``temperature_mk`` and ``qubit_frequency_ghz``: they fix the bath occupation ``n_th``.

The rates follow from the weak-coupling (Born-Markov) master equation:
``gamma_down = gamma_0 (n_th + 1)``, ``gamma_up = gamma_0 n_th`` and ``1/T1 = gamma_down + gamma_up``.
No other mapping is applied: earlier versions derived T1/Tphi from a "device quality" and added a
flux-noise term with a coefficient that had no derivation; both were removed (decision D89).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.constants import BOLTZMANN_CONSTANT, PLANCK_CONSTANT


UNIFIED_ENVIRONMENT_MODEL = "generic_superconducting_open_system_v2"
DEFAULT_TEMPERATURE_MK = 15.0
DEFAULT_QUBIT_FREQUENCY_GHZ = 5.0
DEFAULT_T1_ZERO_TEMPERATURE_US = 100.0
DEFAULT_TPHI_US = 100.0

# Inputs that existed before D89. A config that still names one is rejected with this message
# instead of being silently reinterpreted.
REMOVED_ENVIRONMENT_FIELDS = frozenset({
    "mode", "input_mode", "environment_model", "temperature", "temperature_kelvin",
    "magnetic_field", "magnetic_field_tesla", "noise_level", "device_quality",
    "flux_noise_phi0", "t1_max_us", "tphi_max_us", "normalized", "physical",
    "normalized_temperature", "normalized_magnetic_field",
})
REMOVED_ENVIRONMENT_MESSAGE = (
    "the device-quality, flux-noise and normalized (0-1) environment inputs were removed because they "
    "had no physical derivation; give t1_zero_temperature_us and tphi_us directly"
)


@dataclass(frozen=True)
class EnvironmentRates:
    """Rates consumed by the Lindblad solver, per microsecond."""

    model: str
    n_th: float
    gamma0_per_us: float
    gamma_down_per_us: float
    gamma_up_per_us: float
    gamma_population_relaxation_per_us: float
    gamma_phi_per_us: float
    t1_zero_temperature_us: float
    tphi_us: float
    t1_effective_us: float
    t2_effective_us: float
    temperature_mk: float
    qubit_frequency_ghz: float
    ideal_reference: bool = False


def reject_removed_fields(data: Mapping[str, Any], where: str) -> None:
    """Raise if ``data`` still names an input removed in D89."""

    removed = sorted(REMOVED_ENVIRONMENT_FIELDS.intersection(data))
    if removed:
        raise ValueError(f"{where}: {', '.join(removed)} no longer exist; {REMOVED_ENVIRONMENT_MESSAGE}")


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


def rates_from_times(
    t1_zero_temperature_us: float,
    tphi_us: float,
    temperature_mk: float,
    qubit_frequency_ghz: float,
    *,
    ideal_reference: bool = False,
) -> EnvironmentRates:
    """The Lindblad rates of one qubit."""

    temperature_mk = float(temperature_mk)
    qubit_frequency_ghz = float(qubit_frequency_ghz)
    if ideal_reference:
        return EnvironmentRates(
            model=UNIFIED_ENVIRONMENT_MODEL, n_th=0.0, gamma0_per_us=0.0, gamma_down_per_us=0.0,
            gamma_up_per_us=0.0, gamma_population_relaxation_per_us=0.0, gamma_phi_per_us=0.0,
            t1_zero_temperature_us=math.inf, tphi_us=math.inf, t1_effective_us=math.inf,
            t2_effective_us=math.inf, temperature_mk=temperature_mk,
            qubit_frequency_ghz=qubit_frequency_ghz, ideal_reference=True,
        )
    t1 = _positive_time(t1_zero_temperature_us, "t1_zero_temperature_us")
    tphi = _positive_time(tphi_us, "tphi_us")
    gamma0 = 1.0 / t1
    gamma_phi = 1.0 / tphi
    n_th = compute_thermal_occupation(temperature_mk, qubit_frequency_ghz)
    gamma_down = gamma0 * (1.0 + n_th)
    gamma_up = gamma0 * n_th
    population_relaxation = gamma_down + gamma_up
    return EnvironmentRates(
        model=UNIFIED_ENVIRONMENT_MODEL,
        n_th=n_th,
        gamma0_per_us=gamma0,
        gamma_down_per_us=gamma_down,
        gamma_up_per_us=gamma_up,
        gamma_population_relaxation_per_us=population_relaxation,
        gamma_phi_per_us=gamma_phi,
        t1_zero_temperature_us=t1,
        tphi_us=tphi,
        t1_effective_us=_inverse_or_inf(population_relaxation),
        t2_effective_us=_inverse_or_inf(0.5 * population_relaxation + gamma_phi),
        temperature_mk=temperature_mk,
        qubit_frequency_ghz=qubit_frequency_ghz,
        ideal_reference=False,
    )


def compute_environment_rates(environment: Any) -> EnvironmentRates:
    """The shared rates of an environment config."""

    return rates_from_times(
        getattr(environment, "t1_zero_temperature_us", DEFAULT_T1_ZERO_TEMPERATURE_US),
        getattr(environment, "tphi_us", DEFAULT_TPHI_US),
        getattr(environment, "temperature_mk", DEFAULT_TEMPERATURE_MK),
        getattr(environment, "qubit_frequency_ghz", DEFAULT_QUBIT_FREQUENCY_GHZ),
        ideal_reference=bool(getattr(environment, "ideal_reference", False)),
    )


def compute_environment_rates_per_qubit(
    environment: Any,
    n_qubits: int,
) -> tuple[EnvironmentRates, ...]:
    """Resolve one local rate set per qubit, using shared values as defaults."""

    if n_qubits < 1:
        raise ValueError("n_qubits must be positive")
    overrides = list(getattr(environment, "per_qubit", []) or [])
    if not overrides:
        shared = compute_environment_rates(environment)
        return (shared,) * n_qubits
    if len(overrides) != n_qubits:
        raise ValueError(
            "environment.per_qubit must provide one entry per qubit "
            f"({n_qubits}), got {len(overrides)}"
        )
    shared_t1 = getattr(environment, "t1_zero_temperature_us", DEFAULT_T1_ZERO_TEMPERATURE_US)
    shared_tphi = getattr(environment, "tphi_us", DEFAULT_TPHI_US)
    return tuple(
        rates_from_times(
            entry.get("t1_zero_temperature_us", shared_t1),
            entry.get("tphi_us", shared_tphi),
            getattr(environment, "temperature_mk", DEFAULT_TEMPERATURE_MK),
            getattr(environment, "qubit_frequency_ghz", DEFAULT_QUBIT_FREQUENCY_GHZ),
            ideal_reference=bool(getattr(environment, "ideal_reference", False)),
        )
        for entry in overrides
    )


def per_qubit_rates_to_derived_parameters(
    rates: tuple[EnvironmentRates, ...],
) -> list[dict[str, Any]]:
    """Serialize local rates."""

    return [
        {"qubit": index, **environment_rates_to_derived_parameters(local_rates)}
        for index, local_rates in enumerate(rates)
    ]


def environment_rates_to_derived_parameters(rates: EnvironmentRates) -> dict[str, Any]:
    """Return JSON-safe result metadata."""

    return {
        "environment_model": rates.model,
        "n_th": rates.n_th,
        "gamma0_per_us": rates.gamma0_per_us,
        "t1_zero_temperature_us": rates.t1_zero_temperature_us,
        "tphi_us": rates.tphi_us,
        "gamma_down_per_us": rates.gamma_down_per_us,
        "gamma_up_per_us": rates.gamma_up_per_us,
        "gamma_population_relaxation_per_us": rates.gamma_population_relaxation_per_us,
        "gamma_phi_per_us": rates.gamma_phi_per_us,
        "t1_effective_us": rates.t1_effective_us,
        "t2_effective_us": rates.t2_effective_us,
        "temperature_mk": rates.temperature_mk,
        "qubit_frequency_ghz": rates.qubit_frequency_ghz,
        "ideal_reference": rates.ideal_reference,
    }


def _positive_time(value: Any, name: str) -> float:
    number = float(value)
    if not (number > 0.0) or math.isnan(number):
        raise ValueError(f"{name} must be positive (use math.inf for no dissipation)")
    return number


def _inverse_or_inf(value: float) -> float:
    return math.inf if value <= 0.0 else 1.0 / value
