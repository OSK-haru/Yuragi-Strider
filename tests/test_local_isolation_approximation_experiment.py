"""Guard the pilot's controls and its deliberately failing approximations."""

import unittest

from scripts.experiment_local_isolation_approximation import (
    drive_case,
    readout_case,
    reset_case,
    topology_pilot,
    zz_case,
)


class LocalIsolationApproximationPilotTests(unittest.TestCase):
    def test_reset_retains_victim_dependence(self) -> None:
        result = reset_case()
        self.assertAlmostEqual(result["p_one_after_victim_zero"], 0.0)
        self.assertAlmostEqual(result["p_one_after_victim_one"], 0.1)

    def test_drive_only_is_separable(self) -> None:
        result = drive_case()
        self.assertGreater(result["p_target_one_exact"], 0.0)
        self.assertAlmostEqual(result["p_target_one_exact"], result["p_target_one_local"], places=12)
        self.assertLess(result["state_error_norm"], 1e-12)

    def test_zz_mean_field_fails_for_coherent_spectator(self) -> None:
        result = zz_case()
        controls = result["fixed_phase_controls"]
        self.assertLess(controls["spectator_zero"]["measurement_tvd"], 1e-12)
        self.assertLess(controls["spectator_one"]["measurement_tvd"], 1e-12)
        self.assertGreater(controls["spectator_plus"]["measurement_tvd"], 0.1)
        self.assertEqual(len(result["coherent_spectator_phase_sweep"]), 6)
        self.assertEqual([case["qubits"] for case in result["coherent_cluster_size_sweep"]], [2, 3, 4])

    def test_readout_marginals_do_not_capture_joint_correlation(self) -> None:
        result = readout_case()
        self.assertAlmostEqual(result["p_observed_11_joint"], 0.12)
        self.assertAlmostEqual(result["p_observed_11_independent"], 0.0144)
        self.assertGreater(result["joint_vs_independent_tvd"], 0.1)

    def test_topology_pilot_is_bounded_to_local_states(self) -> None:
        result = topology_pilot(side=20, events=2, job_qubits=8)
        self.assertEqual(result["topology_nodes"], 400)
        self.assertEqual(result["active_qubits"], 16)
        self.assertLess(result["one_hop_affected_qubits"], 400)
        self.assertEqual(result["local_statevector_bytes"], 2 * 256 * 16)
        self.assertEqual(result["hypothetical_joint_density_bytes"], 1 << 36)


if __name__ == "__main__":
    unittest.main()
