"""Focused guards for internal shared-QPU physical primitives."""

import math
import unittest

import numpy as np

from core.capabilities import core_capabilities
from core.experimental_isolation_physics import (
    EXPERIMENTAL_ISOLATION_PHYSICS_VERSION,
    CorrelatedReadoutChannel,
    DriveCrosstalkTransfer,
    ExtendedTransmonNetworkHamiltonian,
    ImperfectResetSpec,
    PhenomenologicalResetModel,
    ResidualZZCoupling,
    ResetModelRequest,
    apply_correlated_readout,
    apply_local_imperfect_reset,
    experimental_isolation_physics_capabilities,
    imperfect_reset_channel,
)
from core.gates import apply_readout_error, density_from_ket
from core.pulse_envelopes import SquarePulseEnvelope
from core.pulse_transmon_network import (
    CoupledTransmonNetworkHamiltonian,
    ScheduledTransmonDrive,
)


class InternalCapabilityBoundaryTests(unittest.TestCase):
    def test_contract_is_versioned_but_not_publicly_selectable(self) -> None:
        internal = experimental_isolation_physics_capabilities()
        public = core_capabilities()

        self.assertEqual(internal["version"], EXPERIMENTAL_ISOLATION_PHYSICS_VERSION)
        self.assertEqual(internal["status"], "internal_only")
        self.assertFalse(internal["public_api_selectable"])
        self.assertNotIn(
            EXPERIMENTAL_ISOLATION_PHYSICS_VERSION,
            public["supported_simulation_models"],
        )
        self.assertNotIn(
            EXPERIMENTAL_ISOLATION_PHYSICS_VERSION,
            public["supported_pulse_models"],
        )


class ImperfectResetTests(unittest.TestCase):
    def test_binary_reset_profile_matches_independent_population_formula(self) -> None:
        success = 0.9
        thermal_one = 0.02
        alpha = 0.02
        beta = 0.04
        model = PhenomenologicalResetModel(
            ImperfectResetSpec(
                success_probability=success,
                target_populations=(1.0 - thermal_one, thermal_one),
            ),
            observed_one_given_zero=alpha,
            observed_zero_given_one=beta,
        )
        for victim_bit, expected in ((0, 0.03692), (1, 0.13092)):
            with self.subTest(victim_bit=victim_bit):
                state = density_from_ket(
                    (1.0 + 0.0j, 0.0j) if victim_bit == 0
                    else (0.0j, 1.0 + 0.0j)
                )
                result = model.run(ResetModelRequest(input_state=state))
                predicted = alpha + (1.0 - alpha - beta) * (
                    (1.0 - success) * victim_bit + success * thermal_one
                )
                self.assertAlmostEqual(predicted, expected, places=12)
                self.assertAlmostEqual(result.attacker_one_probability, predicted, places=12)

    def test_qutrit_reset_channel_is_cptp(self) -> None:
        channel = imperfect_reset_channel(ImperfectResetSpec(
            success_probability=0.83,
            target_populations=(0.96, 0.035, 0.005),
        ))

        self.assertTrue(channel.audit().is_cptp)

    def test_failed_branch_retains_prior_entanglement_information(self) -> None:
        inverse_sqrt_two = 1.0 / math.sqrt(2.0)
        bell = density_from_ket((
            inverse_sqrt_two + 0.0j,
            0.0j,
            0.0j,
            inverse_sqrt_two + 0.0j,
        ))

        reset = apply_local_imperfect_reset(
            bell,
            subsystem_count=2,
            target=0,
            spec=ImperfectResetSpec(success_probability=0.75),
        )

        self.assertAlmostEqual(reset[0][0].real, 0.5, places=12)
        self.assertAlmostEqual(reset[1][1].real, 0.375, places=12)
        self.assertAlmostEqual(reset[3][3].real, 0.125, places=12)
        self.assertAlmostEqual(reset[0][3].real, 0.125, places=12)
        self.assertAlmostEqual(sum(reset[index][index].real for index in range(4)), 1.0)

    def test_disabled_reset_is_exact_identity(self) -> None:
        state = density_from_ket((0.0j, 1.0 + 0.0j))

        reset = apply_local_imperfect_reset(
            state,
            subsystem_count=1,
            target=0,
            spec=ImperfectResetSpec(success_probability=0.0),
        )

        self.assertIs(reset, state)


class CorrelatedReadoutTests(unittest.TestCase):
    def test_cluster_channel_acts_on_requested_non_adjacent_qubits(self) -> None:
        # For true local 00, classify the pair as observed 11 with probability 0.2.
        matrix = (
            (0.8, 0.0, 0.0, 0.0),
            (0.0, 1.0, 0.0, 0.0),
            (0.0, 0.0, 1.0, 0.0),
            (0.2, 0.0, 0.0, 1.0),
        )
        probabilities = {
            format(index, "03b"): 1.0 if index == 0 else 0.0
            for index in range(8)
        }

        observed = apply_correlated_readout(
            probabilities,
            qubit_count=3,
            channels=(CorrelatedReadoutChannel((0, 2), matrix),),
        )

        self.assertAlmostEqual(observed["000"], 0.8)
        self.assertAlmostEqual(observed["101"], 0.2)
        self.assertAlmostEqual(sum(observed.values()), 1.0)

    def test_single_qubit_clusters_match_existing_independent_readout(self) -> None:
        probabilities = {"00": 0.31, "01": 0.19, "10": 0.23, "11": 0.27}
        errors = ((0.02, 0.07), (0.05, 0.01))
        channels = tuple(
            CorrelatedReadoutChannel(
                (qubit,),
                (
                    (1.0 - p10, p01),
                    (p10, 1.0 - p01),
                ),
            )
            for qubit, (p10, p01) in enumerate(errors)
        )

        expected = apply_readout_error(probabilities, 2, errors)
        actual = apply_correlated_readout(probabilities, 2, channels)

        for label in probabilities:
            self.assertAlmostEqual(actual[label], expected[label], places=15)

    def test_overlapping_clusters_are_rejected(self) -> None:
        probabilities = {format(index, "02b"): 0.25 for index in range(4)}
        with self.assertRaises(ValueError):
            apply_correlated_readout(
                probabilities,
                qubit_count=2,
                channels=(
                    CorrelatedReadoutChannel.identity((0,)),
                    CorrelatedReadoutChannel.identity((0, 1)),
                ),
            )


class CoherentExtensionTests(unittest.TestCase):
    def test_empty_extension_is_exactly_the_frozen_baseline(self) -> None:
        base = _two_qubit_network((
            ScheduledTransmonDrive(
                target=0,
                start_time_us=0.0,
                envelope=SquarePulseEnvelope(7.0, 0.02),
            ),
        ))
        extended = ExtendedTransmonNetworkHamiltonian(base)

        self.assertTrue(np.array_equal(
            extended.evaluate_array(0.01),
            base.evaluate_array(0.01),
        ))
        self.assertTrue(np.array_equal(
            extended.for_segment(0.0, 0.02).evaluate_array(0.01),
            base.for_segment(0.0, 0.02).evaluate_array(0.01),
        ))

    def test_residual_zz_adds_the_expected_conditional_shift(self) -> None:
        base = _two_qubit_network(())
        shift = -0.37
        extended = ExtendedTransmonNetworkHamiltonian(
            base,
            residual_zz=(ResidualZZCoupling(0, 1, shift),),
        )

        difference = extended.evaluate_array(0.0) - base.evaluate_array(0.0)

        self.assertTrue(np.allclose(difference, np.diag([0.0, 0.0, 0.0, shift])))

    def test_complex_drive_transfer_adds_quadrature_on_the_target(self) -> None:
        base = _two_qubit_network((
            ScheduledTransmonDrive(
                target=0,
                start_time_us=0.0,
                envelope=SquarePulseEnvelope(10.0, 0.02),
            ),
        ))
        extended = ExtendedTransmonNetworkHamiltonian(
            base,
            drive_crosstalk=(DriveCrosstalkTransfer(0, 1, 0.0, 0.2),),
        )

        difference = extended.evaluate_array(0.01) - base.evaluate_array(0.01)

        expected_local_y = np.asarray(((0.0, -1.0j), (1.0j, 0.0)))
        expected = np.kron(np.eye(2), expected_local_y)
        self.assertTrue(np.allclose(difference, expected))


def _two_qubit_network(
    drives: tuple[ScheduledTransmonDrive, ...],
) -> CoupledTransmonNetworkHamiltonian:
    return CoupledTransmonNetworkHamiltonian(
        anharmonicities_rad_per_us=(0.0, 0.0),
        detunings_rad_per_us=(0.0, 0.0),
        couplings=(),
        drives=drives,
        local_dimension=2,
    )


if __name__ == "__main__":
    unittest.main()
