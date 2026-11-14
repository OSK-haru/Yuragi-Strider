"""Independent check of the thermal-photon dephasing rate (decision D90).

The production code adds Gamma_th = (kappa/2) Re[sqrt((1 + 2i chi/kappa)^2 + 8i chi n/kappa) - 1] to the
pure-dephasing rate. Here QuTiP solves the qubit together with its readout resonator,

    H = chi a^dag a sigma_z,   L = sqrt(kappa (n + 1)) a,  sqrt(kappa n) a^dag,

starting from |+> times a thermal resonator, and the long-time decay rate of the reduced qubit coherence
is fitted. The fit is independent of the formula: it only uses the master equation of the two modes.
"""

from __future__ import annotations

import math
import unittest

import numpy as np

from core.physical_environment import compute_thermal_occupation, thermal_photon_dephasing_rate

FOCK_DIMENSION = 14
MAX_RELATIVE_RATE_ERROR = 1e-4
# (name, temperature [mK], resonator frequency [GHz], kappa/2pi [MHz], chi/2pi [MHz])
CASES = (
    ("sample_resonator_120mK", 120.0, 7.0, 1.0, 0.5),
    ("sample_resonator_200mK", 200.0, 7.0, 1.0, 0.5),
    ("weak_dispersive_150mK", 150.0, 6.5, 2.0, 0.2),
    ("strong_dispersive_150mK", 150.0, 7.5, 0.5, 2.0),
)


def fitted_dephasing_rate(occupation: float, kappa_mhz: float, chi_mhz: float) -> dict[str, float]:
    """Fit the long-time decay rate (1/us) of the qubit coherence from the two-mode master equation."""

    import qutip as qt

    kappa = 2.0 * math.pi * kappa_mhz
    chi = 2.0 * math.pi * chi_mhz
    a = qt.tensor(qt.qeye(2), qt.destroy(FOCK_DIMENSION))
    sigma_z = qt.tensor(qt.sigmaz(), qt.qeye(FOCK_DIMENSION))
    hamiltonian = chi * a.dag() * a * sigma_z
    collapse = [math.sqrt(kappa * (occupation + 1.0)) * a, math.sqrt(kappa * occupation) * a.dag()]
    plus = (qt.basis(2, 0) + qt.basis(2, 1)).unit()
    initial = qt.tensor(plus * plus.dag(), qt.thermal_dm(FOCK_DIMENSION, occupation))
    times = np.linspace(0.0, 40.0 / kappa, 401)
    result = qt.mesolve(hamiltonian, initial, times, collapse, options={"atol": 1e-12, "rtol": 1e-10})
    coherence = np.array([abs(state.ptrace(0)[0, 1]) for state in result.states])
    window = times > 10.0 / kappa      # long-time regime, t >> 1/kappa
    slope, intercept = np.polyfit(times[window], np.log(coherence[window]), 1)
    residual = np.log(coherence[window]) - (slope * times[window] + intercept)
    return {"fitted_rate_per_us": float(-slope), "fit_max_log_residual": float(np.max(np.abs(residual))),
            "fit_start_us": float(times[window][0]), "fit_end_us": float(times[-1])}


def run_case(name: str, temperature_mk: float, frequency_ghz: float, kappa_mhz: float,
             chi_mhz: float) -> dict[str, object]:
    occupation = compute_thermal_occupation(temperature_mk, frequency_ghz)
    production = thermal_photon_dephasing_rate(occupation, kappa_mhz, chi_mhz)
    fit = fitted_dephasing_rate(occupation, kappa_mhz, chi_mhz)
    relative_error = abs(fit["fitted_rate_per_us"] - production) / production
    return {"name": name, "temperature_mk": temperature_mk, "readout_resonator_frequency_ghz": frequency_ghz,
            "readout_resonator_kappa_mhz": kappa_mhz, "readout_resonator_chi_mhz": chi_mhz,
            "n_th_resonator": occupation, "production_rate_per_us": production, **fit,
            "relative_error": relative_error, "pass": relative_error <= MAX_RELATIVE_RATE_ERROR}


class ThermalPhotonDephasingValidation(unittest.TestCase):
    def test_production_rate_matches_the_qubit_resonator_master_equation(self) -> None:
        for case in CASES:
            with self.subTest(case=case[0]):
                report = run_case(*case)
                self.assertGreater(report["n_th_resonator"], 1e-4)
                self.assertLessEqual(report["relative_error"], MAX_RELATIVE_RATE_ERROR, report)


if __name__ == "__main__":
    unittest.main()
