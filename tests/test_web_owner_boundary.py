"""Trusted real-owner scope, not a cookie or public Web authentication test."""

from concurrent.futures import ThreadPoolExecutor
from copy import copy
from threading import Event, Barrier
import unittest
from unittest.mock import patch

from api.attack_sessions import AttackSessionRegistry, SessionCapacityError, WebOwnerContext
from core.attack_simulation.scenario_policy import ScenarioMode
from tests.test_attack_sessions import session_fixture


class WebOwnerBoundaryTests(unittest.TestCase):
    def test_real_owner_and_virtual_credentials_are_both_required_in_both_modes(self):
        for mode in (ScenarioMode.FREE, ScenarioMode.CHALLENGE):
            args = session_fixture(scenario_mode=mode)
            owner = WebOwnerContext()
            registry = AttackSessionRegistry(max_sessions=1)
            registry.register("owned", args[5], owner_context=owner)
            for actor in ("a", "v"):
                self.assertIs(registry.authenticated("owned", args[4][actor], owner_context=owner), args[5])
                for other in (None, WebOwnerContext(), copy(owner), args[4][actor], "owned"):
                    with self.assertRaises(PermissionError):
                        registry.authenticated("owned", args[4][actor], owner_context=other)
            with self.assertRaises(PermissionError):
                registry.authenticated("owned", "not-issued", owner_context=owner)

    def test_other_owner_cannot_discard_or_discover_an_environment(self):
        args = session_fixture()
        owner, other = WebOwnerContext(), WebOwnerContext()
        registry = AttackSessionRegistry(max_sessions=1)
        registry.register("owned", args[5], owner_context=owner)
        messages = []
        for name in ("owned", "absent"):
            with self.assertRaises(PermissionError) as caught:
                registry.discard(name, owner_context=other)
            messages.append(str(caught.exception))
        self.assertEqual(messages[0], messages[1])
        self.assertIs(registry.authenticated("owned", args[4]["a"], owner_context=owner), args[5])
        registry.discard("owned", owner_context=owner)
        self.assertTrue(args[5]._closed)
        self.assertEqual(args[1].events(), ())
        with self.assertRaises(PermissionError):
            registry.authenticated("owned", args[4]["a"], owner_context=owner)

    def test_registry_alias_does_not_bypass_or_replace_real_owner(self):
        args = session_fixture()
        owner = WebOwnerContext()
        registry = AttackSessionRegistry(max_sessions=2)
        registry.register("owned", args[5], owner_context=owner)
        # The fixture's earlier trusted registry still has an alias; virtual
        # Bearer alone cannot regain access or disposal through that alias.
        with self.assertRaises(PermissionError):
            args[6].authenticated("world", args[4]["a"])
        with self.assertRaises(PermissionError):
            args[6].discard("world")
        with self.assertRaises(ValueError):
            registry.register("foreign-owner", args[5], owner_context=WebOwnerContext())
        self.assertEqual(tuple(registry._sessions), ("owned",))

    def test_capacity_failure_does_not_strand_an_owner_binding(self):
        first, second = session_fixture(), session_fixture()
        registry = AttackSessionRegistry(max_sessions=1)
        owner = WebOwnerContext()
        registry.register("one", first[5], owner_context=owner)
        with self.assertRaises(SessionCapacityError):
            registry.register("two", second[5], owner_context=owner)
        self.assertIsNone(second[5]._web_owner_context)
        self.assertIs(second[6].authenticated("world", second[4]["a"]), second[5])

    def test_binding_is_before_requests_and_is_not_a_setting_thaw(self):
        args = session_fixture()
        receipt = args[5].receive(args[4]["a"], "device.list")
        registry = AttackSessionRegistry(max_sessions=1)
        with self.assertRaises(ValueError):
            registry.register("late", args[5], owner_context=WebOwnerContext())
        self.assertFalse(registry._sessions)
        self.assertIsNone(args[5]._web_owner_context)
        args[5].abandon_receipt(receipt)
        with self.assertRaises(RuntimeError):
            args[0].set_assumption(None)

    def test_parallel_owner_binding_has_exactly_one_winner(self):
        args = session_fixture()
        first, second = AttackSessionRegistry(max_sessions=1), AttackSessionRegistry(max_sessions=1)
        owners = WebOwnerContext(), WebOwnerContext()

        def register(registry, owner):
            try:
                registry.register("world", args[5], owner_context=owner)
                return True
            except ValueError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            attempts = [pool.submit(register, registry, owner)
                        for registry, owner in zip((first, second), owners)]
            self.assertEqual(sum(f.result(timeout=5) for f in attempts), 1)

    def test_actor_credential_cannot_become_real_owner_context(self):
        args = session_fixture()
        registry = AttackSessionRegistry(max_sessions=1)
        with self.assertRaises(TypeError):
            registry.register("owned", args[5], owner_context=args[4]["a"])
        self.assertFalse(registry._sessions)
        self.assertIsNone(args[5]._web_owner_context)

    def test_discarded_owner_binding_cannot_be_published_again(self):
        args = session_fixture()
        owner = WebOwnerContext()
        registry = AttackSessionRegistry(max_sessions=1)
        registry.register("owned", args[5], owner_context=owner)
        registry.discard("owned", owner_context=owner)
        for context in (owner, WebOwnerContext()):
            with self.assertRaises(ValueError):
                registry.register("again", args[5], owner_context=context)
        self.assertFalse(registry._sessions)

    def test_real_owner_cannot_be_attached_after_world_assembly(self):
        from tests.test_attack_workloads import fixture
        args, _, _ = fixture()
        registry = AttackSessionRegistry(max_sessions=1)
        with self.assertRaises(ValueError):
            registry.register("late-world", args[5], owner_context=WebOwnerContext())
        self.assertFalse(registry._sessions)

    def test_owned_session_requires_context_at_receipt_and_direct_request(self):
        args = session_fixture()
        owner = WebOwnerContext()
        registry = AttackSessionRegistry(max_sessions=1)
        registry.register("owned", args[5], owner_context=owner)
        with self.assertRaises(PermissionError):
            args[5].receive(args[4]["a"], "device.list")
        with self.assertRaises(PermissionError):
            args[5].request(args[4]["a"], lambda c, w: c.list_devices())
        self.assertEqual(args[5].receipt_records(), ())
        self.assertTrue(args[5].request(args[4]["a"], lambda c, w: c.list_devices(),
                                       _web_owner_context=owner))

    def test_owned_capture_also_precedes_control_wait(self):
        args = session_fixture()
        owner = WebOwnerContext()
        registry = AttackSessionRegistry(max_sessions=1)
        registry.register("owned", args[5], owner_context=owner)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with args[5]._lock:
                captured = pool.submit(args[5].receive, args[4]["a"], "device.list",
                                       _web_owner_context=owner)
                receipt = captured.result(timeout=5)
                self.assertIs(args[5].receipt_records()[0].receipt, receipt)
        args[5].abandon_receipt(receipt)
        self.assertEqual(args[1].events(), ())

    def test_capture_and_owner_binding_choose_one_atomic_order(self):
        for _ in range(8):
            args = session_fixture()
            registry = AttackSessionRegistry(max_sessions=1)
            barrier = Barrier(2, timeout=5)

            def capture():
                barrier.wait()
                try:
                    return args[5].receive(args[4]["a"], "device.list")
                except PermissionError:
                    return None

            def bind():
                barrier.wait()
                try:
                    registry.register("owned", args[5], owner_context=WebOwnerContext())
                    return True
                except ValueError:
                    return False

            with ThreadPoolExecutor(max_workers=2) as pool:
                receipt, binding = pool.submit(capture), pool.submit(bind)
                captured, bound = receipt.result(timeout=5), binding.result(timeout=5)
            self.assertEqual(captured is None, bound)
            self.assertEqual(len(args[5].receipt_records()), int(not bound))

    def test_alias_discard_fences_close_before_a_new_owner_can_bind(self):
        args = session_fixture()
        registry = AttackSessionRegistry(max_sessions=1)
        entered, release = Event(), Event()
        fence = args[5]._fence_close

        def delayed_fence():
            fence()
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test close fence was not released")

        with patch.object(args[5], "_fence_close", side_effect=delayed_fence), \
                ThreadPoolExecutor(max_workers=2) as pool:
            disposal = pool.submit(args[6].discard, "world")
            try:
                self.assertTrue(entered.wait(5))
                binding = pool.submit(registry.register, "owned", args[5], owner_context=WebOwnerContext())
            finally:
                release.set()
            disposal.result(timeout=5)
            with self.assertRaises(ValueError):
                binding.result(timeout=5)
        self.assertFalse(registry._sessions)
        self.assertTrue(args[5]._closed)
        self.assertIsNone(args[5]._web_owner_context)
        self.assertEqual(args[1].events(), ())


if __name__ == "__main__":
    unittest.main()
