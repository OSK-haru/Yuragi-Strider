"""Benchmark the complete internal reset-leakage hidden-grader path."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys
from time import perf_counter


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.attack_simulation import (  # noqa: E402
    FrequencyThresholdEstimator,
    HiddenEvaluationPolicy,
    ObservationBudget,
    QpuStateStore,
    ResetLeakageHiddenGrader,
    ResetLeakageOrchestratorConfig,
    ResetLeakageRuntimePlan,
    ResetLeakageScenarioOrchestrator,
    ScenarioCompiler,
    ScenarioMode,
    ScenarioPolicy,
    ScenarioSession,
    ScenarioTemplate,
    SharedQpuRuntime,
    SharedQpuRuntimeConfig,
    single_qubit_zero_snapshot,
)
from core.experimental_isolation_physics import (  # noqa: E402
    ImperfectResetSpec,
    PhenomenologicalResetModel,
    ResetModelRegistry,
    calibrate_frequency_threshold,
    derive_reset_profile,
)
from core.attack_simulation.resource_binding import (  # noqa: E402
    PhysicalQubitRef,
    ResetResourceBinding,
    reset_resource_binding_config,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--success-probability", type=float, required=True)
    parser.add_argument("--target-excited-population", type=float, required=True)
    parser.add_argument("--observed-one-given-zero", type=float, required=True)
    parser.add_argument("--observed-zero-given-one", type=float, required=True)
    parser.add_argument("--shots-per-case", type=int, required=True)
    parser.add_argument("--case-count", type=int, required=True)
    parser.add_argument("--zero-case-count", type=int, required=True)
    parser.add_argument("--required-correct", type=int, required=True)
    parser.add_argument("--repetitions", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--generated-at-utc", required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def _build_orchestrator(
    model: PhenomenologicalResetModel,
    *,
    shots_per_case: int,
    seed: int,
) -> ResetLeakageScenarioOrchestrator:
    store = QpuStateStore(single_qubit_zero_snapshot(
        qpu_id="calibration-qpu",
        physical_qubit_id=0,
        device_config_version="reset-calibration-v1",
    ))
    registry = ResetModelRegistry()
    registry.register(model)
    runtime = SharedQpuRuntime(
        state_store=store,
        reset_models=registry,
        config=SharedQpuRuntimeConfig(
            reset_model_id=model.model_id,
            reset_model_configuration_id=model.configuration_id,
            resource_binding=ResetResourceBinding(
                PhysicalQubitRef("calibration-qpu", 0),
                PhysicalQubitRef("calibration-qpu", 0),
            ),
        ),
    )
    session = ScenarioSession(
        template=ScenarioTemplate(
            template_id="reset_leakage_calibration",
            template_version="1",
            attacker_config={
                "workspace": "terminal",
                "operation_grants": ["program.submit", "job.status.read", "job.result.read", "job.cancel"],
                "reset_leakage_access": {
                    "labeled_training_enabled": True,
                    "victim_observation_enabled": True,
                },
            },
            victim_config={"workload": "binary_output", "operation_grants": ["program.submit", "job.status.read", "job.result.read", "job.cancel"]},
            qpu_service_config={
                "execution_regime": "nisq_physical",
                "supported_operations": ["program.submit", "job.status.read", "job.result.read", "job.cancel"],
                "resource_inventory": {"qpu_id": "calibration-qpu", "physical_qubit_ids": [0, 1]},
                "reset_model_id": model.model_id,
                "reset_model_configuration_id": model.configuration_id,
                "reset_model_mode": "state_transition_with_readout",
                "supported_placement_policies": [
                    "guaranteed_same_physical_qubit",
                ],
                "supported_attack_mechanisms": ["incomplete_reset_state_carryover"],
            },
            relation_config={
                "resource_binding": reset_resource_binding_config("calibration-qpu"),
                "attack_path": {
                    "attacker_entry": "job_submission",
                    "shared_resource": "same_physical_qubit",
                    "mechanism_id": "incomplete_reset_state_carryover",
                    "victim_asset": "nonpublic_output_bit",
                    "attacker_observation": "own_measurement_counts",
                },
                "placement": {
                    "policy": "guaranteed_same_physical_qubit",
                },
            },
            experiment_config={
                "training": {"label_source": "controlled_reference"},
            },
        ),
        policy=ScenarioPolicy(
            mode=ScenarioMode.CHALLENGE,
            allowed_operations=("program.submit", "job.status.read", "job.result.read", "job.cancel"),
            locked_fields=(
                "attacker.operation_grants",
                "victim.operation_grants",
                "qpu_service.supported_operations",
            ),
            observation_budget=ObservationBudget(
                shots_per_query=shots_per_case,
                training_query_limit=1,
                victim_observation_limit=1,
                retry_limit=0,
            ),
            evaluator_id="binary_victim_output_v1",
            submission_enabled=True,
        ),
    )
    plan = ResetLeakageRuntimePlan.from_manifest(
        ScenarioCompiler().compile(session)
    )
    return ResetLeakageScenarioOrchestrator(
        runtime=runtime,
        plan=plan,
        config=ResetLeakageOrchestratorConfig(instance_id="calibration-instance"),
        secret_seed=seed,
        measurement_seed=seed + 1,
    )


def main() -> None:
    args = _parse_args()
    repetitions = int(args.repetitions)
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    excited = float(args.target_excited_population)
    model = PhenomenologicalResetModel(
        spec=ImperfectResetSpec(
            success_probability=args.success_probability,
            target_populations=(1.0 - excited, excited),
        ),
        observed_one_given_zero=args.observed_one_given_zero,
        observed_zero_given_one=args.observed_zero_given_one,
    )
    profile = derive_reset_profile(
        model,
        reset_count=1,
        victim_measurement=True,
        profile_id="reset_leakage_grader_benchmark",
        generated_at_utc=args.generated_at_utc,
    )
    threshold = calibrate_frequency_threshold(
        profile.one_probability_given_victim_zero,
        profile.one_probability_given_victim_one,
        args.shots_per_case,
    )
    estimator = FrequencyThresholdEstimator(
        threshold=threshold.frequency_threshold,
        predict_one_when_at_or_above=threshold.predict_one_when_at_or_above,
    )
    orchestrator = _build_orchestrator(
        model,
        shots_per_case=args.shots_per_case,
        seed=args.seed,
    )

    durations: list[float] = []
    correct_counts: list[int] = []
    pass_count = 0
    for repetition in range(repetitions):
        policy = HiddenEvaluationPolicy(
            policy_id="reset_hidden_calibration",
            policy_version="1",
            case_count=args.case_count,
            zero_case_count=args.zero_case_count,
            shots_per_case=args.shots_per_case,
            required_correct=args.required_correct,
            case_order_seed=args.seed + 2 * repetition,
            measurement_seed=args.seed + 2 * repetition + 1,
        )
        grader = ResetLeakageHiddenGrader(
            orchestrator=orchestrator,
            policy=policy,
        )
        start = perf_counter()
        result = grader.grade(estimator)
        durations.append(perf_counter() - start)
        correct_counts.append(result.correct_count)
        pass_count += int(result.passed)

    ordered_durations = sorted(durations)
    percentile_index = max(0, int(0.95 * repetitions) - 1)
    payload = {
        "contract": "reset_leakage_grader_benchmark_v1",
        "profile": profile.to_dict(),
        "estimator": {
            "count_threshold": threshold.count_threshold,
            "frequency_threshold": threshold.frequency_threshold,
            "predict_one_when_at_or_above": (
                threshold.predict_one_when_at_or_above
            ),
        },
        "hidden_policy": {
            "case_count": args.case_count,
            "zero_case_count": args.zero_case_count,
            "shots_per_case": args.shots_per_case,
            "required_correct": args.required_correct,
        },
        "sample": {
            "repetitions": repetitions,
            "pass_count": pass_count,
            "empirical_pass_rate": pass_count / repetitions,
            "mean_correct": statistics.fmean(correct_counts),
            "minimum_correct": min(correct_counts),
            "maximum_correct": max(correct_counts),
        },
        "runtime_seconds": {
            "mean": statistics.fmean(durations),
            "median": statistics.median(durations),
            "p95_nearest_rank": ordered_durations[percentile_index],
            "minimum": min(durations),
            "maximum": max(durations),
        },
        "claims": {
            "scope": "internal_end_to_end_hidden_grader",
            "hardware_replication": False,
            "host_specific": True,
        },
    }
    encoded = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
