"""Atomic pre-launch assumption freeze; no service or physical clock policy."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
import unittest
from unittest.mock import Mock

from core.attack_simulation.scenario_compiler import ScenarioCompiler, ScenarioOverride
from core.attack_simulation.scenario_environment import ScenarioEnvironment
from core.attack_simulation.scenario_policy import ScenarioMode
from tests.test_scenario_service_conditions import _session


def change():
    return ScenarioOverride("qpu_service.supported_operations", ["job.status.read"], "test service without cancel")


class ScenarioEnvironmentTests(unittest.TestCase):
    def test_concurrent_establish_publishes_one_complete_manifest_and_compiles_once(self):
        for mode in (ScenarioMode.FREE, ScenarioMode.CHALLENGE):
            with self.subTest(mode=mode):
                environment = ScenarioEnvironment(_session(mode))
                entered, release, second_called = Event(), Event(), Event()
                compiler = ScenarioCompiler()

                def compile_once(*args, **kwargs):
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError("test compile barrier timed out")
                    return compiler.compile(*args, **kwargs)

                first_compiler = Mock(spec=ScenarioCompiler, compile=Mock(side_effect=compile_once))
                unused = Mock(spec=ScenarioCompiler)

                def second_call():
                    second_called.set()
                    return environment.establish(unused)

                with ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(environment.establish, first_compiler)
                    try:
                        self.assertTrue(entered.wait(2))
                        second = pool.submit(second_call)
                        self.assertTrue(second_called.wait(2))
                    finally:
                        release.set()
                    self.assertIs(first.result(timeout=2), second.result(timeout=2))
                self.assertTrue(environment.established)
                self.assertIs(environment.establish(), first.result())
                first_compiler.compile.assert_called_once()
                unused.compile.assert_not_called()

    def test_concurrent_edit_cannot_succeed_after_compile_snapshot_is_taken(self):
        environment = ScenarioEnvironment(_session(ScenarioMode.FREE))
        entered, release, edit_called = Event(), Event(), Event()
        compiler = ScenarioCompiler()

        def compile_once(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test compile barrier timed out")
            return compiler.compile(*args, **kwargs)

        def edit():
            edit_called.set()
            environment.set_assumption(change())

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(environment.establish, Mock(compile=Mock(side_effect=compile_once)))
            try:
                self.assertTrue(entered.wait(2))
                second = pool.submit(edit)
                self.assertTrue(edit_called.wait(2))
            finally:
                release.set()
            manifest = first.result(timeout=2)
            with self.assertRaises(RuntimeError):
                second.result(timeout=2)
        self.assertEqual(manifest.overrides, ())
        self.assertEqual(manifest.qpu_service_config["supported_operations"], ("job.status.read", "job.cancel"))

    def test_compile_failure_does_not_establish_and_preserves_editable_preparation(self):
        environment = ScenarioEnvironment(_session(ScenarioMode.FREE))
        environment.set_assumption(change())
        with self.assertRaisesRegex(ValueError, "test validation failure"):
            environment.establish(Mock(compile=Mock(side_effect=ValueError("test validation failure"))))
        self.assertFalse(environment.established)
        environment.set_assumption(change())
        self.assertEqual(environment.establish().qpu_service_config["supported_operations"], ("job.status.read",))

    def test_reentrant_compile_cannot_mutate_or_recursively_establish(self):
        environment = ScenarioEnvironment(_session(ScenarioMode.FREE))

        def compile_once(session, *, overrides):
            with self.assertRaises(RuntimeError):
                environment.set_assumption(change())
            with self.assertRaises(RuntimeError):
                environment.establish()
            return ScenarioCompiler().compile(session, overrides=overrides)

        manifest = environment.establish(Mock(compile=Mock(side_effect=compile_once)))
        self.assertEqual(manifest.overrides, ())
        self.assertTrue(environment.established)

    def test_invalid_compiler_result_is_not_published_and_failure_can_be_retried(self):
        environment = ScenarioEnvironment(_session(ScenarioMode.FREE))
        with self.assertRaises(TypeError):
            environment.establish(Mock(compile=Mock(return_value=object())))
        self.assertFalse(environment.established)
        self.assertEqual(environment.establish().template_id, environment.session.template.template_id)

    def test_session_identity_cannot_be_replaced_before_or_after_freeze(self):
        for mode in (ScenarioMode.FREE, ScenarioMode.CHALLENGE):
            with self.subTest(mode=mode):
                environment = ScenarioEnvironment(_session(mode))
                original = environment.session
                replacement = replace(original, template=replace(original.template, template_id="other"))
                for established in (False, True):
                    with self.subTest(established=established):
                        if established:
                            environment.establish()
                        with self.assertRaises(AttributeError):
                            environment.session = replacement
                        self.assertIs(environment.session, original)
                self.assertEqual(environment.establish().template_id, original.template.template_id)


if __name__ == "__main__":
    unittest.main()
