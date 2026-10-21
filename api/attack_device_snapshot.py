"""Verified loader for checked-in device calibration snapshots (default: IBM Kingston)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import re
from types import MappingProxyType

from core.attack_simulation.backend_declaration import BackendDeclaration, KINGSTON_BACKEND


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_DIRECTORY = REPOSITORY_ROOT / "data" / "device_snapshots" / "ibm_kingston_2026-04-15"
DEFAULT_DEVICE_ID = "ibm_kingston"
_HASH_ROW = re.compile(r"^\| `((?:props|conf)_[a-z0-9]+\.json)` \|[^|]+\| `([0-9a-f]{64})` \|$", re.M)
_MAX_JSON_BYTES = 2_000_000


@dataclass(frozen=True)
class QubitCalibration:
    physical_qubit_id: int
    t1_us: float | None
    t2_us: float | None
    readout_error: float
    p_one_given_zero: float
    p_zero_given_one: float
    measurement_duration_us: float | None
    reset_duration_us: float | None


@dataclass(frozen=True)
class CzCalibration:
    physical_qubit_ids: tuple[int, int]
    error: float
    duration_us: float


@dataclass(frozen=True)
class TwoQubitGateCalibration:
    gate: str
    physical_qubit_ids: tuple[int, int]
    error: float
    duration_us: float


@dataclass(frozen=True)
class SingleQubitGateCalibration:
    gate: str
    physical_qubit_id: int
    error: float
    duration_us: float


@dataclass(frozen=True)
class DeviceSnapshot:
    device_id: str
    source_path: str  # repository-relative POSIX path of the SOURCE.md that vouches for the files
    snapshot_date: str
    n_qubits: int
    props_sha256: str
    conf_sha256: str
    directed_coupling_map: tuple[tuple[int, int], ...]
    undirected_coupling_edges: tuple[tuple[int, int], ...]
    qubits: Mapping[int, QubitCalibration]
    cz_calibrations: Mapping[tuple[int, int], CzCalibration]
    ineligible_qubit_reasons: Mapping[int, tuple[str, ...]]
    eligibility_policy: Mapping[str, object]
    single_qubit_calibrations: Mapping[tuple[str, int], SingleQubitGateCalibration]
    backend_declaration: BackendDeclaration
    two_qubit_calibrations: Mapping[str, Mapping[tuple[int, int], TwoQubitGateCalibration]] = field(default_factory=dict)

    @property
    def usable_qubit_ids(self) -> tuple[int, ...]:
        return tuple(qid for qid in range(self.n_qubits)
                     if qid not in self.ineligible_qubit_reasons)


def load_ibm_kingston_snapshot(directory: Path | str | None = None) -> DeviceSnapshot:
    """The default checked-in snapshot; other devices go through load_device_snapshot."""
    return load_device_snapshot(SNAPSHOT_DIRECTORY if directory is None else directory,
                                device_id=DEFAULT_DEVICE_ID, backend_declaration=KINGSTON_BACKEND)


def load_device_snapshot(directory: Path | str, *, device_id: str,
                         backend_declaration: BackendDeclaration) -> DeviceSnapshot:
    """Read immutable source files only after matching their SOURCE.md digests."""
    root = Path(directory)
    source_path = root / "SOURCE.md"
    source = _read_bounded(source_path, 16_384).decode("utf-8")
    # Hash rows are line-oriented metadata. Normalize SOURCE.md line endings
    # before matching so CRLF (or legacy CR) does not alter the parsed record.
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    hash_rows = _HASH_ROW.findall(source)
    expected = {name: digest for name, digest in hash_rows}
    props_name = [name for name in expected if name.startswith("props_")]
    conf_name = [name for name in expected if name.startswith("conf_")]
    if len(props_name) != 1 or len(conf_name) != 1:
        raise ValueError("snapshot SOURCE.md has no complete SHA-256 manifest")
    props_name, conf_name = props_name[0], conf_name[0]
    if len(hash_rows) != len(expected):
        raise ValueError("snapshot SOURCE.md contains duplicate SHA-256 entries")
    payloads = {}
    actual_hashes = {}
    for filename in expected:
        raw = _read_bounded(root / filename, _MAX_JSON_BYTES)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != expected[filename]:
            raise ValueError(f"snapshot digest mismatch: {filename}")
        actual_hashes[filename] = digest
        payloads[filename] = _json_object(raw)

    policy = _json_object(_read_bounded(root / "eligibility_policy.json", 16_384))
    policy = _validate_policy(policy)
    props, config = payloads[props_name], payloads[conf_name]
    if (props.get("backend_name") != device_id
            or config.get("backend_name") != device_id
            or type(config.get("n_qubits")) is not int
            or config["n_qubits"] != len(props.get("qubits", ()))
            or type(props.get("qubits")) is not list):
        raise ValueError("snapshot device identity or qubit count is inconsistent")
    n_qubits = config["n_qubits"]
    raw_couplings = config.get("coupling_map")
    if type(raw_couplings) is not list:
        raise ValueError("snapshot coupling map is missing")
    directed = []
    for edge in raw_couplings:
        if (type(edge) is not list or len(edge) != 2
                or any(type(value) is not int or not 0 <= value < n_qubits for value in edge)
                or edge[0] == edge[1]):
            raise ValueError("snapshot coupling map contains an invalid edge")
        directed.append(tuple(edge))
    if len(set(directed)) != len(directed):
        raise ValueError("snapshot coupling map contains duplicate directed edges")
    directed_set = set(directed)
    undirected = tuple(sorted({tuple(sorted(edge)) for edge in directed_set}))

    qubit_parameters = [_parameter_map(item) for item in props["qubits"]]
    raw_gates = props.get("gates")
    if type(raw_gates) is not list:
        raise ValueError("snapshot gate calibrations are missing")
    gate_records: dict[tuple[str, tuple[int, ...]], Mapping[str, float]] = {}
    two_records: dict[str, dict[tuple[int, int], tuple[float, float]]] = defaultdict(dict)
    for raw_gate in raw_gates:
        if not isinstance(raw_gate, Mapping):
            raise ValueError("snapshot contains an invalid gate record")
        gate = raw_gate.get("gate")
        qubits = raw_gate.get("qubits")
        if (type(gate) is not str or type(qubits) is not list
                or not qubits or any(type(value) is not int or not 0 <= value < n_qubits for value in qubits)):
            raise ValueError("snapshot gate identity is invalid")
        parameters = _parameter_map(raw_gate.get("parameters"))
        key = gate, tuple(qubits)
        if key in gate_records:
            raise ValueError("snapshot contains duplicate gate calibration records")
        gate_records[key] = parameters
        if gate in backend_declaration.two_qubit_gates:
            if len(qubits) != 2:
                raise ValueError(f"{gate.upper()} calibration must have two operands")
            edge = tuple(qubits)
            if edge not in directed_set:
                raise ValueError(f"{gate.upper()} calibration direction is absent from the coupling map")
            error = _required_number(parameters, "gate_error")
            length = _required_number(parameters, "gate_length")
            duration = _ns_to_us(length, parameters, "gate_length")
            if not 0.0 <= error <= 1.0 or duration <= 0.0:
                raise ValueError(f"{gate.upper()} calibration is outside its physical range")
            two_records[gate][edge] = (error, duration)

    two_qubit_calibrations: dict[str, Mapping[tuple[int, int], TwoQubitGateCalibration]] = {}
    for gate, directed_flag in backend_declaration.two_qubit_gates.items():
        records = two_records.get(gate, {})
        if not records:
            raise ValueError(f"snapshot has no calibration for declared {gate.upper()} gate")
        if not directed_flag and set(records) != directed_set:
            raise ValueError(f"{gate.upper()} calibrations do not match the directed coupling map")
        if directed_flag:
            if set(records) != directed_set:
                raise ValueError(f"{gate.upper()} calibrations do not match the directed coupling map")
            calibration = {edge: TwoQubitGateCalibration(gate, edge, *values)
                           for edge, values in records.items()}
        else:
            pairs = {tuple(sorted(edge)) for edge in records}
            calibration = {}
            for pair in pairs:
                forward, reverse = pair, (pair[1], pair[0])
                if forward not in records or reverse not in records or records[forward] != records[reverse]:
                    raise ValueError(f"opposite {gate.upper()} directions have inconsistent calibration")
                calibration[pair] = TwoQubitGateCalibration(gate, pair, *records[forward])
            if set(records) != {edge for left, right in pairs for edge in ((left, right), (right, left))}:
                raise ValueError(f"{gate.upper()} calibrations do not match complete coupling pairs")
        two_qubit_calibrations[gate] = MappingProxyType(calibration)
    if set(two_records) - set(backend_declaration.two_qubit_gates):
        raise ValueError("snapshot contains undeclared two-qubit gate calibrations")
    cz_calibrations = {
        pair: CzCalibration(pair, item.error, item.duration_us)
        for pair, item in two_qubit_calibrations.get("cz", {}).items()
    }

    single_qubit_calibrations = {}
    for (gate, operands), parameters in gate_records.items():
        if gate not in backend_declaration.native_single_qubit_gates:
            continue
        if len(operands) != 1:
            raise ValueError("native single-qubit calibration must have one operand")
        error = _required_number(parameters, "gate_error")
        duration = _gate_duration(parameters, gate)
        if gate in backend_declaration.virtual_single_qubit_gates and duration != 0.0:
            raise ValueError("virtual single-qubit calibrations must have zero duration")
        if error > 1.0 or (gate not in backend_declaration.virtual_single_qubit_gates and duration <= 0.0):
            raise ValueError("native single-qubit calibration is outside its physical range")
        qid = operands[0]
        single_qubit_calibrations[gate, qid] = SingleQubitGateCalibration(gate, qid, error, duration)
    expected_native = {(gate, qid) for gate in backend_declaration.native_single_qubit_gates
                       for qid in range(n_qubits)}
    if set(single_qubit_calibrations) != expected_native:
        raise ValueError("snapshot native calibrations do not match backend declaration")

    qubits = {}
    for qid, parameters in enumerate(qubit_parameters):
        readout_length = parameters.get("readout_length")
        measure = gate_records.get(("measure", (qid,)))
        reset = gate_records.get(("reset", (qid,)))
        qubits[qid] = QubitCalibration(
            qid,
            _optional_number(parameters, "T1"),
            _optional_number(parameters, "T2"),
            _required_number(parameters, "readout_error"),
            _required_number(parameters, "prob_meas1_prep0"),
            _required_number(parameters, "prob_meas0_prep1"),
            _gate_duration(measure, "measure") if measure is not None else _ns_value(readout_length),
            _gate_duration(reset, "reset") if reset is not None else None,
        )
        calibration = qubits[qid]
        if (calibration.readout_error > 1.0 or calibration.p_one_given_zero > 1.0
                or calibration.p_zero_given_one > 1.0
                or calibration.t1_us is not None and calibration.t1_us <= 0.0
                or calibration.t2_us is not None and calibration.t2_us <= 0.0):
            raise ValueError("snapshot qubit calibration is outside its physical range")
        if any(value is not None and value <= 0.0 for value in (
                calibration.measurement_duration_us, calibration.reset_duration_us)):
            raise ValueError("snapshot measurement/reset duration must be positive")

    reasons: dict[int, list[str]] = {qid: [] for qid in range(n_qubits)}
    readout_limit = policy["readout_error_exclude_above"]
    cz_limit = policy["cz_error_exclude_at_or_above"]
    for qid, calibration in qubits.items():
        if calibration.readout_error > readout_limit:
            reasons[qid].append("readout_error_above_threshold")
        if policy["exclude_missing_t1_or_t2"] and (calibration.t1_us is None or calibration.t2_us is None):
            reasons[qid].append("missing_t1_or_t2")
        if (policy["exclude_missing_measure_or_reset_duration"]
                and (calibration.measurement_duration_us is None or calibration.reset_duration_us is None)):
            reasons[qid].append("missing_measure_or_reset_duration")
    for edge, calibration in cz_calibrations.items():
        if calibration.error > 1.0 or calibration.duration_us <= 0.0:
            raise ValueError("snapshot CZ calibration is outside its physical range")
        if calibration.error >= cz_limit:
            for qid in edge:
                reasons[qid].append(f"attached_to_cz_error_at_or_above_threshold:{edge[0]}-{edge[1]}")
    frozen_reasons = MappingProxyType({qid: tuple(sorted(set(items)))
                                      for qid, items in reasons.items() if items})
    return DeviceSnapshot(
        device_id, _repository_relative(source_path), str(props.get("last_update_date")), n_qubits,
        actual_hashes[props_name], actual_hashes[conf_name],
        tuple(directed), undirected, MappingProxyType(qubits),
        MappingProxyType(cz_calibrations), frozen_reasons, MappingProxyType(policy),
        MappingProxyType(single_qubit_calibrations), backend_declaration,
        MappingProxyType(two_qubit_calibrations),
    )


def _repository_relative(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def deterministic_connected_region(
    snapshot: DeviceSnapshot, maximum: int, *, excluded_qubit_ids: frozenset[int] = frozenset(),
) -> tuple[int, ...]:
    """Choose a stable connected usable subregion outside any reserved region."""
    if type(maximum) is not int or maximum < 1:
        raise ValueError("region maximum must be a positive integer")
    if type(excluded_qubit_ids) is not frozenset or any(type(qid) is not int for qid in excluded_qubit_ids):
        raise ValueError("excluded qubits must be a frozen set of physical IDs")
    usable = set(snapshot.usable_qubit_ids) - excluded_qubit_ids
    neighbors: dict[int, set[int]] = {qid: set() for qid in usable}
    for left, right in snapshot.undirected_coupling_edges:
        if left in usable and right in usable:
            neighbors[left].add(right)
            neighbors[right].add(left)
    for start in sorted(usable):
        visited = {start}
        queue = [start]
        ordered = []
        while queue and len(ordered) < maximum:
            current = queue.pop(0)
            ordered.append(current)
            for neighbor in sorted(neighbors[current]):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        if len(ordered) == maximum:
            return tuple(ordered)
    if not usable:
        raise ValueError("snapshot has no usable qubit")
    # A smaller connected component is allowed when the device cannot fill the
    # configured dense local limit.
    return tuple(sorted(usable)[:maximum]) if maximum == 1 else _largest_component(neighbors, maximum)


def _largest_component(neighbors: Mapping[int, set[int]], maximum: int) -> tuple[int, ...]:
    seen: set[int] = set()
    components = []
    for start in sorted(neighbors):
        if start in seen:
            continue
        component = []
        queue = [start]
        seen.add(start)
        while queue:
            node = queue.pop(0)
            component.append(node)
            for neighbor in sorted(neighbors[node]):
                if neighbor not in seen:
                    seen.add(neighbor)
                    queue.append(neighbor)
        components.append(tuple(component))
    return min((item for item in components if len(item) >= maximum), key=lambda item: item[:maximum])[:maximum] if any(
        len(item) >= maximum for item in components) else max(components, key=len)


def _read_bounded(path: Path, maximum: int) -> bytes:
    if path.stat().st_size > maximum:
        raise ValueError("device snapshot input exceeds its fixed host bound")
    raw = path.read_bytes()
    if len(raw) > maximum:
        raise ValueError("device snapshot input exceeds its fixed host bound")
    return raw


def _json_object(raw: bytes) -> dict[str, object]:
    def no_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key in snapshot JSON")
            result[key] = value
        return result

    parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid number")))
    if not isinstance(parsed, dict):
        raise ValueError("snapshot JSON root must be an object")
    return parsed


def _validate_policy(raw: Mapping[str, object]) -> dict[str, object]:
    required = {
        "schema_version", "decision_ref", "readout_error_exclude_above",
        "cz_error_exclude_at_or_above", "exclude_missing_t1_or_t2",
        "exclude_missing_measure_or_reset_duration", "meaning",
    }
    if set(raw) != required or raw["schema_version"] != "device_eligibility_policy_v1":
        raise ValueError("unsupported device eligibility policy")
    for field in ("readout_error_exclude_above", "cz_error_exclude_at_or_above"):
        value = raw[field]
        if type(value) not in (int, float) or not 0 <= value <= 1:
            raise ValueError("eligibility thresholds must be recorded probabilities")
    for field in ("exclude_missing_t1_or_t2", "exclude_missing_measure_or_reset_duration"):
        if type(raw[field]) is not bool:
            raise ValueError("eligibility missing-data policies must be explicit booleans")
    if any(type(raw[field]) is not str or not raw[field] for field in ("decision_ref", "meaning")):
        raise ValueError("eligibility policy provenance must be explicit")
    return dict(raw)


def _parameter_map(raw: object) -> dict[str, dict[str, object]]:
    if type(raw) is not list:
        raise ValueError("snapshot parameter collection must be a list")
    result = {}
    for item in raw:
        if not isinstance(item, Mapping) or type(item.get("name")) is not str:
            raise ValueError("snapshot parameter entry is invalid")
        if item["name"] in result:
            raise ValueError("snapshot parameter name is duplicated")
        result[item["name"]] = dict(item)
    return result


def _optional_number(parameters: Mapping[str, Mapping[str, object]], name: str) -> float | None:
    item = parameters.get(name)
    if item is None:
        return None
    return _parameter_number(item)


def _required_number(parameters: Mapping[str, Mapping[str, object]], name: str) -> float:
    value = _optional_number(parameters, name)
    if value is None:
        raise ValueError(f"required snapshot calibration is missing: {name}")
    return value


def _parameter_number(item: Mapping[str, object]) -> float:
    value = item.get("value")
    if type(value) not in (int, float) or not float(value) >= 0 or not math.isfinite(float(value)):
        raise ValueError("snapshot calibration value must be finite and nonnegative")
    return float(value)


def _gate_duration(parameters: Mapping[str, Mapping[str, object]], name: str) -> float:
    return _ns_to_us(_required_number(parameters, "gate_length"), parameters, "gate_length")


def _ns_to_us(value: float, parameters: Mapping[str, Mapping[str, object]], name: str) -> float:
    if parameters[name].get("unit") != "ns":
        raise ValueError(f"snapshot {name} must use ns")
    return value / 1000.0


def _ns_value(item: Mapping[str, object] | None) -> float | None:
    if item is None:
        return None
    if item.get("unit") != "ns":
        raise ValueError("snapshot readout_length must use ns")
    return _parameter_number(item) / 1000.0
