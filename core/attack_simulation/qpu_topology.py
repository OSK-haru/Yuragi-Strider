"""Bounded full-device metadata and its binding to a retained local register.

Relations declare possible paths, not active interactions or calibrated values.
No dense matrix, implicit all-to-all connectivity, or graph-distance cutoff is
constructed here. Gate routing and active-physics plans remain separate owners.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from core.attack_simulation.resource_binding import PhysicalQubitLayout


TOPOLOGY_SCHEMA_VERSION = "shared_qpu_topology_v1"


def _identifier(value: object, name: str) -> None:
    if type(value) is not str or not value.strip() or len(value) > 128:
        raise ValueError(f"{name} must be a bounded nonempty identifier")


def _qubits(value: object, name: str, *, maximum: int | None = None) -> tuple[int, ...]:
    if type(value) not in (tuple, list) or not value:
        raise ValueError(f"{name} must be a nonempty finite sequence")
    if maximum is not None and len(value) > maximum:
        raise ValueError(f"{name} exceeds its host metadata limit")
    if any(type(q) is not int or q < 0 for q in value) or len(set(value)) != len(value):
        raise ValueError(f"{name} must contain unique nonnegative integer IDs")
    return tuple(value)


@dataclass(frozen=True)
class TopologyLimits:
    max_qubits: int
    max_relations: int
    max_group_memberships: int

    def __post_init__(self) -> None:
        for name in ("max_qubits", "max_relations", "max_group_memberships"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError("topology host limits must be explicit positive integers")


@dataclass(frozen=True)
class ControlLine:
    control_id: str
    driven_qubit_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        _identifier(self.control_id, "control_id")
        object.__setattr__(self, "driven_qubit_ids", _qubits(self.driven_qubit_ids, "driven_qubit_ids"))


@dataclass(frozen=True)
class ControlTransferLink:
    source_control_id: str
    target_qubit_id: int

    def __post_init__(self) -> None:
        _identifier(self.source_control_id, "source_control_id")
        if type(self.target_qubit_id) is not int or self.target_qubit_id < 0:
            raise ValueError("target_qubit_id must be a nonnegative integer")


@dataclass(frozen=True)
class ReadoutGroup:
    group_id: str
    member_qubit_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        _identifier(self.group_id, "group_id")
        object.__setattr__(self, "member_qubit_ids", _qubits(self.member_qubit_ids, "member_qubit_ids"))


@dataclass(frozen=True)
class QpuTopology:
    qpu_id: str
    topology_version: str
    physical_qubit_ids: tuple[int, ...]
    limits: TopologyLimits
    gate_edges: tuple[tuple[int, int], ...] = ()
    residual_zz_edges: tuple[tuple[int, int], ...] = ()
    control_lines: tuple[ControlLine, ...] = ()
    control_transfers: tuple[ControlTransferLink, ...] = ()
    readout_groups: tuple[ReadoutGroup, ...] = ()
    _inventory: frozenset[int] = field(init=False, repr=False, compare=False)
    _gate_pairs: frozenset[tuple[int, int]] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _identifier(self.qpu_id, "qpu_id")
        _identifier(self.topology_version, "topology_version")
        if type(self.limits) is not TopologyLimits:
            raise TypeError("explicit TopologyLimits are required")
        ids = _qubits(self.physical_qubit_ids, "physical_qubit_ids", maximum=self.limits.max_qubits)
        inventory = frozenset(ids)
        object.__setattr__(self, "physical_qubit_ids", ids)
        object.__setattr__(self, "_inventory", inventory)
        relation_count = 0
        for name in ("gate_edges", "residual_zz_edges"):
            raw = getattr(self, name)
            if type(raw) not in (tuple, list) or len(raw) > self.limits.max_relations:
                raise ValueError("edge collection exceeds topology metadata support")
            edges: list[tuple[int, int]] = []
            seen: set[tuple[int, int]] = set()
            for item in raw:
                pair = _qubits(item, name, maximum=2)
                if len(pair) != 2 or not set(pair) <= inventory:
                    raise ValueError("edge endpoints must exist in the topology")
                edge = tuple(sorted(pair))
                if edge in seen:
                    raise ValueError("duplicate undirected topology edge")
                seen.add(edge)
                edges.append(edge)
            object.__setattr__(self, name, tuple(edges))
            relation_count += len(edges)
        object.__setattr__(self, "_gate_pairs", frozenset(self.gate_edges))
        for name, item_type in (("control_lines", ControlLine), ("control_transfers", ControlTransferLink),
                                ("readout_groups", ReadoutGroup)):
            raw = getattr(self, name)
            if (type(raw) not in (tuple, list) or len(raw) > self.limits.max_relations
                    or any(type(item) is not item_type for item in raw)):
                raise ValueError(f"invalid bounded {name}")
            object.__setattr__(self, name, tuple(raw))
            relation_count += len(raw)
        if relation_count > self.limits.max_relations:
            raise ValueError("total topology relations exceed the host metadata limit")
        controls = {line.control_id for line in self.control_lines}
        groups = {group.group_id for group in self.readout_groups}
        if len(controls) != len(self.control_lines) or len(groups) != len(self.readout_groups):
            raise ValueError("control and readout IDs must be unique within their namespace")
        memberships = sum(len(line.driven_qubit_ids) for line in self.control_lines)
        memberships += sum(len(group.member_qubit_ids) for group in self.readout_groups)
        if memberships > self.limits.max_group_memberships:
            raise ValueError("topology group memberships exceed the host metadata limit")
        if any(not set(line.driven_qubit_ids) <= inventory for line in self.control_lines):
            raise ValueError("control line refers to absent qubits")
        if any(not set(group.member_qubit_ids) <= inventory for group in self.readout_groups):
            raise ValueError("readout group refers to absent qubits")
        links = set()
        for link in self.control_transfers:
            if link.source_control_id not in controls or link.target_qubit_id not in inventory:
                raise ValueError("control transfer refers to an absent endpoint")
            key = link.source_control_id, link.target_qubit_id
            if key in links:
                raise ValueError("duplicate control transfer")
            links.add(key)

    def validate_layout(self, layout: PhysicalQubitLayout) -> None:
        if type(layout) is not PhysicalQubitLayout:
            raise TypeError("a server-owned PhysicalQubitLayout is required")
        if layout.qpu_id != self.qpu_id or not set(layout.physical_qubit_ids) <= self._inventory:
            raise ValueError("layout is outside the declared QPU topology")

    def validate_direct_operands(self, physical_qubit_ids: tuple[int, ...]) -> None:
        """Conservative unrouted adapter: every operand pair must be adjacent.

        This is a support restriction, not a claim that a clique makes a gate
        native. The service must also explicitly support the compiled gate.
        Decomposition/routing may later relax this restriction with evidence.
        """
        ids = _qubits(physical_qubit_ids, "gate operand IDs")
        if not set(ids) <= self._inventory:
            raise ValueError("gate operands are outside the declared topology")
        if any(tuple(sorted((a, b))) not in self._gate_pairs
               for index, a in enumerate(ids) for b in ids[index + 1:]):
            raise ValueError("gate operands require unsupported topology routing")

    def to_config(self) -> dict[str, object]:
        """Detached metadata; not automatically a participant-visible view."""
        return {
            "schema_version": TOPOLOGY_SCHEMA_VERSION,
            "qpu_id": self.qpu_id,
            "topology_version": self.topology_version,
            "physical_qubit_ids": list(self.physical_qubit_ids),
            "gate_edges": [list(edge) for edge in self.gate_edges],
            "residual_zz_edges": [list(edge) for edge in self.residual_zz_edges],
            "control_lines": [{"control_id": line.control_id, "driven_qubit_ids": list(line.driven_qubit_ids)}
                              for line in self.control_lines],
            "control_transfers": [{"source_control_id": link.source_control_id, "target_qubit_id": link.target_qubit_id}
                                  for link in self.control_transfers],
            "readout_groups": [{"group_id": group.group_id, "member_qubit_ids": list(group.member_qubit_ids)}
                               for group in self.readout_groups],
        }

    @classmethod
    def from_config(cls, config: Mapping[str, object], *, limits: TopologyLimits) -> "QpuTopology":
        required = {"schema_version", "qpu_id", "topology_version", "physical_qubit_ids", "gate_edges",
                    "residual_zz_edges", "control_lines", "control_transfers", "readout_groups"}
        if not isinstance(config, Mapping) or set(config) != required or config["schema_version"] != TOPOLOGY_SCHEMA_VERSION:
            raise ValueError("unsupported or incomplete topology schema")
        if type(limits) is not TopologyLimits:
            raise TypeError("explicit TopologyLimits are required")
        _qubits(config["physical_qubit_ids"], "physical_qubit_ids", maximum=limits.max_qubits)
        total_relations = 0
        for name in ("gate_edges", "residual_zz_edges", "control_lines", "control_transfers", "readout_groups"):
            raw = config[name]
            if type(raw) not in (tuple, list) or len(raw) > limits.max_relations:
                raise ValueError("topology relation sequence exceeds its host limit")
            total_relations += len(raw)
        if total_relations > limits.max_relations:
            raise ValueError("total topology relations exceed the host metadata limit")
        total_memberships = 0
        for name, member_key in (("control_lines", "driven_qubit_ids"), ("readout_groups", "member_qubit_ids")):
            for item in config[name]:
                if not isinstance(item, Mapping):
                    raise ValueError(f"invalid {name} schema")
                members = item.get(member_key)
                if type(members) not in (tuple, list):
                    raise ValueError("group members must be finite sequences")
                total_memberships += len(members)
                if total_memberships > limits.max_group_memberships:
                    raise ValueError("topology group memberships exceed the host metadata limit")
        parsed: dict[str, tuple] = {}
        for name, item_type, keys in (
            ("control_lines", ControlLine, {"control_id", "driven_qubit_ids"}),
            ("control_transfers", ControlTransferLink, {"source_control_id", "target_qubit_id"}),
            ("readout_groups", ReadoutGroup, {"group_id", "member_qubit_ids"}),
        ):
            raw = config[name]
            if type(raw) not in (tuple, list) or len(raw) > limits.max_relations:
                raise ValueError("topology relation sequence exceeds its host limit")
            if any(not isinstance(item, Mapping) or set(item) != keys for item in raw):
                raise ValueError(f"invalid {name} schema")
            parsed[name] = tuple(item_type(**dict(item)) for item in raw)
        return cls(config["qpu_id"], config["topology_version"], config["physical_qubit_ids"], limits,
                   config["gate_edges"], config["residual_zz_edges"], **parsed)


@dataclass(frozen=True)
class RetainedRegionBinding:
    """Bind a state tensor's ordered physical IDs, not the whole-device matrix."""

    topology: QpuTopology
    physical_qubit_ids: tuple[int, ...]
    max_local_qubits: int

    def __post_init__(self) -> None:
        if type(self.topology) is not QpuTopology:
            raise TypeError("an immutable QpuTopology is required")
        if type(self.max_local_qubits) is not int or self.max_local_qubits < 1:
            raise ValueError("an explicit positive local-region cap is required")
        ids = _qubits(self.physical_qubit_ids, "retained physical IDs", maximum=self.max_local_qubits)
        self.topology.validate_layout(PhysicalQubitLayout(self.topology.qpu_id, ids))
        object.__setattr__(self, "physical_qubit_ids", ids)

    def validate_layout(self, layout: PhysicalQubitLayout) -> None:
        self.topology.validate_layout(layout)
        if not set(layout.physical_qubit_ids) <= set(self.physical_qubit_ids):
            raise ValueError("layout requires state outside the retained local region")

    def validate_snapshot(self, snapshot: object) -> None:
        # Local import avoids making full-device metadata depend on dense state.
        from core.attack_simulation.qpu_state import QpuStateSnapshot
        if type(snapshot) is not QpuStateSnapshot:
            raise TypeError("an immutable QpuStateSnapshot is required")
        if snapshot.qpu_id != self.topology.qpu_id or snapshot.physical_qubit_ids != self.physical_qubit_ids:
            raise ValueError("state tensor differs from the retained topology binding")
