"""Acceptance and counterexamples for the exploratory P(1) reductions."""

import unittest

from scripts.explore_excitation_probability_approximations import (
    Case,
    prepare_case,
    solve_case,
)


class ExcitationProbabilityApproximationExperimentTests(unittest.TestCase):
    def test_no_zz_mean_field_matches_coupled_with_both_sites_driven(self) -> None:
        prepared = prepare_case(Case(0.0, 20.0, 8.0, "plus", 1.0, 0.12))
        exact = solve_case(prepared, "exact")
        local = solve_case(prepared, "mean_field")
        self.assertLess(max(abs(a - b) for a, b in zip(exact, local, strict=True)), 1e-8)

    def test_undriven_spectator_conditional_blocks_match_exact(self) -> None:
        for state in ("zero", "one", "plus", "mixed_30pct_one"):
            for noise_scale in (0.0, 1.0, 4.0):
                with self.subTest(state=state, noise_scale=noise_scale):
                    prepared = prepare_case(Case(8.0, 20.0, 0.0, state, noise_scale, 0.12))
                    exact = solve_case(prepared, "exact")
                    conditional = solve_case(prepared, "conditional_blocks")
                    self.assertLess(max(abs(a - b) for a, b in zip(exact, conditional, strict=True)), 1e-8)

    def test_conditional_blocks_reject_spectator_drive(self) -> None:
        prepared = prepare_case(Case(8.0, 20.0, 8.0, "plus", 1.0, 0.12))
        with self.assertRaisesRegex(ValueError, "undriven spectator"):
            solve_case(prepared, "conditional_blocks")

    def test_mean_field_is_not_universally_within_one_percentage_point(self) -> None:
        prepared = prepare_case(Case(8.0, 20.0, 0.0, "plus", 0.0, 0.12))
        exact = solve_case(prepared, "exact")
        mean_field = solve_case(prepared, "mean_field")
        self.assertGreater(max(abs(a - b) for a, b in zip(exact, mean_field, strict=True)), 0.01)

    def test_step_halving_is_smaller_than_one_percentage_point(self) -> None:
        prepared = prepare_case(Case(8.0, 20.0, 0.0, "plus", 1.0, 0.12))
        coarse = solve_case(prepared, "exact", 0.004)
        fine = solve_case(prepared, "exact", 0.002)
        self.assertLess(max(abs(a - b) for a, b in zip(coarse, fine, strict=True)), 1e-5)

    def test_phase_product_point_seven_is_not_a_safe_mean_field_rule(self) -> None:
        prepared = prepare_case(Case(8.0, 40.0, 8.0, "plus", 0.0, 0.0875))
        self.assertAlmostEqual(prepared.case.zz_rad_per_us * prepared.case.pulse_us, 0.7)
        for step in (0.004, 0.002):
            with self.subTest(step=step):
                exact = solve_case(prepared, "exact", step)
                mean_field = solve_case(prepared, "mean_field", step)
                self.assertGreater(max(abs(a - b) for a, b in zip(exact, mean_field, strict=True)), 0.01)


if __name__ == "__main__":
    unittest.main()
