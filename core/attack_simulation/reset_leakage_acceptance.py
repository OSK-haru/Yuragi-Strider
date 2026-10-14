"""Acceptance audit for the first reset-leakage vertical slice."""

from __future__ import annotations

from dataclasses import dataclass
import math

from core.attack_simulation.reset_leakage_grader import HiddenEvaluationPolicy
from core.experimental_isolation_physics.reset_calibration import (
    LeakageCalibrationPoint,
    ResetCalibrationInputMode,
    ResetCalibrationResult,
)


@dataclass(frozen=True)
class ResetLeakageAcceptancePolicy:
    """Explicit thresholds; this module deliberately supplies no defaults."""

    policy_id: str
    policy_version: str
    shots_per_case: int
    minimum_candidate_information_bits: float
    minimum_candidate_advantage: float
    maximum_control_information_bits: float
    maximum_control_advantage: float

    def __post_init__(self) -> None:
        _identifier(self.policy_id, "policy_id")
        _identifier(self.policy_version, "policy_version")
        shots = int(self.shots_per_case)
        if shots < 1:
            raise ValueError("shots_per_case must be positive")
        object.__setattr__(self, "shots_per_case", shots)
        for name in (
            "minimum_candidate_information_bits",
            "minimum_candidate_advantage",
            "maximum_control_information_bits",
            "maximum_control_advantage",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and non-negative")
            object.__setattr__(self, name, value)
        if self.minimum_candidate_information_bits > 1.0:
            raise ValueError("minimum candidate information cannot exceed one bit")
        if self.maximum_control_information_bits > 1.0:
            raise ValueError("maximum control information cannot exceed one bit")
        if self.minimum_candidate_advantage > 0.5:
            raise ValueError("minimum candidate advantage cannot exceed 0.5")
        if self.maximum_control_advantage > 0.5:
            raise ValueError("maximum control advantage cannot exceed 0.5")


@dataclass(frozen=True)
class ResetLeakageAcceptanceCheck:
    check_id: str
    passed: bool
    observed: float | bool
    requirement: str

    def to_dict(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "passed": self.passed,
            "observed": self.observed,
            "requirement": self.requirement,
        }


@dataclass(frozen=True)
class ResetLeakageAcceptanceReport:
    policy_id: str
    policy_version: str
    passed: bool
    checks: tuple[ResetLeakageAcceptanceCheck, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "passed": self.passed,
            "checks": [check.to_dict() for check in self.checks],
        }


def audit_reset_leakage_acceptance(
    *,
    candidate: ResetCalibrationResult,
    perfect_reset_control: ResetCalibrationResult,
    isolated_resource_control: ResetCalibrationResult,
    hidden_policy: HiddenEvaluationPolicy,
    observation_disabled_verified: bool,
    policy: ResetLeakageAcceptancePolicy,
) -> ResetLeakageAcceptanceReport:
    """Evaluate attack signal, negative controls, and grading invariants."""

    _require_comparable_profiles(
        candidate,
        perfect_reset_control,
        isolated_resource_control,
    )
    if (
        candidate.profile.input_mode
        is not ResetCalibrationInputMode.VICTIM_DEPENDENT
    ):
        raise ValueError("candidate must use victim-dependent input")
    if (
        perfect_reset_control.profile.input_mode
        is not ResetCalibrationInputMode.VICTIM_DEPENDENT
    ):
        raise ValueError("perfect-reset control must use victim-dependent input")
    if (
        isolated_resource_control.profile.input_mode
        is not ResetCalibrationInputMode.FIXED_ZERO_CONTROL
    ):
        raise ValueError("isolated-resource control must use fixed-zero input")

    candidate_point = _point(candidate, policy.shots_per_case)
    perfect_point = _point(perfect_reset_control, policy.shots_per_case)
    isolated_point = _point(isolated_resource_control, policy.shots_per_case)
    one_case_count = hidden_policy.case_count - hidden_policy.zero_case_count
    constant_best = max(hidden_policy.zero_case_count, one_case_count)

    checks = (
        _minimum_check(
            "candidate_information",
            candidate_point.per_case_mutual_information_bits,
            policy.minimum_candidate_information_bits,
        ),
        _minimum_check(
            "candidate_advantage",
            candidate_point.attacker_advantage,
            policy.minimum_candidate_advantage,
        ),
        _maximum_check(
            "perfect_reset_information",
            perfect_point.per_case_mutual_information_bits,
            policy.maximum_control_information_bits,
        ),
        _maximum_check(
            "perfect_reset_advantage",
            perfect_point.attacker_advantage,
            policy.maximum_control_advantage,
        ),
        _maximum_check(
            "isolated_resource_information",
            isolated_point.per_case_mutual_information_bits,
            policy.maximum_control_information_bits,
        ),
        _maximum_check(
            "isolated_resource_advantage",
            isolated_point.attacker_advantage,
            policy.maximum_control_advantage,
        ),
        ResetLeakageAcceptanceCheck(
            check_id="constant_estimator_cannot_pass",
            passed=hidden_policy.required_correct > constant_best,
            observed=float(constant_best),
            requirement=f"required_correct > {constant_best}",
        ),
        ResetLeakageAcceptanceCheck(
            check_id="hidden_shots_match_calibration",
            passed=hidden_policy.shots_per_case == policy.shots_per_case,
            observed=float(hidden_policy.shots_per_case),
            requirement=f"equals {policy.shots_per_case}",
        ),
        ResetLeakageAcceptanceCheck(
            check_id="observation_disabled_control",
            passed=bool(observation_disabled_verified),
            observed=bool(observation_disabled_verified),
            requirement="must be verified",
        ),
    )
    return ResetLeakageAcceptanceReport(
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        passed=all(check.passed for check in checks),
        checks=checks,
    )


def _require_comparable_profiles(
    candidate: ResetCalibrationResult,
    perfect_reset_control: ResetCalibrationResult,
    isolated_resource_control: ResetCalibrationResult,
) -> None:
    reference = candidate.profile
    for name, result in (
        ("perfect_reset_control", perfect_reset_control),
        ("isolated_resource_control", isolated_resource_control),
    ):
        profile = result.profile
        if profile.reset_count != reference.reset_count:
            raise ValueError(f"{name} reset_count does not match candidate")
        if profile.victim_measurement != reference.victim_measurement:
            raise ValueError(f"{name} victim_measurement does not match candidate")
        if profile.model_mode is not reference.model_mode:
            raise ValueError(f"{name} model_mode does not match candidate")
    if (
        isolated_resource_control.profile.model_configuration_id
        != reference.model_configuration_id
    ):
        raise ValueError(
            "isolated_resource_control model configuration does not match candidate"
        )


def _point(
    result: ResetCalibrationResult,
    shots_per_case: int,
) -> LeakageCalibrationPoint:
    for point in result.points:
        if point.shots_per_case == shots_per_case:
            return point
    raise ValueError(
        f"calibration does not contain shots_per_case={shots_per_case}"
    )


def _minimum_check(
    check_id: str,
    observed: float,
    minimum: float,
) -> ResetLeakageAcceptanceCheck:
    return ResetLeakageAcceptanceCheck(
        check_id=check_id,
        passed=observed >= minimum,
        observed=observed,
        requirement=f">= {minimum}",
    )


def _maximum_check(
    check_id: str,
    observed: float,
    maximum: float,
) -> ResetLeakageAcceptanceCheck:
    return ResetLeakageAcceptanceCheck(
        check_id=check_id,
        passed=observed <= maximum,
        observed=observed,
        requirement=f"<= {maximum}",
    )


def _identifier(value: str, name: str) -> str:
    converted = str(value).strip()
    if not converted:
        raise ValueError(f"{name} must not be empty")
    return converted
