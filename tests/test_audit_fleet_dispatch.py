"""Focused checks for the unattended audit gate."""

from datetime import datetime
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from audit_fleet_dispatch import (  # noqa: E402
    MAX_COMPLETED_AUDITS_PER_DAY,
    MAX_FAILED_MODEL_ATTEMPTS_PER_DAY,
    due_roles, model_budget_remaining, order_model_reviews, reserve_model_run, role_path,
)


NOW = datetime.fromisoformat("2026-09-30T21:05:00+09:00")


class AuditFleetDispatchTests(unittest.TestCase):
    def test_initial_changes_are_queued_in_bounded_batches(self) -> None:
        hashes = {
            f"core/attack_simulation/module_{index}.py": str(index)
            for index in range(4)
        }
        state = {"version": 2, "roles": {}}
        due = due_roles(state, hashes, set(hashes), NOW)
        selected = next(paths for role, _, paths in due if role == "5")
        self.assertEqual(selected, sorted(hashes)[:2])
        self.assertEqual(len(state["roles"]["5"]["pending"]), 4)

    def test_deletion_after_baseline_is_not_lost(self) -> None:
        path = "core/attack_simulation/shared_qpu_runtime.py"
        state = {"version": 2, "roles": {
            "5": {"last_seen": {path: "before"}, "pending": []}
        }}
        due = due_roles(state, {}, set(), NOW)
        selected = next(paths for role, _, paths in due if role == "5")
        self.assertEqual(selected, [path])

    def test_deferred_selection_waits_for_new_evidence(self) -> None:
        path = "frontend/src/App.tsx"
        state = {"version": 2, "roles": {}}
        first = due_roles(state, {path: "v1"}, {path}, NOW)
        key = next(key for role, key, _ in first if role == "6")
        state["roles"]["6"]["deferred_key"] = key
        second = due_roles(state, {path: "v1"}, set(), NOW)
        self.assertNotIn("6", [role for role, _, _ in second])
        third = due_roles(state, {path: "v2"}, set(), NOW)
        self.assertIn("6", [role for role, _, _ in third])

    def test_permission_design_change_reaches_system_audit(self) -> None:
        self.assertTrue(role_path("5", "docs_for_develop/development/attack-simulation/service-and-access-model.md"))

    def test_model_launches_respect_daily_limit(self) -> None:
        state = {"version": 2, "roles": {}}
        self.assertEqual(model_budget_remaining(state, NOW), MAX_COMPLETED_AUDITS_PER_DAY)
        with patch("audit_fleet_dispatch.save_state") as save:
            for _ in range(MAX_COMPLETED_AUDITS_PER_DAY):
                reserve_model_run(state, NOW)
                state["model_budget"]["completed"] += 1
            with self.assertRaisesRegex(RuntimeError, "limit reached"):
                reserve_model_run(state, NOW)
        self.assertEqual(save.call_count, MAX_COMPLETED_AUDITS_PER_DAY)
        self.assertEqual(model_budget_remaining(state, NOW), 0)
        self.assertEqual(
            model_budget_remaining(state, NOW.replace(day=1, month=10)),
            MAX_COMPLETED_AUDITS_PER_DAY,
        )

    def test_failed_model_starts_have_a_separate_daily_circuit_breaker(self) -> None:
        state = {"version": 2, "roles": {}}
        with patch("audit_fleet_dispatch.save_state") as save:
            for _ in range(MAX_FAILED_MODEL_ATTEMPTS_PER_DAY):
                reserve_model_run(state, NOW)
                state["model_budget"]["failed_attempts"] += 1
            with self.assertRaisesRegex(RuntimeError, "limit reached"):
                reserve_model_run(state, NOW)
        self.assertEqual(save.call_count, MAX_FAILED_MODEL_ATTEMPTS_PER_DAY)

    def test_oldest_unreviewed_role_runs_first(self) -> None:
        state = {"roles": {
            "1": {"last_model_run": NOW.isoformat()}, "4": {}, "5": {},
        }}
        due = [("1", "slot", []), ("4", "change", []), ("5", "change", [])]
        self.assertEqual([role for role, _, _ in order_model_reviews(due, state)], ["4", "5", "1"])

    def test_failed_files_do_not_block_the_rest_of_the_queue(self) -> None:
        paths = [f"core/attack_simulation/module_{i}.py" for i in range(3)]
        hashes = {path: str(i) for i, path in enumerate(paths)}
        state = {"version": 2, "roles": {
            "5": {"last_seen": hashes.copy(), "pending": paths,
                  "failed_hashes": {paths[0]: "0", paths[1]: "1"}},
        }}
        due = due_roles(state, hashes, set(), NOW)
        selected = next(selected for role, _, selected in due if role == "5")
        self.assertEqual(selected, [paths[2]])


if __name__ == "__main__":
    unittest.main()
