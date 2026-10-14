"""Service assumptions freeze at establishment; terminal grammar stays shared."""

from dataclasses import replace
import unittest

from api.attack_terminal import TerminalAction, parse_terminal_command
from core.attack_simulation.job_authorization import JobAuthorizationPolicy
from core.attack_simulation.scenario_assumptions import AssumptionBasis, AssumptionRecord
from core.attack_simulation.scenario_compiler import ScenarioCompilationError, ScenarioOverride
from core.attack_simulation.scenario_environment import ScenarioEnvironment
from core.attack_simulation.scenario_policy import (
    ObservationBudget,
    ScenarioMode,
    ScenarioPolicy,
    ScenarioPreset,
    ScenarioSession,
    ScenarioTemplate,
)


def _session(mode: ScenarioMode) -> ScenarioSession:
    return ScenarioSession(
        template=ScenarioTemplate(
            template_id="service_conditions_test",
            template_version="1",
            attacker_config={"operation_grants": ["job.status.read", "job.cancel"]},
            victim_config={"operation_grants": ["job.status.read"]},
            qpu_service_config={
                "execution_regime": "nisq_physical",
                "supported_operations": ["job.status.read", "job.cancel"],
            },
            free_override_fields=("qpu_service.supported_operations",),
        ),
        policy=ScenarioPolicy(
            mode=mode,
            observation_budget=ObservationBudget(1, 0, 0),
            allowed_operations=("job.status.read", "job.cancel"),
            locked_fields=(
                "attacker.operation_grants",
                "victim.operation_grants",
                "qpu_service.supported_operations",
            ) if mode is ScenarioMode.CHALLENGE else (),
        ),
    )


class ServiceConditionLifecycleTests(unittest.TestCase):
    @staticmethod
    def _with_explicit_permissions(mode: ScenarioMode) -> ScenarioSession:
        base = _session(mode)
        policy = replace(base.policy, observation_budget=None,
                         evaluator_id="permission_fixture" if mode is ScenarioMode.CHALLENGE else None,
                         submission_enabled=mode is ScenarioMode.CHALLENGE)
        return replace(base, policy=policy, assumptions=tuple(
            AssumptionRecord(
                assumption_id=f"fixture.{path}", target_paths=(path,),
                basis=AssumptionBasis.SIMULATOR_CHOICE,
                rationale="Declared operation permissions for this fixture.",
            ) for path in policy.locked_fields
        ))

    def test_explicit_zero_grants_are_valid_and_deny_every_operation(self) -> None:
        for mode in (ScenarioMode.FREE, ScenarioMode.CHALLENGE):
            for role in ("attacker", "victim"):
                with self.subTest(mode=mode, role=role):
                    base = self._with_explicit_permissions(mode)
                    template = replace(base.template, **{f"{role}_config": {"operation_grants": []}})
                    environment = ScenarioEnvironment(replace(base, template=template))
                    manifest = environment.establish()
                    policy = JobAuthorizationPolicy.from_manifest(
                        manifest, attacker_tenant_id="attacker", victim_tenant_id="victim",
                    )
                    self.assertEqual(policy.actor_grants[role], frozenset())
                    for operation in manifest.allowed_operations:
                        with self.assertRaises(PermissionError):
                            policy.authorize(role, operation)
                    other = "victim" if role == "attacker" else "attacker"
                    policy.authorize(other, "job.status.read")
                    with self.assertRaises(RuntimeError):
                        environment.set_assumption(ScenarioOverride(
                            "qpu_service.supported_operations", ["job.status.read"], "test",
                        ))

    def test_zero_grants_do_not_allow_missing_malformed_or_duplicate_permissions(self) -> None:
        for mode in (ScenarioMode.FREE, ScenarioMode.CHALLENGE):
            for role in ("attacker", "victim"):
                for invalid in (None, {}, [1], [" "], ["job.status.read", "job.status.read"]):
                    with self.subTest(mode=mode, role=role, invalid=invalid):
                        base = self._with_explicit_permissions(mode)
                        template = replace(base.template, **{f"{role}_config": {"operation_grants": invalid}})
                        environment = ScenarioEnvironment(replace(base, template=template))
                        with self.assertRaises(ValueError):
                            manifest = environment.establish()
                            JobAuthorizationPolicy.from_manifest(
                                manifest, attacker_tenant_id="attacker", victim_tenant_id="victim",
                            )

    def test_service_catalog_still_requires_an_explicit_nonempty_list(self) -> None:
        for mode in (ScenarioMode.FREE, ScenarioMode.CHALLENGE):
            base = self._with_explicit_permissions(mode)
            template = replace(base.template, qpu_service_config={"supported_operations": []})
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                manifest = ScenarioEnvironment(replace(base, template=template)).establish()
                JobAuthorizationPolicy.from_manifest(
                    manifest, attacker_tenant_id="attacker", victim_tenant_id="victim",
                )

    def test_submittable_preset_is_checked_at_environment_establishment(self) -> None:
        base = _session(ScenarioMode.CHALLENGE)
        policy = replace(
            base.policy,
            evaluator_id="test_evaluator",
            submission_enabled=True,
        )
        assumptions = tuple(
            AssumptionRecord(
                assumption_id=f"test.fixed.{path.replace('.', '_')}",
                target_paths=(path,),
                basis=AssumptionBasis.SIMULATOR_CHOICE,
                rationale="This challenge fixes its operation capabilities.",
            )
            for path in policy.locked_fields
        )
        preset = ScenarioPreset(base.template, policy, assumptions)
        valid = ScenarioEnvironment(preset.materialize(ScenarioMode.CHALLENGE))
        self.assertTrue(valid.establish().submission_enabled)

        invalid_presets = (
            (replace(preset, assumptions=()), "requires provenance"),
            (
                replace(preset, challenge_policy=replace(
                    policy, allowed_operations=("job.status.read", "program.submit"),
                )),
                "exceed service capabilities",
            ),
        )
        for invalid_preset, reason in invalid_presets:
            with self.subTest(reason=reason):
                environment = ScenarioEnvironment(
                    invalid_preset.materialize(ScenarioMode.CHALLENGE)
                )
                with self.assertRaisesRegex(ScenarioCompilationError, reason):
                    environment.establish()
                self.assertFalse(environment.established)

    def test_undeclared_service_change_is_rejected_before_establishment(self) -> None:
        environment = ScenarioEnvironment(_session(ScenarioMode.FREE))
        change = ScenarioOverride(
            "qpu_service.execution_regime", "ftqc_logical", "test",
        )
        with self.assertRaises(PermissionError):
            environment.set_assumption(change)
        self.assertFalse(environment.established)

    def test_free_mode_changes_service_capability_only_before_establishment(self) -> None:
        environment = ScenarioEnvironment(_session(ScenarioMode.FREE))
        change = ScenarioOverride(
            "qpu_service.supported_operations",
            ["job.status.read"],
            "Model a service without cancellation.",
        )
        environment.set_assumption(change)
        manifest = environment.establish()
        self.assertEqual(manifest.qpu_service_config["supported_operations"], ("job.status.read",))
        self.assertEqual(manifest.resolve_path(change.path), ("job.status.read",))
        with self.assertRaises(TypeError):
            manifest.qpu_service_config["execution_regime"] = "changed"
        with self.assertRaises(RuntimeError):
            environment.set_assumption(change)

    def test_challenge_service_conditions_are_preset_only(self) -> None:
        environment = ScenarioEnvironment(_session(ScenarioMode.CHALLENGE))
        with self.assertRaises(PermissionError):
            environment.set_assumption(ScenarioOverride(
                "qpu_service.supported_operations", ["job.status.read"], "test",
            ))
        self.assertEqual(
            environment.establish().qpu_service_config["supported_operations"],
            ("job.status.read", "job.cancel"),
        )

    def test_terminal_syntax_does_not_depend_on_service_capabilities(self) -> None:
        full = ScenarioEnvironment(_session(ScenarioMode.FREE)).establish()
        limited_environment = ScenarioEnvironment(_session(ScenarioMode.FREE))
        limited_environment.set_assumption(ScenarioOverride(
            "qpu_service.supported_operations", ["job.status.read"], "test",
        ))
        limited = limited_environment.establish()
        self.assertNotEqual(full.manifest_id, limited.manifest_id)
        self.assertEqual(
            parse_terminal_command("job cancel job-7").action,
            TerminalAction.JOB_CANCEL,
        )
        full_policy = JobAuthorizationPolicy.from_manifest(
            full, attacker_tenant_id="attacker-tenant", victim_tenant_id="victim-tenant",
        )
        limited_policy = JobAuthorizationPolicy.from_manifest(
            limited, attacker_tenant_id="attacker-tenant", victim_tenant_id="victim-tenant",
        )
        full_policy.authorize("attacker-tenant", "job.cancel")
        with self.assertRaises(PermissionError):
            limited_policy.authorize("attacker-tenant", "job.cancel")


if __name__ == "__main__":
    unittest.main()
