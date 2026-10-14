"""Approved initial tuning values for the reset-leakage vertical slice."""

from __future__ import annotations

from dataclasses import dataclass
import math

from core.attack_simulation.reset_leakage_acceptance import (
    ResetLeakageAcceptancePolicy,
)
from core.attack_simulation.reset_leakage_grader import HiddenEvaluationPolicy
from core.attack_simulation.scenario_policy import ObservationBudget
from core.experimental_isolation_physics import (
    ImperfectResetSpec,
    PhenomenologicalResetModel,
)


@dataclass(frozen=True)
class ResetLeakageV0Settings:
    """Bundle approved defaults without merging their configuration owners.

    ``reset_success_probability`` belongs to QPU-service/model configuration.
    ``shots_per_case`` is resolved into the ScenarioPolicy observation budget.
    A preset may override the latter before compilation; the resolved manifest
    then records the effective value for replay.
    """

    profile_id: str = "reset_leakage_v0"
    profile_version: str = "1"
    scope: str = "reset_leakage_challenge_only"
    source: str = "product_policy"
    reset_success_probability: float = 0.90
    target_excited_population: float = 0.02
    observed_one_given_zero: float = 0.02
    observed_zero_given_one: float = 0.04
    reset_count: int = 1
    shots_per_case: int = 32
    minimum_calibrated_training_queries_per_label: int = 8
    training_queries_per_label: int = 10
    hidden_case_count: int = 20
    hidden_zero_case_count: int = 10
    hidden_required_correct: int = 16
    minimum_candidate_information_bits: float = 0.45
    minimum_candidate_advantage: float = 0.30
    maximum_control_information_bits: float = 1e-9
    maximum_control_advantage: float = 1e-9

    def __post_init__(self) -> None:
        for name in ("profile_id", "profile_version", "scope", "source"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must not be empty")
        if self.scope != "reset_leakage_challenge_only":
            raise ValueError("v0 settings are scoped only to the reset challenge")
        if self.source != "product_policy":
            raise ValueError("v0 settings must record product_policy provenance")
        for name in (
            "reset_success_probability",
            "target_excited_population",
            "observed_one_given_zero",
            "observed_zero_given_one",
        ):
            _probability(getattr(self, name), name)
        for name in (
            "minimum_candidate_information_bits",
            "minimum_candidate_advantage",
            "maximum_control_information_bits",
            "maximum_control_advantage",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and non-negative")
        if int(self.reset_count) < 0:
            raise ValueError("reset_count must be non-negative")
        if int(self.shots_per_case) < 1:
            raise ValueError("shots_per_case must be positive")
        if int(self.minimum_calibrated_training_queries_per_label) < 1:
            raise ValueError(
                "minimum_calibrated_training_queries_per_label must be positive"
            )
        if (
            int(self.training_queries_per_label)
            < int(self.minimum_calibrated_training_queries_per_label)
        ):
            raise ValueError(
                "training_queries_per_label must include the calibrated minimum"
            )
        if not 0 < int(self.hidden_zero_case_count) < int(self.hidden_case_count):
            raise ValueError("hidden cases must contain both victim bits")
        if not 1 <= int(self.hidden_required_correct) <= int(self.hidden_case_count):
            raise ValueError("hidden_required_correct is outside the case count")
        constant_best = max(
            int(self.hidden_zero_case_count),
            int(self.hidden_case_count) - int(self.hidden_zero_case_count),
        )
        if int(self.hidden_required_correct) <= constant_best:
            raise ValueError("a constant estimator must not pass")
        self.acceptance_policy()

    def build_reset_model(self) -> PhenomenologicalResetModel:
        """Build the reset-only behavioral model approved for this challenge."""

        excited = float(self.target_excited_population)
        return PhenomenologicalResetModel(
            spec=ImperfectResetSpec(
                success_probability=self.reset_success_probability,
                target_populations=(1.0 - excited, excited),
            ),
            observed_one_given_zero=self.observed_one_given_zero,
            observed_zero_given_one=self.observed_zero_given_one,
        )

    def observation_budget(
        self,
        *,
        training_query_limit: int | None = None,
        victim_observation_limit: int,
        retry_limit: int = 0,
        shots_per_case: int | None = None,
    ) -> ObservationBudget:
        """Build policy-owned limits with an optional per-preset shot value."""

        resolved_shots = (
            self.shots_per_case
            if shots_per_case is None
            else int(shots_per_case)
        )
        return ObservationBudget(
            shots_per_query=resolved_shots,
            training_query_limit=(
                self.training_query_limit
                if training_query_limit is None
                else training_query_limit
            ),
            victim_observation_limit=victim_observation_limit,
            retry_limit=retry_limit,
        )

    @property
    def training_query_limit(self) -> int:
        """Total labelled-training budget across the two binary labels."""

        return 2 * int(self.training_queries_per_label)

    def hidden_evaluation_policy(
        self,
        *,
        case_order_seed: int,
        measurement_seed: int,
        shots_per_case: int | None = None,
    ) -> HiddenEvaluationPolicy:
        """Build the approved grader policy with explicit replay seeds."""

        resolved_shots = (
            self.shots_per_case
            if shots_per_case is None
            else int(shots_per_case)
        )
        return HiddenEvaluationPolicy(
            policy_id="reset_leakage_v0_hidden",
            policy_version=self.profile_version,
            case_count=self.hidden_case_count,
            zero_case_count=self.hidden_zero_case_count,
            shots_per_case=resolved_shots,
            required_correct=self.hidden_required_correct,
            case_order_seed=case_order_seed,
            measurement_seed=measurement_seed,
        )

    def acceptance_policy(
        self,
        *,
        shots_per_case: int | None = None,
    ) -> ResetLeakageAcceptancePolicy:
        """Build the deterministic calibration acceptance policy."""

        resolved_shots = (
            self.shots_per_case
            if shots_per_case is None
            else int(shots_per_case)
        )
        return ResetLeakageAcceptancePolicy(
            policy_id="reset_leakage_v0_acceptance",
            policy_version=self.profile_version,
            shots_per_case=resolved_shots,
            minimum_candidate_information_bits=(
                self.minimum_candidate_information_bits
            ),
            minimum_candidate_advantage=self.minimum_candidate_advantage,
            maximum_control_information_bits=(
                self.maximum_control_information_bits
            ),
            maximum_control_advantage=self.maximum_control_advantage,
        )
def _probability(value: float, name: str) -> float:
    converted = float(value)
    if not math.isfinite(converted) or converted < 0.0 or converted > 1.0:
        raise ValueError(f"{name} must be a finite probability")
    return converted


RESET_LEAKAGE_V0_SETTINGS = ResetLeakageV0Settings()
