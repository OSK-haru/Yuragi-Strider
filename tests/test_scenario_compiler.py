"""Focused tests for compile-once scenario configuration management."""

from dataclasses import dataclass, replace
import json
import unittest

from core.attack_simulation import (
    AssumptionBasis,
    AssumptionRecord,
    ExecutionRegime,
    InMemoryScenarioManifestStore,
    ObservationBudget,
    ResetLeakageRuntimePlan,
    ScenarioCompilationError,
    ScenarioCompiler,
    ScenarioConstraintViolation,
    ScenarioMode,
    ScenarioOverride,
    ScenarioPolicy,
    ScenarioPreset,
    ScenarioSession,
    ScenarioTemplate,
)
from core.attack_simulation.config_limits import MAX_CONFIG_BYTES, MAX_CONFIG_NODES
from core.attack_simulation.attack_path import (
    RESET_LEAKAGE_MECHANISM,
    reset_leakage_attack_path_config,
)
from core.attack_simulation.resource_binding import reset_resource_binding_config


class ScenarioCompilerTests(unittest.TestCase):
    def test_non_reset_template_compiles_without_training_or_victim_budget(self) -> None:
        paths = (
            "attacker.operation_grants", "victim.operation_grants",
            "qpu_service.supported_operations",
        )
        template = ScenarioTemplate(
            template_id="result_integrity_fixture",
            template_version="1",
            attacker_config={"operation_grants": ["program.submit"]},
            victim_config={"operation_grants": ["program.submit"]},
            qpu_service_config={"supported_operations": ["program.submit"]},
        )
        policy = ScenarioPolicy(
            mode=ScenarioMode.CHALLENGE,
            allowed_operations=("program.submit",),
            locked_fields=paths,
            evaluator_id="result_integrity_fixture",
            submission_enabled=True,
        )
        preset = ScenarioPreset(template, policy, tuple(
            AssumptionRecord(
                assumption_id=f"fixture.{path}",
                target_paths=(path,),
                basis=AssumptionBasis.SIMULATOR_CHOICE,
                rationale="This fixture declares the shared operation catalog.",
            ) for path in paths
        ))
        for mode in (ScenarioMode.FREE, ScenarioMode.CHALLENGE):
            with self.subTest(mode=mode):
                manifest = ScenarioCompiler().compile(preset.materialize(mode))
                self.assertIsNone(manifest.observation_budget)
                self.assertIsNone(manifest.to_dict()["policy"]["observation_budget"])
                self.assertIsNone(manifest.resolve_path("policy.observation_budget"))
                with self.assertRaises(KeyError):
                    manifest.resolve_path("policy.observation_budget.shots_per_query")
                self.assertNotIn("reset_model_id", manifest.qpu_service_config)
                self.assertEqual(manifest.submission_enabled, mode is ScenarioMode.CHALLENGE)
                # This proves configuration resolution only, not physics support.
                store = InMemoryScenarioManifestStore()
                self.assertIs(store.put(manifest), manifest)

    def test_existing_populated_budget_keeps_manifest_identity(self) -> None:
        manifest = ScenarioCompiler().compile(_session())
        # Schema v2 includes the challenge editable-field allowlist in identity;
        # changing preset editability must never retain the old content hash.
        self.assertEqual(manifest.manifest_id,
                         "sha256:0552266d5e1aa32c28abd1256fa1a059bf930a1f1969bea2322385c4056d5587")

    def test_reset_runtime_rejects_missing_budget_before_physics_resolution(self) -> None:
        session = _session()
        manifest = ScenarioCompiler().compile(replace(
            session, policy=replace(session.policy, observation_budget=None),
        ))
        with self.assertRaisesRegex(ValueError, "requires an explicit observation budget"):
            ResetLeakageRuntimePlan.from_manifest(manifest)

    def test_direct_manifest_rejects_untyped_budget(self) -> None:
        with self.assertRaises(TypeError):
            replace(ScenarioCompiler().compile(_session()), observation_budget={}, manifest_id="")

    def test_preset_carries_provenance_into_compiled_manifest(self) -> None:
        session = _session()
        preset = ScenarioPreset(
            template=session.template,
            challenge_policy=session.policy,
            assumptions=session.assumptions,
        )

        materialized = preset.materialize(ScenarioMode.CHALLENGE)
        manifest = ScenarioCompiler().compile(materialized)

        self.assertEqual(
            {item.assumption_id for item in manifest.assumptions},
            {item.assumption_id for item in session.assumptions},
        )
        self.assertEqual(
            preset.materialize(ScenarioMode.FREE).assumptions,
            session.assumptions,
        )

    def test_submittable_challenge_requires_permission_provenance(self) -> None:
        with self.assertRaisesRegex(
            ScenarioCompilationError,
            "requires provenance: attacker.operation_grants",
        ):
            ScenarioCompiler().compile(replace(_session(), assumptions=()))

    def test_submittable_challenge_rejects_grants_not_supported_by_service(self) -> None:
        session = _session()
        attacker = dict(session.template.attacker_config)
        attacker["operation_grants"] = [*attacker["operation_grants"], "admin.delete"]
        invalid = replace(
            session,
            template=replace(session.template, attacker_config=attacker),
        )

        with self.assertRaisesRegex(
            ScenarioCompilationError,
            "attacker.operation_grants contains operations unsupported",
        ):
            ScenarioCompiler().compile(invalid)

    def test_submittable_challenge_requires_permission_fields_to_be_locked(self) -> None:
        session = _session()
        policy = replace(
            session.policy,
            locked_fields=tuple(
                path for path in session.policy.locked_fields
                if path != "victim.operation_grants"
            ),
        )

        with self.assertRaisesRegex(
            ScenarioCompilationError,
            "must be locked: victim.operation_grants",
        ):
            ScenarioCompiler().compile(replace(session, policy=policy))

    def test_same_content_has_same_manifest_id_and_store_is_deduplicated(self) -> None:
        compiler = ScenarioCompiler()
        assumptions = (
            AssumptionRecord(
                assumption_id="paper.same_qubit_reuse",
                target_paths=("relation.placement.policy",),
                basis=AssumptionBasis.PAPER_ASSUMPTION,
                evidence_ref="Mi-CCS-2022",
                rationale="The reset-leakage experiment requires sequential reuse.",
                applicability={"scenario": "reset_leakage_v0"},
            ),
        )

        first = compiler.compile(_session(), assumptions=assumptions)
        second = compiler.compile(_session(), assumptions=assumptions)
        store = InMemoryScenarioManifestStore()

        self.assertEqual(first.manifest_id, second.manifest_id)
        self.assertIs(store.put(first), first)
        self.assertIs(store.put(second), first)
        self.assertIs(store.get(first.manifest_id), first)
        self.assertEqual(store.manifest_ids(), (first.manifest_id,))

    def test_mode_and_configuration_changes_change_manifest_identity(self) -> None:
        compiler = ScenarioCompiler()

        challenge = compiler.compile(_session())
        free = compiler.compile(_session(mode=ScenarioMode.FREE))
        isolated = compiler.compile(_session(
            placement_policy="isolated_physical_resource",
        ))

        self.assertNotEqual(challenge.manifest_id, free.manifest_id)
        self.assertNotEqual(challenge.manifest_id, isolated.manifest_id)

    def test_manifest_is_deeply_immutable_and_resolves_paths(self) -> None:
        manifest = ScenarioCompiler().compile(_session())

        self.assertEqual(
            manifest.resolve_path("relation.placement.policy"),
            "guaranteed_same_physical_qubit",
        )
        self.assertFalse(
            manifest.resolve_path("policy.reveal_individual_evaluations")
        )
        with self.assertRaises(TypeError):
            manifest.relation_config["placement"] = {"policy": "changed"}
        with self.assertRaises(TypeError):
            manifest.relation_config["placement"]["policy"] = "changed"

    def test_direct_manifest_copies_policy_path_lists_before_hashing(self) -> None:
        baseline = ScenarioCompiler().compile(_session())
        locked = ["attacker.reset_leakage_access"]
        hidden = ["attacker.reset_leakage_access"]
        manifest = replace(
            baseline,
            locked_fields=locked,
            hidden_fields=hidden,
            manifest_id="",
        )
        original_id = manifest.manifest_id
        original_content = manifest.to_dict()

        locked.append("victim.workload")
        hidden.append("victim.workload")

        self.assertEqual(manifest.locked_fields, ("attacker.reset_leakage_access",))
        self.assertEqual(manifest.hidden_fields, ("attacker.reset_leakage_access",))
        self.assertEqual(manifest.manifest_id, original_id)
        self.assertEqual(manifest.to_dict(), original_content)

    def test_authoring_rejects_deep_and_oversized_configuration(self) -> None:
        nested = "leaf"
        for _ in range(40):
            nested = {"child": nested}
        with self.assertRaisesRegex(ValueError, "nesting limit"):
            ScenarioTemplate(
                template_id="deep",
                template_version="1",
                attacker_config={"nested": nested},
                victim_config={},
                qpu_service_config={},
            )
        with self.assertRaisesRegex(ValueError, "byte limit"):
            ScenarioOverride(
                path="attacker.large",
                value="x" * (MAX_CONFIG_BYTES + 1),
                rationale="Oversized input.",
            )
        with self.assertRaisesRegex(ValueError, "node limit"):
            ScenarioOverride(
                path="attacker.wide",
                value=[None] * (MAX_CONFIG_NODES + 1),
                rationale="Oversized input.",
            )

    def test_manifest_store_has_separate_entry_and_byte_limits(self) -> None:
        challenge = ScenarioCompiler().compile(_session())
        free = ScenarioCompiler().compile(_session(mode=ScenarioMode.FREE))
        store = InMemoryScenarioManifestStore(max_entries=1)
        store.put(challenge)
        self.assertIs(store.put(challenge), challenge)
        with self.assertRaisesRegex(ValueError, "entry limit"):
            store.put(free)
        with self.assertRaisesRegex(ValueError, "byte limit"):
            InMemoryScenarioManifestStore(max_bytes=1).put(challenge)
        first_size = len(json.dumps(
            challenge.to_dict(), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8"))
        cumulative = InMemoryScenarioManifestStore(max_bytes=first_size)
        cumulative.put(challenge)
        with self.assertRaisesRegex(ValueError, "byte limit"):
            cumulative.put(free)

    def test_manifest_rejects_aggregate_overrides_above_the_byte_limit(self) -> None:
        baseline = ScenarioCompiler().compile(_session(mode=ScenarioMode.FREE))
        overrides = (
            ScenarioOverride("attacker.first", "x" * 600_000, "test"),
            ScenarioOverride("attacker.second", "y" * 600_000, "test"),
        )
        with self.assertRaisesRegex(ValueError, "byte limit"):
            replace(baseline, overrides=overrides, manifest_id="")

    def test_assumption_must_reference_an_existing_configuration_path(self) -> None:
        assumption = AssumptionRecord(
            assumption_id="missing",
            target_paths=("relation.missing.policy",),
            basis="simulator_choice",
            rationale="Invalid test reference.",
        )

        with self.assertRaises(ScenarioCompilationError):
            ScenarioCompiler().compile(_session(), assumptions=(assumption,))

    def test_named_cross_field_constraint_runs_during_compile(self) -> None:
        constraint = _CountingConstraint()
        compiler = ScenarioCompiler((constraint,))

        compiler.compile(_session())

        self.assertEqual(constraint.calls, 1)
        rejecting = ScenarioCompiler((_RejectingConstraint(),))
        with self.assertRaises(ScenarioConstraintViolation) as raised:
            rejecting.compile(_session())
        self.assertIn("reject-test", str(raised.exception))

    def test_reset_runtime_plan_contains_only_hot_path_values(self) -> None:
        manifest = ScenarioCompiler().compile(_session())

        plan = ResetLeakageRuntimePlan.from_manifest(manifest)

        self.assertEqual(plan.manifest_id, manifest.manifest_id)
        self.assertIs(plan.execution_regime, ExecutionRegime.NISQ_PHYSICAL)
        self.assertTrue(plan.access_config.labeled_training_enabled)
        self.assertEqual(
            plan.reset_model_id,
            "phenomenological_imperfect_reset",
        )
        self.assertEqual(
            plan.reset_model_configuration_id,
            "test-reset-config-v1",
        )
        self.assertFalse(hasattr(plan, "attacker_config"))
        self.assertFalse(hasattr(plan, "assumptions"))

    def test_unsupported_placement_is_rejected_before_runtime(self) -> None:
        manifest = ScenarioCompiler().compile(_session(
            placement_policy="scheduler_dependent",
        ))

        with self.assertRaises(ValueError):
            ResetLeakageRuntimePlan.from_manifest(manifest)

    def test_reset_path_must_be_declared_and_supported_before_runtime(self) -> None:
        baseline = ScenarioCompiler().compile(_session())
        missing = dict(baseline.relation_config)
        missing.pop("attack_path")
        with self.assertRaisesRegex(ValueError, "relation_config.attack_path"):
            ResetLeakageRuntimePlan.from_manifest(replace(
                baseline, relation_config=missing, manifest_id="",
            ))

        unsupported = dict(baseline.qpu_service_config)
        unsupported["supported_attack_mechanisms"] = []
        with self.assertRaisesRegex(ValueError, "not supported by the service"):
            ResetLeakageRuntimePlan.from_manifest(replace(
                baseline, qpu_service_config=unsupported, manifest_id="",
            ))

        wrong_resource = dict(baseline.relation_config)
        wrong_resource["attack_path"] = {
            **reset_leakage_attack_path_config(),
            "shared_resource": "separate_qpus",
        }
        with self.assertRaisesRegex(ValueError, "not supported by the reset-leakage runtime"):
            ResetLeakageRuntimePlan.from_manifest(replace(
                baseline, relation_config=wrong_resource, manifest_id="",
            ))

    def test_reset_resource_binding_rejects_mismatched_placements(self) -> None:
        baseline = ScenarioCompiler().compile(_session())
        for binding, message in (
            (reset_resource_binding_config("qpu-1", attacker_qubit_id=1),
             "identical resource IDs"),
            (reset_resource_binding_config("qpu-1", attacker_qubit_id=99),
             "absent from service inventory"),
            ({"victim": {"qpu_id": "qpu-1", "physical_qubit_id": 0},
              "attacker": {"qpu_id": "qpu-2", "physical_qubit_id": 0}},
             "only one QPU"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                ResetLeakageRuntimePlan.from_manifest(replace(
                    baseline,
                    relation_config={**baseline.relation_config, "resource_binding": binding},
                    manifest_id="",
                ))

        isolated = ScenarioCompiler().compile(_session(
            placement_policy="isolated_physical_resource",
        ))
        with self.assertRaisesRegex(ValueError, "distinct physical qubit IDs"):
            ResetLeakageRuntimePlan.from_manifest(replace(
                isolated,
                relation_config={**isolated.relation_config,
                                 "resource_binding": reset_resource_binding_config("qpu-1")},
                manifest_id="",
            ))

    def test_ftqc_profile_compiles_but_is_rejected_by_nisq_reset_runtime(self) -> None:
        manifest = ScenarioCompiler().compile(_session(
            execution_regime="fault_tolerant_logical",
        ))

        self.assertEqual(
            manifest.resolve_path("qpu_service.execution_regime"),
            "fault_tolerant_logical",
        )
        with self.assertRaisesRegex(ValueError, "nisq_physical"):
            ResetLeakageRuntimePlan.from_manifest(manifest)

    def test_effective_observation_model_cannot_enter_stateful_runtime(self) -> None:
        manifest = ScenarioCompiler().compile(_session(
            reset_model_mode="effective_observation_only",
        ))

        with self.assertRaisesRegex(ValueError, "state_transition_with_readout"):
            ResetLeakageRuntimePlan.from_manifest(manifest)

    def test_free_override_creates_a_new_manifest_without_mutating_template(self) -> None:
        session = _session(mode=ScenarioMode.FREE)

        manifest = ScenarioCompiler().compile(
            session,
            overrides=(ScenarioOverride(
                path="attacker.reset_leakage_access.labeled_training_enabled",
                value=False,
                rationale="Compare the attack without labelled reference data.",
            ),),
        )

        self.assertFalse(manifest.resolve_path(
            "attacker.reset_leakage_access.labeled_training_enabled"
        ))
        self.assertTrue(
            session.template.attacker_config["reset_leakage_access"][
                "labeled_training_enabled"
            ]
        )
        self.assertEqual(manifest.overrides[0].path, (
            "attacker.reset_leakage_access.labeled_training_enabled"
        ))
        self.assertEqual(
            manifest.assumptions[0].basis,
            AssumptionBasis.USER_OVERRIDE,
        )

    def test_challenge_preset_allows_unlocked_override_but_rejects_locked_field(self) -> None:
        # This compile-only fixture deliberately leaves execution_regime unlocked.
        editable = ScenarioOverride(
            path="qpu_service.execution_regime",
            value="ftqc_logical",
            rationale="The preset leaves this field editable in this compile-only test.",
        )
        session = _session()
        session = replace(
            session,
            policy=replace(session.policy, editable_fields=("qpu_service.execution_regime",)),
        )
        manifest = ScenarioCompiler().compile(session, overrides=(editable,))
        self.assertEqual(manifest.qpu_service_config["execution_regime"], "ftqc_logical")

        locked = ScenarioOverride(
            path="attacker.reset_leakage_access.labeled_training_enabled",
            value=False,
            rationale="Locked by the challenge preset.",
        )
        with self.assertRaisesRegex(ScenarioCompilationError, "not editable in the selected mode"):
            ScenarioCompiler().compile(_session(), overrides=(locked,))

        missing = ScenarioOverride(
            path="attacker.missing.enabled",
            value=False,
            rationale="Invalid path.",
        )
        with self.assertRaises(ScenarioCompilationError):
            ScenarioCompiler().compile(
                _session(mode=ScenarioMode.FREE),
                overrides=(missing,),
            )

    def test_free_override_must_be_declared_even_when_compiler_called_directly(self) -> None:
        session = _session(mode=ScenarioMode.FREE)
        with self.assertRaisesRegex(ScenarioCompilationError, "not editable in the selected mode"):
            ScenarioCompiler().compile(
                session,
                overrides=(ScenarioOverride(
                    "qpu_service.execution_regime", "ftqc_logical", "test",
                ),),
            )

    def test_free_override_declaration_must_reference_existing_field(self) -> None:
        with self.assertRaisesRegex(ValueError, "path does not exist"):
            ScenarioTemplate(
                template_id="invalid_editability",
                template_version="1",
                attacker_config={},
                victim_config={},
                qpu_service_config={},
                free_override_fields=("qpu_service.missing",),
            )

    def test_policy_lock_path_must_exist(self) -> None:
        valid = _session()
        invalid = ScenarioSession(
            template=valid.template,
            policy=ScenarioPolicy(
                mode=ScenarioMode.CHALLENGE,
                observation_budget=_budget(),
                locked_fields=("relation.missing",),
            ),
        )

        with self.assertRaises(ScenarioCompilationError):
            ScenarioCompiler().compile(invalid)

    def test_labeled_training_requires_an_experiment_label_source(self) -> None:
        manifest = ScenarioCompiler().compile(_session(
            include_label_source=False,
        ))

        with self.assertRaises(ValueError):
            ResetLeakageRuntimePlan.from_manifest(manifest)


@dataclass
class _CountingConstraint:
    constraint_id: str = "count-test"
    calls: int = 0

    def validate(self, manifest) -> None:
        self.calls += 1


@dataclass(frozen=True)
class _RejectingConstraint:
    constraint_id: str = "reject-test"

    def validate(self, manifest) -> None:
        raise ValueError("combination is invalid")


def _session(
    *,
    mode: ScenarioMode = ScenarioMode.CHALLENGE,
    placement_policy: str = "guaranteed_same_physical_qubit",
    execution_regime: str = "nisq_physical",
    reset_model_mode: str = "state_transition_with_readout",
    include_label_source: bool = True,
) -> ScenarioSession:
    template = ScenarioTemplate(
        template_id="reset_leakage_v0",
        template_version="1",
        attacker_config={
            "operation_grants": ["program.submit", "job.status.read", "job.result.read", "job.cancel"],
            "reset_leakage_access": {
                "labeled_training_enabled": True,
                "victim_observation_enabled": True,
            },
        },
        victim_config={"workload": "binary_output", "operation_grants": ["program.submit", "job.status.read", "job.result.read", "job.cancel"]},
        qpu_service_config={
            "execution_regime": execution_regime,
            "supported_operations": ["program.submit", "job.status.read", "job.result.read", "job.cancel"],
            "resource_inventory": {"qpu_id": "qpu-1", "physical_qubit_ids": [0, 1]},
            "reset_model_id": "phenomenological_imperfect_reset",
            "reset_model_configuration_id": "test-reset-config-v1",
            "reset_model_mode": reset_model_mode,
            "supported_placement_policies": [
                "guaranteed_same_physical_qubit",
                "scheduler_dependent",
                "isolated_physical_resource",
            ],
            "supported_attack_mechanisms": [RESET_LEAKAGE_MECHANISM],
        },
        relation_config={
            "placement": {"policy": placement_policy},
            "attack_path": reset_leakage_attack_path_config(),
            "resource_binding": reset_resource_binding_config(
                "qpu-1", isolated=placement_policy == "isolated_physical_resource",
            ),
        },
        experiment_config=(
            {"training": {"label_source": "controlled_reference"}}
            if include_label_source
            else {}
        ),
        free_override_fields=(
            "attacker.reset_leakage_access.labeled_training_enabled",
        ),
    )
    policy = (
        ScenarioPolicy(
            mode=ScenarioMode.CHALLENGE,
            observation_budget=_budget(),
            allowed_operations=("program.submit", "job.status.read", "job.result.read", "job.cancel"),
            locked_fields=(
                "attacker.reset_leakage_access",
                "attacker.operation_grants",
                "victim.operation_grants",
                "qpu_service.supported_operations",
                "relation.placement",
            ),
            evaluator_id="binary_victim_output_v1",
            submission_enabled=True,
        )
        if mode is ScenarioMode.CHALLENGE
        else ScenarioPolicy(
            mode=ScenarioMode.FREE,
            observation_budget=_budget(),
            allowed_operations=("program.submit", "job.status.read", "job.result.read", "job.cancel"),
        )
    )
    assumptions = tuple(
        AssumptionRecord(
            assumption_id=f"test.fixed.{path.replace('.', '_')}",
            target_paths=(path,),
            basis=AssumptionBasis.SIMULATOR_CHOICE,
            rationale="Test preset fixes this field for a deterministic fixture.",
        )
        for path in policy.locked_fields
    )
    return ScenarioSession(template=template, policy=policy, assumptions=assumptions)


def _budget() -> ObservationBudget:
    return ObservationBudget(
        shots_per_query=3,
        training_query_limit=1,
        victim_observation_limit=1,
        retry_limit=1,
    )


if __name__ == "__main__":
    unittest.main()
