"""Frozen live assumptions and pure cancellation trace boundaries."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import math
import unittest

from core.attack_simulation.event_commit_boundary import EventPosition
from core.attack_simulation.live_ingress import LiveIngressSettings
from core.attack_simulation.live_service_config import (
    CancellationPolicy, ControlExecutionWindow, DeclaredControlTiming,
    LiveServiceConfig, parse_live_service_config,
)
from core.attack_simulation.scenario_environment import ScenarioEnvironment
from core.attack_simulation.scenario_policy import (
    ScenarioMode, ScenarioPolicy, ScenarioSession, ScenarioTemplate,
)


LIVE = {"receipt_origin": "authenticated_complete_envelope", "outbound_delay_us": 7.5,
        "basis": "Test-only explicit post-envelope transport assumption."}
CONTROL = {"processing_delay_us": 2, "publication_delay_us": 3,
           "basis": "Test-only explicit control timing, not a provider measurement.",
           "cancellation_policy": "queued_only"}
CLOCK = {"clock_domain": "world", "rate": 4}


def manifest(*, mode=ScenarioMode.FREE, live=LIVE, control=CONTROL, clock=CLOCK, other=None):
    config = dict(other or {})
    if live is not None:
        config["live_ingress"] = deepcopy(live)
    if control is not None:
        config["control_timing"] = deepcopy(control)
    environment = ScenarioEnvironment(ScenarioSession(
        template=ScenarioTemplate("live_config_fixture", "1", {}, {}, config,
            experiment_config={} if clock is None else {"world_clock": deepcopy(clock)}),
        policy=ScenarioPolicy(mode),
    ))
    return environment, environment.establish()


def parse(value, **kwargs):
    return parse_live_service_config(value, **{"max_pending": 4, "max_history": 8, **kwargs})


def timing(**kwargs):
    return DeclaredControlTiming(**{
        "clock_domain": "world", "processing_delay_us": 2, "publication_delay_us": 3,
        "basis": "Test-only explicit assumption.",
        "cancellation_policy": CancellationPolicy.QUEUED_ONLY, **kwargs,
    })


class LiveServiceConfigTests(unittest.TestCase):
    def test_frozen_manifest_both_modes_use_same_explicit_settings(self):
        snapshots = []
        for mode in ScenarioMode:
            environment, frozen = manifest(mode=mode)
            before = frozen.to_dict()
            result = parse(frozen)
            self.assertEqual(result.ingress, LiveIngressSettings(
                "world", "host_monotonic_ns", 7.5, LIVE["basis"], 4, 8))
            self.assertEqual(result.control_timing.processing_delay_us, 2)
            self.assertEqual(result.control_timing.publication_delay_us, 3)
            self.assertIs(result.control_timing.cancellation_policy, CancellationPolicy.QUEUED_ONLY)
            self.assertEqual(result.control_timing.basis, CONTROL["basis"])
            self.assertEqual(result.control_timing.plan_cancel(EventPosition("world", 10, 9)),
                ControlExecutionWindow(EventPosition("world", 10, 9),
                                       EventPosition("world", 12, 0), EventPosition("world", 15, 0)))
            self.assertEqual(frozen.to_dict(), before)
            with self.assertRaises(RuntimeError):
                environment.set_assumption(None)
            snapshots.append(result)
        self.assertEqual(*snapshots)

    def test_output_is_immutable_and_detached_from_authoring_data(self):
        source = deepcopy(LIVE)
        _, frozen = manifest(live=source)
        result = parse(frozen)
        source["outbound_delay_us"] = 99
        self.assertEqual(result.ingress.outbound_delay_us, 7.5)
        for obj, field in ((result, "ingress"), (result.ingress, "basis"),
                           (result.control_timing, "processing_delay_us")):
            with self.assertRaises(FrozenInstanceError):
                setattr(obj, field, None)
        with self.assertRaises(TypeError):
            frozen.qpu_service_config["live_ingress"]["outbound_delay_us"] = 99

    def test_parser_requires_manifest_and_explicit_trusted_host_caps(self):
        _, frozen = manifest()
        for value in (frozen.to_dict(), None, object()):
            with self.subTest(value=type(value)), self.assertRaises(TypeError):
                parse(value)
        with self.assertRaises(TypeError):
            parse_live_service_config(frozen)
        for name in ("max_pending", "max_history"):
            for bad in (True, 0, -1, 1.0, "4", None):
                with self.subTest(name=name, bad=bad), self.assertRaises(ValueError):
                    parse(frozen, **{name: bad})

    def test_each_live_control_clock_field_is_required_and_unknown_fields_reject(self):
        for name, block in (("live", LIVE), ("control", CONTROL), ("clock", CLOCK)):
            for missing in block:
                value = {key: child for key, child in block.items() if key != missing}
                with self.subTest(block=name, missing=missing), self.assertRaises(ValueError):
                    parse(manifest(**{name: value})[1])
            with self.subTest(block=name, extra=True), self.assertRaises(ValueError):
                parse(manifest(**{name: {**block, "unexpected": 1}})[1])
            for nonobject in (None, [], "settings"):
                with self.subTest(block=name, value=nonobject), self.assertRaises(TypeError):
                    parse(manifest(**{name: nonobject})[1])

    def test_receipt_origin_and_policy_have_no_implicit_fallback(self):
        for bad in ("button_press", "complete_envelope", " authenticated_complete_envelope", True, 0, None):
            with self.subTest(origin=bad), self.assertRaises(ValueError):
                parse(manifest(live={**LIVE, "receipt_origin": bad})[1])
        for bad in ("best_effort", "queued_only ", "running_allowed", True, 0, None):
            with self.subTest(policy=bad), self.assertRaises(ValueError):
                parse(manifest(control={**CONTROL, "cancellation_policy": bad})[1])

    def test_scenario_cannot_supply_host_metadata_or_bookkeeping_limits(self):
        for field, value in (("host_clock_domain", "client_ns"), ("clock_domain", "device"),
                             ("max_pending", 99), ("max_history", 99),
                             ("received_at_ns", 1), ("order", 3)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                parse(manifest(live={**LIVE, field: value})[1])

    def test_explicit_zero_and_supported_boundary_widths_are_preserved(self):
        _, frozen = manifest(live={**LIVE, "outbound_delay_us": 0, "basis": "a" * 1024},
                             control={**CONTROL, "processing_delay_us": 0,
                                      "publication_delay_us": 0, "basis": "b" * 1024},
                             clock={"clock_domain": "d" * 128, "rate": 0.5})
        result = parse(frozen, max_pending=1, max_history=1)
        self.assertEqual(result.ingress.outbound_delay_us, 0)
        self.assertEqual(result.control_timing.plan_cancel(EventPosition("d" * 128, 0, 4)).release,
                         EventPosition("d" * 128, 0, 6))
        with self.assertRaises(TypeError):
            DeclaredControlTiming("world", 0, 0)

    def test_live_ingress_reused_type_rejects_nonfinite_delay(self):
        for bad in (math.nan, math.inf, -math.inf):
            with self.subTest(delay=bad), self.assertRaises(ValueError):
                LiveIngressSettings("world", "host_monotonic_ns", bad, "Explicit test basis", 4, 8)

    def test_numeric_types_ranges_basis_and_domain_reject(self):
        for block_name, field in (("live", "outbound_delay_us"),
                                  ("control", "processing_delay_us"),
                                  ("control", "publication_delay_us")):
            block = LIVE if block_name == "live" else CONTROL
            for bad in (True, "1", None, -1, 10 ** 400):
                with self.subTest(block=block_name, field=field, bad=bad), self.assertRaises(ValueError):
                    parse(manifest(**{block_name: {**block, field: bad}})[1])
        for block_name, block in (("live", LIVE), ("control", CONTROL)):
            for bad in ("", " \t", "a" * 1025, True, 1, None):
                with self.subTest(block=block_name, basis=bad), self.assertRaises(ValueError):
                    parse(manifest(**{block_name: {**block, "basis": bad}})[1])
        for domain in ("", "あ", "a" * 129, 1, None):
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                parse(manifest(clock={**CLOCK, "clock_domain": domain})[1])
        for rate in (0, -1, True, "1", 10 ** 400):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                parse(manifest(clock={**CLOCK, "rate": rate})[1])

    def test_unused_profiles_are_not_validated_repaired_or_defaulted(self):
        _, frozen = manifest(other={"declared_service_timing": {"processing_delay_us": -9},
                                    "future_service_profile": {"unsupported_field": "future"}})
        self.assertEqual(parse(frozen).ingress.outbound_delay_us, 7.5)
        # Older closed manifests still establish without either new block.
        _, old = manifest(live=None, control=None)
        self.assertNotIn("live_ingress", old.qpu_service_config)
        with self.assertRaises(TypeError):
            parse(old)

    def test_control_positions_add_each_delay_once_and_zero_only_advances_order(self):
        arrival = EventPosition("world", 10, 99)
        cases = ((0, 0, (10, 100), (10, 101)), (0, 3, (10, 100), (13, 0)),
                 (2, 0, (12, 0), (12, 1)), (2, 3, (12, 0), (15, 0)))
        for processing, publication, application, release in cases:
            with self.subTest(processing=processing, publication=publication):
                result = timing(processing_delay_us=processing,
                                publication_delay_us=publication).plan_cancel(arrival)
                self.assertIs(result.arrival, arrival)
                self.assertEqual(result.application.key, application)
                self.assertEqual(result.release.key, release)
                self.assertEqual(result, timing(processing_delay_us=processing,
                                               publication_delay_us=publication).plan_cancel(arrival))

    def test_control_timing_rejects_bad_direct_values_without_host_or_mutation(self):
        for field in ("processing_delay_us", "publication_delay_us"):
            for bad in (True, -1, math.nan, math.inf, -math.inf, "1", 10 ** 400):
                with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                    timing(**{field: bad})
        for bad in ("queued_only", None, object()):
            with self.assertRaises(ValueError):
                timing(cancellation_policy=bad)
        for bad in (None, ("world", 1, 0), EventPosition("device", 1, 0)):
            with self.assertRaises((TypeError, ValueError)):
                timing().plan_cancel(bad)

    def test_positive_collapse_and_overflow_at_either_control_boundary_reject(self):
        for clock in (EventPosition("world", 1e20, 3), EventPosition("world", 1e308, 3)):
            delay = 1 if clock.time_us == 1e20 else 1e308
            for processing, publication in ((delay, 0), (0, delay)):
                with self.subTest(time=clock.time_us, processing=processing), self.assertRaises(ValueError):
                    timing(processing_delay_us=processing,
                           publication_delay_us=publication).plan_cancel(clock)
        tiny = math.ulp(0.0)
        self.assertEqual(timing(processing_delay_us=tiny, publication_delay_us=0)
                         .plan_cancel(EventPosition("world", 0, 0)).application.time_us, tiny)

    def test_control_window_and_combined_config_reject_foreign_or_noncausal_parts(self):
        p = EventPosition("world", 1, 0)
        for application, release in ((p, EventPosition("world", 2, 0)),
                                      (EventPosition("world", 2, 0), p),
                                      (EventPosition("device", 2, 0), EventPosition("world", 3, 0)),
                                      (None, EventPosition("world", 3, 0))):
            with self.assertRaises((TypeError, ValueError)):
                ControlExecutionWindow(p, application, release)
        result = parse(manifest()[1])
        with self.assertRaises(ValueError):
            LiveServiceConfig(result.ingress, replace(result.control_timing, clock_domain="device"))
        with self.assertRaises(TypeError):
            LiveServiceConfig(result.ingress, None)


if __name__ == "__main__":
    unittest.main()
