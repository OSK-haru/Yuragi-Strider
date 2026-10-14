"""Credential-delivery assumptions are configurable only before start."""

import unittest

from core.attack_simulation.credential_policy import CredentialDelivery, CredentialOrigin
from core.attack_simulation.scenario_compiler import ScenarioCompilationError, ScenarioOverride
from core.attack_simulation.scenario_environment import ScenarioEnvironment
from core.attack_simulation.scenario_policy import (
    ObservationBudget, ScenarioMode, ScenarioPolicy, ScenarioSession, ScenarioTemplate,
)


def session(mode: ScenarioMode) -> ScenarioSession:
    return ScenarioSession(
        template=ScenarioTemplate(
            template_id="credential_test", template_version="1",
            attacker_config={
                "credential_delivery": "preconfigured_profile",
                "credential_origin": "simulator_issued",
            },
            victim_config={"workload": "private"},
            qpu_service_config={"supported_credential_deliveries": [
                "preconfigured_profile", "manual_simulated_credential",
            ]},
            free_override_fields=(
                "attacker.credential_delivery",
                "attacker.credential_origin",
            ),
        ),
        policy=ScenarioPolicy(
            mode=mode,
            observation_budget=ObservationBudget(1, 0, 0),
            locked_fields=("attacker.credential_delivery",) if mode is ScenarioMode.CHALLENGE else (),
        ),
    )


class CredentialLifecycleTests(unittest.TestCase):
    def test_free_mode_changes_method_before_start_and_freezes_it(self) -> None:
        environment = ScenarioEnvironment(session(ScenarioMode.FREE))
        change = ScenarioOverride(
            "attacker.credential_delivery",
            CredentialDelivery.MANUAL_SIMULATED_CREDENTIAL.value,
            "Test a manually supplied virtual service credential.",
        )
        environment.set_assumption(change)
        self.assertFalse(environment.established)
        manifest = environment.establish()
        self.assertTrue(environment.established)
        self.assertEqual(manifest.attacker_config["credential_delivery"], change.value)
        self.assertEqual(manifest.attacker_config["credential_origin"], CredentialOrigin.SIMULATOR_ISSUED.value)
        self.assertIs(environment.establish(), manifest)
        with self.assertRaises(RuntimeError):
            environment.set_assumption(change)

    def test_challenge_cannot_change_preset_even_before_start(self) -> None:
        environment = ScenarioEnvironment(session(ScenarioMode.CHALLENGE))
        with self.assertRaises(PermissionError):
            environment.set_assumption(ScenarioOverride(
                "attacker.credential_delivery", "manual_simulated_credential", "test",
            ))
        self.assertEqual(
            environment.establish().attacker_config["credential_delivery"],
            "preconfigured_profile",
        )

    def test_unsupported_delivery_fails_without_establishing(self) -> None:
        environment = ScenarioEnvironment(session(ScenarioMode.FREE))
        environment.set_assumption(ScenarioOverride(
            "attacker.credential_delivery", "unknown", "test",
        ))
        with self.assertRaises(ScenarioCompilationError):
            environment.establish()
        self.assertFalse(environment.established)
        environment.set_assumption(ScenarioOverride(
            "attacker.credential_delivery", "preconfigured_profile", "repair",
        ))
        environment.establish()

    def test_user_defined_credential_origin_is_not_silently_accepted(self) -> None:
        environment = ScenarioEnvironment(session(ScenarioMode.FREE))
        environment.set_assumption(ScenarioOverride(
            "attacker.credential_origin", "user_defined", "test",
        ))
        with self.assertRaises(ScenarioCompilationError):
            environment.establish()


if __name__ == "__main__":
    unittest.main()
