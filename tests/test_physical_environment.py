"""The environment inputs are measurable quantities (decisions D89, D90): T1 and Tphi at zero temperature,
temperature, qubit frequency and the readout resonator. These tests pin the rate formulas and the
rejection of the removed or replaced inputs."""

import math
import unittest

from api.main import SimulateRequest
from core.circuit_model import CircuitConfig
from core.io.config_io import config_from_dict, config_to_dict
from core.physical_environment import (
    UNIFIED_ENVIRONMENT_MODEL,
    compute_environment_rates,
    compute_environment_rates_per_qubit,
    compute_thermal_occupation,
    rates_from_times,
    thermal_photon_dephasing_rate,
)
from core.constants import BOLTZMANN_CONSTANT, PLANCK_CONSTANT
from core.results import EnvironmentConfig, SimulationConfig, SimulationResult
from core.simulator import run_simulation
from core.validation import validate_simulation_config

NO_RESONATOR_COUPLING = {"readout_resonator_chi_mhz": 0.0}
API_RESONATOR = {"readout_resonator_frequency_ghz": 7.0, "readout_resonator_kappa_mhz": 1.0,
                 "readout_resonator_chi_mhz": 0.5}


class RateFormulaTests(unittest.TestCase):
    def test_rates_follow_the_weak_coupling_formulas(self) -> None:
        rates = rates_from_times(40.0, 25.0, 80.0, 4.5, **NO_RESONATOR_COUPLING)
        n_th = 1.0 / math.expm1(PLANCK_CONSTANT * 4.5e9 / (BOLTZMANN_CONSTANT * 80e-3))
        self.assertAlmostEqual(rates.n_th, n_th, places=15)
        self.assertAlmostEqual(rates.gamma0_per_us, 1.0 / 40.0, places=15)
        self.assertAlmostEqual(rates.gamma_down_per_us, (n_th + 1.0) / 40.0, places=15)
        self.assertAlmostEqual(rates.gamma_up_per_us, n_th / 40.0, places=15)
        self.assertAlmostEqual(rates.gamma_phi_per_us, 1.0 / 25.0, places=15)
        # 1/T1 = gamma_down + gamma_up, 1/T2 = (gamma_down + gamma_up)/2 + gamma_phi
        self.assertAlmostEqual(1.0 / rates.t1_effective_us, (2.0 * n_th + 1.0) / 40.0, places=15)
        self.assertAlmostEqual(1.0 / rates.t2_effective_us, (2.0 * n_th + 1.0) / 80.0 + 1.0 / 25.0, places=15)

    def test_detailed_balance(self) -> None:
        rates = rates_from_times(50.0, 50.0, 120.0, 5.0)
        boltzmann = math.exp(-PLANCK_CONSTANT * 5e9 / (BOLTZMANN_CONSTANT * 120e-3))
        self.assertAlmostEqual(rates.gamma_up_per_us / rates.gamma_down_per_us, boltzmann, places=14)

    def test_zero_temperature_has_no_upward_rate_and_times_equal_the_inputs(self) -> None:
        rates = rates_from_times(70.0, 30.0, 0.0, 5.0)
        self.assertEqual(rates.n_th, 0.0)
        self.assertEqual(rates.gamma_up_per_us, 0.0)
        self.assertEqual(rates.gamma_phi_thermal_photon_per_us, 0.0)
        self.assertAlmostEqual(rates.t1_effective_us, 70.0, places=12)
        self.assertAlmostEqual(rates.tphi_effective_us, 30.0, places=12)

    def test_temperature_shortens_t1_and_tphi_but_not_the_zero_temperature_inputs(self) -> None:
        cold = rates_from_times(70.0, 30.0, 15.0, 5.0)
        hot = rates_from_times(70.0, 30.0, 300.0, 5.0)
        self.assertGreater(hot.n_th, cold.n_th)
        self.assertLess(hot.t1_effective_us, cold.t1_effective_us)
        self.assertLess(hot.tphi_effective_us, cold.tphi_effective_us)
        self.assertEqual(hot.t1_zero_temperature_us, cold.t1_zero_temperature_us)
        self.assertEqual(hot.tphi_zero_temperature_us, cold.tphi_zero_temperature_us)

    def test_infinite_times_mean_no_dissipation(self) -> None:
        rates = rates_from_times(math.inf, math.inf, 50.0, 5.0, **NO_RESONATOR_COUPLING)
        self.assertEqual((rates.gamma_down_per_us, rates.gamma_up_per_us, rates.gamma_phi_per_us), (0.0, 0.0, 0.0))

    def test_ideal_reference_has_zero_rates(self) -> None:
        rates = compute_environment_rates(EnvironmentConfig(ideal_reference=True, temperature_mk=200.0))
        self.assertTrue(rates.ideal_reference)
        self.assertEqual((rates.gamma_down_per_us, rates.gamma_up_per_us, rates.gamma_phi_per_us), (0.0, 0.0, 0.0))
        self.assertEqual(rates.gamma_phi_thermal_photon_per_us, 0.0)

    def test_thermal_occupation_edge_cases(self) -> None:
        self.assertEqual(compute_thermal_occupation(0.0, 5.0), 0.0)
        self.assertEqual(compute_thermal_occupation(1e-6, 5.0), 0.0)  # exponent beyond 700
        self.assertGreater(compute_thermal_occupation(200.0, 5.0), compute_thermal_occupation(15.0, 5.0))

    def test_per_qubit_times_override_the_shared_ones(self) -> None:
        environment = EnvironmentConfig(
            t1_zero_temperature_us=100.0, tphi_zero_temperature_us=100.0, temperature_mk=0.0,
            per_qubit=[{"t1_zero_temperature_us": 20.0}, {"tphi_zero_temperature_us": 5.0}])
        first, second = compute_environment_rates_per_qubit(environment, 2)
        self.assertAlmostEqual(first.t1_effective_us, 20.0)
        self.assertAlmostEqual(first.tphi_zero_temperature_us, 100.0)
        self.assertAlmostEqual(second.t1_effective_us, 100.0)
        self.assertAlmostEqual(second.tphi_zero_temperature_us, 5.0)


class ThermalPhotonDephasingTests(unittest.TestCase):
    """Gamma_th = (kappa/2) Re[sqrt((1 + 2i chi/kappa)^2 + 8i chi n/kappa) - 1] (Clerk and Utami 2007).
    The independent check against a qubit-plus-resonator master equation is in
    tests/test_validation_thermal_photon_dephasing.py."""

    def test_small_occupation_limit(self) -> None:
        # n << 1: Gamma = 4 chi^2 kappa n / (kappa^2 + 4 chi^2), with angular kappa and chi.
        for kappa_mhz, chi_mhz in ((1.0, 0.5), (2.0, 0.1), (0.5, 3.0)):
            kappa, chi, n = 2 * math.pi * kappa_mhz, 2 * math.pi * chi_mhz, 1e-6
            with self.subTest(kappa=kappa_mhz, chi=chi_mhz):
                self.assertAlmostEqual(
                    thermal_photon_dephasing_rate(n, kappa_mhz, chi_mhz) / (4 * chi**2 * kappa * n / (kappa**2 + 4 * chi**2)),
                    1.0, places=5)

    def test_no_photons_or_no_coupling_means_no_dephasing(self) -> None:
        self.assertEqual(thermal_photon_dephasing_rate(0.0, 1.0, 0.5), 0.0)
        self.assertEqual(thermal_photon_dephasing_rate(0.1, 1.0, 0.0), 0.0)

    def test_sign_of_chi_does_not_matter(self) -> None:
        self.assertEqual(thermal_photon_dephasing_rate(0.05, 1.0, 0.7), thermal_photon_dephasing_rate(0.05, 1.0, -0.7))

    def test_strong_dispersive_limit_is_kappa_times_occupation(self) -> None:
        # chi >> kappa: every photon that enters or leaves fully dephases the qubit, Gamma -> kappa n.
        n = 1e-3
        self.assertAlmostEqual(thermal_photon_dephasing_rate(n, 1.0, 1000.0) / (2 * math.pi * 1.0 * n), 1.0, places=3)

    def test_total_dephasing_adds_the_thermal_photon_term(self) -> None:
        rates = rates_from_times(70.0, 30.0, 60.0, 5.0, readout_resonator_frequency_ghz=6.5,
                                 readout_resonator_kappa_mhz=2.0, readout_resonator_chi_mhz=0.8)
        n_r = compute_thermal_occupation(60.0, 6.5)
        self.assertEqual(rates.n_th_resonator, n_r)
        self.assertGreater(n_r, 0.0)
        expected = thermal_photon_dephasing_rate(n_r, 2.0, 0.8)
        self.assertAlmostEqual(rates.gamma_phi_thermal_photon_per_us, expected, places=15)
        self.assertAlmostEqual(rates.gamma_phi_per_us, 1.0 / 30.0 + expected, places=15)
        self.assertAlmostEqual(rates.tphi_effective_us, 1.0 / (1.0 / 30.0 + expected), places=12)
        self.assertAlmostEqual(1.0 / rates.t2_effective_us,
                               0.5 / rates.t1_effective_us + 1.0 / 30.0 + expected, places=14)

    def test_negligible_at_the_default_temperature(self) -> None:
        rates = compute_environment_rates(EnvironmentConfig())
        self.assertLess(rates.gamma_phi_thermal_photon_per_us / rates.gamma_phi_per_us, 1e-6)

    def test_invalid_resonator_inputs_are_rejected(self) -> None:
        for kwargs in ({"readout_resonator_kappa_mhz": 0.0}, {"readout_resonator_frequency_ghz": -1.0},
                       {"readout_resonator_chi_mhz": -0.1}, {"readout_resonator_kappa_mhz": math.inf}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                rates_from_times(50.0, 50.0, 20.0, 5.0, **kwargs)
        config = SimulationConfig(environment=EnvironmentConfig(readout_resonator_kappa_mhz=0.0,
                                                                readout_resonator_chi_mhz=-1.0))
        codes = [issue.code for issue in validate_simulation_config(config)]
        self.assertEqual(codes.count("INVALID_READOUT_RESONATOR"), 2)


class RemovedInputTests(unittest.TestCase):
    def test_removed_fields_are_rejected_not_reinterpreted(self) -> None:
        for name in ("device_quality", "flux_noise_phi0", "noise_level", "input_mode", "t1_max_us"):
            with self.subTest(field=name), self.assertRaisesRegex(ValueError, "no longer exist"):
                EnvironmentConfig.from_dict({name: 0.5})
        with self.assertRaisesRegex(ValueError, "no longer exist"):
            EnvironmentConfig.from_dict({"per_qubit": [{"t1_max_us": 10.0}]})
        with self.assertRaises(TypeError):
            EnvironmentConfig(device_quality=0.5)

    def test_tphi_us_is_rejected_because_its_meaning_changed(self) -> None:
        with self.assertRaisesRegex(ValueError, "tphi_zero_temperature_us"):
            EnvironmentConfig.from_dict({"tphi_us": 30.0})
        with self.assertRaisesRegex(ValueError, "tphi_zero_temperature_us"):
            EnvironmentConfig.from_dict({"per_qubit": [{"tphi_us": 30.0}]})

    def test_old_config_schemas_are_rejected(self) -> None:
        for version, message in (("1.1", "removed"), ("2.0", "tphi_zero_temperature_us")):
            encoded = config_to_dict(SimulationConfig())
            encoded["schema_version"] = version
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, message):
                config_from_dict(encoded)

    def test_api_rejects_removed_parameters_and_requires_the_resonator(self) -> None:
        base = {"simulation_backend": "python_dense", "circuit_preset": "bell",
                "parameters": {"temperature_mk": 15.0, "qubit_frequency_ghz": 5.0,
                               "t1_zero_temperature_us": 50.0, "tphi_zero_temperature_us": 40.0,
                               **API_RESONATOR,
                               "duration_us": 1.0, "time_steps": 5, "fidelity_threshold": 0.9}}
        SimulateRequest.model_validate(base)
        for extra in ({"input_mode": "physical"},):
            with self.assertRaisesRegex(ValueError, "no longer exist"):
                SimulateRequest.model_validate({**base, **extra})
        with self.assertRaisesRegex(ValueError, "no longer exist"):
            SimulateRequest.model_validate({**base, "parameters": {**base["parameters"], "device_quality": 0.8}})
        with self.assertRaisesRegex(ValueError, "no longer exist"):
            SimulateRequest.model_validate({**base, "parameters": {**base["parameters"], "tphi_us": 40.0}})
        without_chi = {key: value for key, value in base["parameters"].items() if key != "readout_resonator_chi_mhz"}
        with self.assertRaisesRegex(ValueError, "readout_resonator_chi_mhz"):
            SimulateRequest.model_validate({**base, "parameters": without_chi})

    def test_non_positive_times_are_validation_issues(self) -> None:
        config = SimulationConfig(environment=EnvironmentConfig(t1_zero_temperature_us=0.0,
                                                                tphi_zero_temperature_us=-1.0))
        codes = [issue.code for issue in validate_simulation_config(config)]
        self.assertEqual(codes.count("INVALID_COHERENCE_TIME"), 2)


class SimulationTests(unittest.TestCase):
    def test_one_qubit_h_runs_and_reports_the_inputs(self) -> None:
        config = SimulationConfig(
            circuit=CircuitConfig.one_qubit_h(),
            environment=EnvironmentConfig(t1_zero_temperature_us=30.0, tphi_zero_temperature_us=20.0,
                                          temperature_mk=50.0),
            duration_us=2.0, time_steps=11, fidelity_threshold=0.9)
        result = run_simulation(config)
        self.assertIsInstance(result, SimulationResult)
        derived = result.derived_parameters
        self.assertEqual(derived["environment_model"], UNIFIED_ENVIRONMENT_MODEL)
        self.assertEqual(derived["t1_zero_temperature_us"], 30.0)
        self.assertEqual(derived["tphi_zero_temperature_us"], 20.0)
        self.assertGreater(derived["gamma_phi_thermal_photon_per_us"], 0.0)
        self.assertAlmostEqual(derived["gamma_phi_per_us"], 0.05 + derived["gamma_phi_thermal_photon_per_us"])

    def test_longer_times_reduce_decoherence(self) -> None:
        def purity(time_us):
            config = SimulationConfig(
                circuit=CircuitConfig.one_qubit_h(),
                environment=EnvironmentConfig(t1_zero_temperature_us=time_us, tphi_zero_temperature_us=time_us,
                                              temperature_mk=0.0),
                duration_us=5.0, time_steps=11)
            return run_simulation(config).purity[-1]
        self.assertGreater(purity(200.0), purity(5.0))

    def test_config_round_trips_through_qscope_dict(self) -> None:
        config = SimulationConfig(
            circuit=CircuitConfig.one_qubit_h(),
            environment=EnvironmentConfig(t1_zero_temperature_us=45.0, tphi_zero_temperature_us=math.inf,
                                          temperature_mk=25.0, qubit_frequency_ghz=4.8,
                                          readout_resonator_frequency_ghz=6.8, readout_resonator_kappa_mhz=1.5,
                                          readout_resonator_chi_mhz=0.3))
        encoded = config_to_dict(config)
        self.assertEqual(encoded["schema_version"], "3.0")
        self.assertIsNone(encoded["environment"]["tphi_zero_temperature_us"])          # JSON has no infinity
        decoded = config_from_dict(encoded)
        self.assertEqual(decoded.environment.t1_zero_temperature_us, 45.0)
        self.assertEqual(decoded.environment.tphi_zero_temperature_us, math.inf)
        self.assertEqual(decoded.environment.temperature_mk, 25.0)
        self.assertEqual(decoded.environment.qubit_frequency_ghz, 4.8)
        self.assertEqual(decoded.environment.readout_resonator_frequency_ghz, 6.8)
        self.assertEqual(decoded.environment.readout_resonator_kappa_mhz, 1.5)
        self.assertEqual(decoded.environment.readout_resonator_chi_mhz, 0.3)


if __name__ == "__main__":
    unittest.main()
