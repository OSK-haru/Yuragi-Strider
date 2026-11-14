"""Environment inputs and the Lindblad rates they determine.

The inputs are physical quantities that can be measured on a device:

* ``t1_zero_temperature_us``: the energy-relaxation time the qubit would have at zero
  temperature, ``1/gamma_0``. It fixes the coupling to the bosonic bath.
* ``tphi_zero_temperature_us``: the pure-dephasing time at zero temperature, ``1/gamma_phi0``.
  It holds every dephasing source other than thermal photons in the readout resonator; those
  sources are assumed not to depend on temperature (as ``gamma_0`` is).
* ``temperature_mk`` and ``qubit_frequency_ghz``: they fix the qubit bath occupation ``n_th``.
* ``readout_resonator_frequency_ghz``, ``readout_resonator_kappa_mhz`` (``kappa/2pi``) and
  ``readout_resonator_chi_mhz`` (``chi/2pi`` for the dispersive coupling ``H = chi a^dag a sigma_z``):
  with the temperature they fix the thermal-photon dephasing of the qubit.

The rates follow from the weak-coupling (Born-Markov) master equation:
``gamma_down = gamma_0 (n_th + 1)``, ``gamma_up = gamma_0 n_th`` and ``1/T1 = gamma_down + gamma_up``.
The pure-dephasing rate is ``gamma_phi = gamma_phi0 + Gamma_th``, where ``Gamma_th`` is the
dephasing by thermal photons in a resonator held at the same temperature
(Clerk and Utami, PRA 75, 042302 (2007); Rigetti et al., PRB 86, 100506(R) (2012)):

    Gamma_th = (kappa/2) Re[ sqrt((1 + 2i chi/kappa)^2 + 8i chi n_r/kappa) - 1 ],

with ``n_r`` the Bose occupation at the resonator frequency. It is the long-time (t >> 1/kappa)
decay rate of the qubit coherence, so using it as a constant Lindblad rate is the same Markov
approximation as for the other rates.

Earlier versions derived T1/Tphi from a "device quality" and added a flux-noise term with a
coefficient that had no derivation; both were removed (decision D89). The temperature
dependence of dephasing was added in decision D90.
"""

from __future__ import annotations

import cmath
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.constants import BOLTZMANN_CONSTANT, PLANCK_CONSTANT


UNIFIED_ENVIRONMENT_MODEL = "generic_superconducting_open_system_v3"
DEFAULT_TEMPERATURE_MK = 15.0
DEFAULT_QUBIT_FREQUENCY_GHZ = 5.0
DEFAULT_T1_ZERO_TEMPERATURE_US = 100.0
DEFAULT_TPHI_ZERO_TEMPERATURE_US = 100.0
# Sample values, not the calibration of a particular device.
DEFAULT_READOUT_RESONATOR_FREQUENCY_GHZ = 7.0
DEFAULT_READOUT_RESONATOR_KAPPA_MHZ = 1.0
DEFAULT_READOUT_RESONATOR_CHI_MHZ = 0.5

COHERENCE_TIME_FIELDS = ("t1_zero_temperature_us", "tphi_zero_temperature_us")
READOUT_RESONATOR_FIELDS = (
    "readout_resonator_frequency_ghz", "readout_resonator_kappa_mhz", "readout_resonator_chi_mhz",
)

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
    "had no physical derivation; give t1_zero_temperature_us and tphi_zero_temperature_us directly"
)
# Inputs whose meaning changed (D90): tphi_us was the total pure-dephasing time at the working
# temperature; tphi_zero_temperature_us excludes the thermal-photon part, which is now derived.
RENAMED_ENVIRONMENT_FIELDS = {"tphi_us": "tphi_zero_temperature_us"}
RENAMED_ENVIRONMENT_MESSAGE = (
    "tphi_us was replaced by tphi_zero_temperature_us, the pure-dephasing time without the "
    "thermal-photon dephasing of the readout resonator (which is now derived from the temperature)"
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
    gamma_phi_zero_temperature_per_us: float
    gamma_phi_thermal_photon_per_us: float
    n_th_resonator: float
    t1_zero_temperature_us: float
    tphi_zero_temperature_us: float
    t1_effective_us: float
    tphi_effective_us: float
    t2_effective_us: float
    temperature_mk: float
    qubit_frequency_ghz: float
    readout_resonator_frequency_ghz: float
    readout_resonator_kappa_mhz: float
    readout_resonator_chi_mhz: float
    ideal_reference: bool = False


def reject_removed_fields(data: Mapping[str, Any], where: str) -> None:
    """Raise if ``data`` still names an input removed in D89 or replaced in D90."""

    removed = sorted(REMOVED_ENVIRONMENT_FIELDS.intersection(data))
    if removed:
        raise ValueError(f"{where}: {', '.join(removed)} no longer exist; {REMOVED_ENVIRONMENT_MESSAGE}")
    renamed = sorted(set(RENAMED_ENVIRONMENT_FIELDS).intersection(data))
    if renamed:
        raise ValueError(f"{where}: {', '.join(renamed)} no longer exist; {RENAMED_ENVIRONMENT_MESSAGE}")


def compute_thermal_occupation(
    temperature_mk: float,
    qubit_frequency_ghz: float,
) -> float:
    """Return the Bose occupation of a mode at this frequency and temperature."""

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


def thermal_photon_dephasing_rate(
    resonator_occupation: float,
    kappa_mhz: float,
    chi_mhz: float,
) -> float:
    """Dephasing rate (1/us) from thermal photons in a dispersively coupled resonator.

    ``kappa_mhz`` is ``kappa/2pi`` and ``chi_mhz`` is ``chi/2pi`` for ``H = chi a^dag a sigma_z``
    (the two qubit states shift the resonator by ``2 chi``). The sign of chi does not matter.
    """

    occupation = float(resonator_occupation)
    kappa = 2.0 * math.pi * float(kappa_mhz)
    chi = 2.0 * math.pi * abs(float(chi_mhz))
    if occupation == 0.0 or chi == 0.0:
        return 0.0
    a = 1.0 + 2j * chi / kappa
    b = 8j * chi * occupation / kappa
    # sqrt(a^2 + b) - 1 = (a - 1) + b / (sqrt(a^2 + b) + a) and Re(a - 1) = 0. This form keeps the
    # small-occupation limit 4 chi^2 kappa n / (kappa^2 + 4 chi^2) free of cancellation.
    return 0.5 * kappa * (b / (cmath.sqrt(a * a + b) + a)).real


def rates_from_times(
    t1_zero_temperature_us: float,
    tphi_zero_temperature_us: float,
    temperature_mk: float,
    qubit_frequency_ghz: float,
    *,
    readout_resonator_frequency_ghz: float = DEFAULT_READOUT_RESONATOR_FREQUENCY_GHZ,
    readout_resonator_kappa_mhz: float = DEFAULT_READOUT_RESONATOR_KAPPA_MHZ,
    readout_resonator_chi_mhz: float = DEFAULT_READOUT_RESONATOR_CHI_MHZ,
    ideal_reference: bool = False,
) -> EnvironmentRates:
    """The Lindblad rates of one qubit."""

    temperature_mk = float(temperature_mk)
    qubit_frequency_ghz = float(qubit_frequency_ghz)
    resonator = {
        "readout_resonator_frequency_ghz": _positive_finite(
            readout_resonator_frequency_ghz, "readout_resonator_frequency_ghz"),
        "readout_resonator_kappa_mhz": _positive_finite(
            readout_resonator_kappa_mhz, "readout_resonator_kappa_mhz"),
        "readout_resonator_chi_mhz": _non_negative_finite(
            readout_resonator_chi_mhz, "readout_resonator_chi_mhz"),
    }
    if ideal_reference:
        return EnvironmentRates(
            model=UNIFIED_ENVIRONMENT_MODEL, n_th=0.0, gamma0_per_us=0.0, gamma_down_per_us=0.0,
            gamma_up_per_us=0.0, gamma_population_relaxation_per_us=0.0, gamma_phi_per_us=0.0,
            gamma_phi_zero_temperature_per_us=0.0, gamma_phi_thermal_photon_per_us=0.0,
            n_th_resonator=0.0, t1_zero_temperature_us=math.inf, tphi_zero_temperature_us=math.inf,
            t1_effective_us=math.inf, tphi_effective_us=math.inf, t2_effective_us=math.inf,
            temperature_mk=temperature_mk, qubit_frequency_ghz=qubit_frequency_ghz,
            ideal_reference=True, **resonator,
        )
    t1 = _positive_time(t1_zero_temperature_us, "t1_zero_temperature_us")
    tphi = _positive_time(tphi_zero_temperature_us, "tphi_zero_temperature_us")
    gamma0 = 1.0 / t1
    gamma_phi0 = 1.0 / tphi
    n_th = compute_thermal_occupation(temperature_mk, qubit_frequency_ghz)
    n_resonator = compute_thermal_occupation(temperature_mk, resonator["readout_resonator_frequency_ghz"])
    gamma_thermal_photon = thermal_photon_dephasing_rate(
        n_resonator, resonator["readout_resonator_kappa_mhz"], resonator["readout_resonator_chi_mhz"])
    gamma_phi = gamma_phi0 + gamma_thermal_photon
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
        gamma_phi_zero_temperature_per_us=gamma_phi0,
        gamma_phi_thermal_photon_per_us=gamma_thermal_photon,
        n_th_resonator=n_resonator,
        t1_zero_temperature_us=t1,
        tphi_zero_temperature_us=tphi,
        t1_effective_us=_inverse_or_inf(population_relaxation),
        tphi_effective_us=_inverse_or_inf(gamma_phi),
        t2_effective_us=_inverse_or_inf(0.5 * population_relaxation + gamma_phi),
        temperature_mk=temperature_mk,
        qubit_frequency_ghz=qubit_frequency_ghz,
        ideal_reference=False,
        **resonator,
    )


def _resonator_inputs(environment: Any) -> dict[str, Any]:
    return {
        "readout_resonator_frequency_ghz": getattr(
            environment, "readout_resonator_frequency_ghz", DEFAULT_READOUT_RESONATOR_FREQUENCY_GHZ),
        "readout_resonator_kappa_mhz": getattr(
            environment, "readout_resonator_kappa_mhz", DEFAULT_READOUT_RESONATOR_KAPPA_MHZ),
        "readout_resonator_chi_mhz": getattr(
            environment, "readout_resonator_chi_mhz", DEFAULT_READOUT_RESONATOR_CHI_MHZ),
    }


def compute_environment_rates(environment: Any) -> EnvironmentRates:
    """The shared rates of an environment config."""

    return rates_from_times(
        getattr(environment, "t1_zero_temperature_us", DEFAULT_T1_ZERO_TEMPERATURE_US),
        getattr(environment, "tphi_zero_temperature_us", DEFAULT_TPHI_ZERO_TEMPERATURE_US),
        getattr(environment, "temperature_mk", DEFAULT_TEMPERATURE_MK),
        getattr(environment, "qubit_frequency_ghz", DEFAULT_QUBIT_FREQUENCY_GHZ),
        ideal_reference=bool(getattr(environment, "ideal_reference", False)),
        **_resonator_inputs(environment),
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
    shared_tphi = getattr(environment, "tphi_zero_temperature_us", DEFAULT_TPHI_ZERO_TEMPERATURE_US)
    return tuple(
        rates_from_times(
            entry.get("t1_zero_temperature_us", shared_t1),
            entry.get("tphi_zero_temperature_us", shared_tphi),
            getattr(environment, "temperature_mk", DEFAULT_TEMPERATURE_MK),
            getattr(environment, "qubit_frequency_ghz", DEFAULT_QUBIT_FREQUENCY_GHZ),
            ideal_reference=bool(getattr(environment, "ideal_reference", False)),
            **_resonator_inputs(environment),
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
        "tphi_zero_temperature_us": rates.tphi_zero_temperature_us,
        "gamma_down_per_us": rates.gamma_down_per_us,
        "gamma_up_per_us": rates.gamma_up_per_us,
        "gamma_population_relaxation_per_us": rates.gamma_population_relaxation_per_us,
        "gamma_phi_per_us": rates.gamma_phi_per_us,
        "gamma_phi_zero_temperature_per_us": rates.gamma_phi_zero_temperature_per_us,
        "gamma_phi_thermal_photon_per_us": rates.gamma_phi_thermal_photon_per_us,
        "n_th_resonator": rates.n_th_resonator,
        "t1_effective_us": rates.t1_effective_us,
        "tphi_effective_us": rates.tphi_effective_us,
        "t2_effective_us": rates.t2_effective_us,
        "temperature_mk": rates.temperature_mk,
        "qubit_frequency_ghz": rates.qubit_frequency_ghz,
        "readout_resonator_frequency_ghz": rates.readout_resonator_frequency_ghz,
        "readout_resonator_kappa_mhz": rates.readout_resonator_kappa_mhz,
        "readout_resonator_chi_mhz": rates.readout_resonator_chi_mhz,
        "ideal_reference": rates.ideal_reference,
    }


def _positive_time(value: Any, name: str) -> float:
    number = float(value)
    if not (number > 0.0) or math.isnan(number):
        raise ValueError(f"{name} must be positive (use math.inf for no dissipation)")
    return number


def _positive_finite(value: Any, name: str) -> float:
    number = float(value)
    if not (number > 0.0 and math.isfinite(number)):
        raise ValueError(f"{name} must be positive and finite")
    return number


def _non_negative_finite(value: Any, name: str) -> float:
    number = float(value)
    if not (number >= 0.0 and math.isfinite(number)):
        raise ValueError(f"{name} must be non-negative and finite")
    return number


def _inverse_or_inf(value: float) -> float:
    return math.inf if value <= 0.0 else 1.0 / value
