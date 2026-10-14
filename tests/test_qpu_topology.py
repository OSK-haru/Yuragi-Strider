"""Full-device sparse metadata stays distinct from the local state tensor."""

from dataclasses import replace
import unittest
from unittest.mock import patch

from core.attack_simulation.qpu_topology import (
    ControlLine, ControlTransferLink, QpuTopology, ReadoutGroup,
    RetainedRegionBinding, TopologyLimits,
)
from core.attack_simulation.qpu_state import QpuStateStore, QpuStateTransition, local_zero_snapshot
from core.attack_simulation.resource_binding import PhysicalQubitLayout
from tests.test_common_command_pipeline import pipeline
from tests.test_local_program_runtime import program_json, submit
from core.attack_simulation.circuit_program import NATIVE_GATES
from core.attack_simulation.program_request import ProgramRequestError
from core.attack_simulation.resource_binding import PhysicalQubitRef, ResetResourceBinding


def large_topology() -> QpuTopology:
    ids = tuple(10 * i + 7 for i in range(300))
    # A grid with branches/cycles plus a disconnected component, not a chain.
    edges = []
    for i in range(280):
        if i % 20 != 19:
            edges.append((ids[i], ids[i + 1]))
        if i + 20 < 280:
            edges.append((ids[i], ids[i + 20]))
    edges.append((ids[290], ids[291]))
    return QpuTopology(
        "qpu-1", "grid-with-remote-lines-v1", ids, TopologyLimits(512, 1200, 600),
        gate_edges=tuple(edges), residual_zz_edges=((ids[0], ids[20]),),
        control_lines=(ControlLine("drive-main", (ids[0], ids[20])),),
        control_transfers=(ControlTransferLink("drive-main", ids[299]),),
        readout_groups=(ReadoutGroup("shared-classifier", (ids[0], ids[20], ids[299])),),
    )


class QpuTopologyTests(unittest.TestCase):
    def test_unrouted_gate_constraints_reject_before_queue_without_state_changes(self):
        for edges, accepted in (((), False), (((3, 11),), True)):
            topology = QpuTopology("qpu-1", "v1", (3, 11, 99), TopologyLimits(3, 4, 4),
                                   gate_edges=edges)
            binding = RetainedRegionBinding(topology, (3, 11), 2)
            store, runtime, _, (terminal, _) = pipeline(
                physical_qubit_ids=(3, 11), topology_binding=binding, supported_gates=NATIVE_GATES,
            )
            text = program_json(2, [[{"type": "X", "targets": [0]}],
                                    [{"type": "CNOT", "targets": [1], "controls": [0]}]])
            if not accepted:
                with self.assertRaisesRegex(ProgramRequestError, "topology routing"):
                    submit(terminal, text)
                self.assertEqual(runtime.scheduler.queued_job_ids(), ())
                self.assertEqual(store.read().revision, 0)
                self.assertEqual(store.events(), ())
            else:
                job = submit(terminal, text)
                runtime.run_next()
                self.assertEqual(terminal.execute(f"job result {job.job_id}").result["counts"], {"11": 1})
                # Execution rechecks the topology if trusted assembly changes binding.
                job = submit(terminal, text, request_id="recheck")
                empty = replace(topology, gate_edges=())
                with patch.object(store, "_region_binding", RetainedRegionBinding(empty, (3, 11), 2)):
                    before = store.read()
                    self.assertEqual(runtime.run_next().status.value, "internal_error")
                    self.assertIs(store.read(), before)

    def test_multioperand_gate_is_not_implicitly_routed_over_a_chain(self):
        topology = QpuTopology("qpu-1", "chain", (3, 11, 99), TopologyLimits(3, 4, 4),
                               gate_edges=((3, 11), (11, 99)))
        topology.validate_direct_operands((3, 11))
        with self.assertRaisesRegex(ValueError, "routing"):
            topology.validate_direct_operands((3, 11, 99))
        replace(topology, gate_edges=((3, 11), (11, 99), (3, 99))).validate_direct_operands((3, 11, 99))

    def test_fixed_region_isolation_is_rejected_at_assembly(self):
        topology = QpuTopology("qpu-1", "v1", (3, 11), TopologyLimits(2, 4, 4))
        retained = RetainedRegionBinding(topology, (3,), 1)
        resources = ResetResourceBinding(PhysicalQubitRef("qpu-1", 3), PhysicalQubitRef("qpu-1", 11))
        with self.assertRaisesRegex(ValueError, "fixed retained region"):
            pipeline(physical_qubit_ids=(3,), topology_binding=retained, resource_binding=resources,
                     placement_policy="isolated_physical_resource")

    def test_common_terminal_executes_a_local_register_on_a_300_qubit_inventory(self):
        topology = large_topology()
        binding = RetainedRegionBinding(topology, (207, 7, 17), 4)
        store, runtime, _, (attacker, victim) = pipeline(
            physical_qubit_ids=binding.physical_qubit_ids, topology_binding=binding,
        )
        for terminal in (attacker, victim):
            job = submit(terminal, program_json(2, [[{"type": "X", "targets": [0]}]]))
            runtime.run_next()
            self.assertEqual(terminal.execute(f"job result {job.job_id}").result["counts"], {"10": 1})
        self.assertEqual(store.read().physical_qubit_ids, (207, 7, 17))
        self.assertEqual(len(store.read().density_matrix), 8)
        self.assertIs(store.region_binding.topology, topology)
        self.assertGreater(len(topology.gate_edges), 500)

    def test_large_hybrid_topology_roundtrips_without_dense_state_or_hop_cutoff(self):
        topology = large_topology()
        self.assertEqual(len(topology.physical_qubit_ids), 300)
        self.assertGreater(len(topology.gate_edges), 500)
        self.assertEqual(QpuTopology.from_config(topology.to_config(), limits=topology.limits), topology)
        self.assertEqual(topology.control_transfers[0].target_qubit_id, 2997)
        self.assertEqual(topology.readout_groups[0].member_qubit_ids[-1], 2997)
        # Being connected for a gate does not invent a control-transfer link.
        self.assertEqual(len(topology.control_transfers), 1)
        self.assertFalse(hasattr(topology, "density_matrix"))

    def test_metadata_inputs_and_export_are_detached(self):
        ids = [1, 5, 9]
        edges = [[1, 5]]
        topology = QpuTopology("qpu-1", "v1", ids, TopologyLimits(3, 3, 3), gate_edges=edges)
        ids[0] = 99
        edges[0][0] = 99
        output = topology.to_config()
        output["physical_qubit_ids"][0] = 123
        output["gate_edges"][0][0] = 123
        self.assertEqual(topology.physical_qubit_ids, (1, 5, 9))
        self.assertEqual(topology.gate_edges, ((1, 5),))

    def test_malformed_endpoints_duplicates_and_caps_fail_closed(self):
        topology = large_topology()
        invalid = (
            {"physical_qubit_ids": (True, 7)},
            {"gate_edges": ((7, 17), (17, 7))},
            {"residual_zz_edges": ((7, 8),)},
            {"control_transfers": (ControlTransferLink("missing-line", 7),)},
            {"control_transfers": (ControlTransferLink("drive-main", 7),) * 2},
            {"readout_groups": (ReadoutGroup("group", (7, 8)),)},
            {"limits": TopologyLimits(299, 1200, 600)},
            {"limits": TopologyLimits(512, 4, 600)},
            {"limits": TopologyLimits(512, 1200, 4)},
        )
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(topology, **change)
        for value in (True, 0, -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                TopologyLimits(value, 10, 10)

    def test_config_parser_rejects_unknown_fields_and_unbounded_iterables(self):
        topology = large_topology()
        raw = topology.to_config()
        raw["pretend_all_to_all"] = True
        with self.assertRaises(ValueError):
            QpuTopology.from_config(raw, limits=topology.limits)
        raw = topology.to_config()
        raw["control_lines"] = iter(raw["control_lines"])
        with self.assertRaises(ValueError):
            QpuTopology.from_config(raw, limits=topology.limits)
        with self.assertRaises(ValueError):
            QpuTopology("qpu-1", "v1", iter((7, 17)), TopologyLimits(3, 3, 3))

    def test_local_state_and_layout_keep_physical_order_and_spectators(self):
        topology = large_topology()
        binding = RetainedRegionBinding(topology, (207, 7, 17), 4)
        layout = PhysicalQubitLayout("qpu-1", (17, 7))
        binding.validate_layout(layout)
        snapshot = local_zero_snapshot(qpu_id="qpu-1", physical_qubit_ids=binding.physical_qubit_ids,
                                       device_config_version="calibration-v1")
        store = QpuStateStore(snapshot, region_binding=binding)
        self.assertIs(store.region_binding, binding)
        self.assertEqual(len(store.read().density_matrix), 8)
        self.assertEqual(len(store.region_binding.topology.physical_qubit_ids), 300)
        with self.assertRaises(ValueError):
            binding.validate_layout(PhysicalQubitLayout("qpu-1", (2997,)))
        with self.assertRaises(ValueError):
            QpuStateStore(replace(snapshot, physical_qubit_ids=(7, 207, 17)), region_binding=binding)

    def test_assignment_cannot_relabel_bound_state_or_partially_commit(self):
        topology = large_topology()
        binding = RetainedRegionBinding(topology, (7, 17), 2)
        initial = local_zero_snapshot(qpu_id="qpu-1", physical_qubit_ids=(7, 17), device_config_version="v1")
        store = QpuStateStore(initial, region_binding=binding)
        event = QpuStateTransition("move", "isolated_resource_assignment", "job1", initial.density_matrix,
                                   0.0, None, next_physical_qubit_ids=(7, 2997))
        with self.assertRaises(ValueError):
            store.commit(0, event)
        self.assertIs(store.read(), initial)
        self.assertEqual(store.events(), ())
        store.commit(0, replace(event, event_id="normal", event_kind="program_shot", next_physical_qubit_ids=None))
        self.assertEqual(store.read().revision, 1)

    def test_region_caps_and_foreign_qpu_are_not_program_width(self):
        topology = large_topology()
        region = RetainedRegionBinding(topology, topology.physical_qubit_ids[:9], 9)
        region.validate_layout(PhysicalQubitLayout("qpu-1", topology.physical_qubit_ids[:8]))
        with self.assertRaises(ValueError):
            RetainedRegionBinding(topology, topology.physical_qubit_ids[:9], 8)
        with self.assertRaises(ValueError):
            region.validate_layout(PhysicalQubitLayout("other-qpu", (7,)))


if __name__ == "__main__":
    unittest.main()
