"""Circuit-file ingestion and a subject-neutral program submission envelope.

This boundary does not schedule or execute a circuit. A selected service must
still compile the program and enforce capabilities before accepting a job.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from typing import Any

from core.attack_simulation.config_limits import check_json_bounds
from core.circuit_model import CircuitConfig
from core.circuit_validation import validate_circuit_config


CIRCUIT_FILE_KIND = "quantscope_circuit_config"
CIRCUIT_FILE_VERSION = 1
MAX_CIRCUIT_FILE_BYTES = 64 * 1024  # Existing Circuit Studio import limit.
MAX_EDITOR_LOGICAL_QUBITS = 8
MAX_CIRCUIT_COLUMNS = 200
MAX_GATES_PER_COLUMN = 16
MAX_CIRCUIT_GATES = 800
MAX_CIRCUIT_ANNOTATIONS = 200
_DEVICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
PROGRAM_INITIALIZATIONS = ("prepare", "inherit")
STATE_INHERIT_OPERATION_ID = "program.state.inherit"
INTERLEAVE_VICTIM_OPERATION_ID = "program.interleave.victim"
INTERLEAVE_TRAINING_OPERATION_ID = "program.interleave.training"
_INTERLEAVE_REFERENCE = re.compile(r"(victim|training:[01])\Z")


def interleave_operation_id(reference: str) -> str:
    """Map a public interleave reference to the grant that controls it."""
    if not isinstance(reference, str) or not _INTERLEAVE_REFERENCE.fullmatch(reference):
        raise ProgramRequestError("interleave must be victim, training:0 or training:1")
    return (INTERLEAVE_VICTIM_OPERATION_ID if reference == "victim"
            else INTERLEAVE_TRAINING_OPERATION_ID)


class ProgramRequestError(ValueError):
    """The circuit document or submission envelope is invalid."""


@dataclass(frozen=True)
class CircuitProgram:
    """Immutable copy of an editor circuit, without visual-only annotations."""

    _canonical_json: str
    source_annotation_count: int

    @classmethod
    def from_editor_json(cls, text: str) -> "CircuitProgram":
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_CIRCUIT_FILE_BYTES:
            raise ProgramRequestError("circuit file exceeds the import byte limit")
        try:
            document = json.loads(
                text,
                parse_constant=_reject_nonfinite_constant,
            )
            check_json_bounds(document, "circuit file")
        except (ValueError, TypeError, RecursionError) as error:
            raise ProgramRequestError("invalid circuit JSON") from error
        if not isinstance(document, dict):
            raise ProgramRequestError("circuit file must contain an object")
        if any(key in document for key in ("version", "kind", "circuit_config")):
            if (
                type(document.get("version")) is not int
                or document["version"] != CIRCUIT_FILE_VERSION
                or document.get("kind") != CIRCUIT_FILE_KIND
                or set(document) != {"version", "kind", "circuit_config"}
            ):
                raise ProgramRequestError("unsupported circuit file wrapper")
            payload = document["circuit_config"]
        else:
            payload = document  # The existing editor importer also accepts raw configs.
        if not isinstance(payload, dict):
            raise ProgramRequestError("circuit_config must be an object")
        _check_editor_shape(payload)
        try:
            circuit = CircuitConfig.from_dict(payload)
            issues = validate_circuit_config(circuit)
        except (KeyError, TypeError, ValueError) as error:
            raise ProgramRequestError("invalid circuit configuration") from error
        if issues:
            raise ProgramRequestError(f"invalid circuit configuration: {issues[0].code}")
        annotations = len(circuit.annotations)
        executable = circuit.to_dict()
        executable["annotations"] = []  # UI labels are never QPU instructions.
        return cls(
            _canonical_json=json.dumps(
                executable, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False,
            ),
            source_annotation_count=annotations,
        )

    @property
    def logical_qubits(self) -> int:
        return int(self.to_circuit_dict()["logical_qubits"])

    def to_circuit_dict(self) -> dict[str, Any]:
        """Return a detached copy; callers cannot mutate the stored program."""

        return json.loads(self._canonical_json)


@dataclass(frozen=True)
class ProgramSubmitRequest:
    """Application-level envelope; identity comes from authentication context."""

    program: CircuitProgram
    device_id: str
    shots: int
    idempotency_key: str
    initialization: str = "prepare"
    interleave: str | None = None

    def __post_init__(self) -> None:
        if self.interleave is not None:
            interleave_operation_id(self.interleave)
            if self.initialization != "inherit":
                raise ProgramRequestError("interleaved runs must inherit the carried-over state")
        if not isinstance(self.program, CircuitProgram):
            raise TypeError("program must be a CircuitProgram")
        if not isinstance(self.initialization, str) or self.initialization not in PROGRAM_INITIALIZATIONS:
            raise ProgramRequestError("initialization must be prepare or inherit")
        if not isinstance(self.device_id, str) or not _DEVICE_ID.fullmatch(self.device_id):
            raise ProgramRequestError("invalid device ID")
        if type(self.shots) is not int or not 1 <= self.shots <= 2**31 - 1:
            raise ProgramRequestError("shots must be a positive bounded integer")
        if (
            not isinstance(self.idempotency_key, str)
            or not 1 <= len(self.idempotency_key.encode("utf-8")) <= 128
            or any(character.isspace() for character in self.idempotency_key)
        ):
            raise ProgramRequestError("invalid idempotency key")

    def to_application_payload(self) -> dict[str, Any]:
        """No principal, tenant, credential, physical placement, or editor labels."""

        payload = {
            "device_id": self.device_id,
            "shots": self.shots,
            "circuit_config": self.program.to_circuit_dict(),
        }
        if self.initialization != "prepare":
            payload["initialization"] = self.initialization
        if self.interleave is not None:
            payload["interleave"] = self.interleave
        return payload


def _check_editor_shape(payload: dict[str, Any]) -> None:
    allowed = {"logical_qubits", "initial_states", "classical_bits", "columns", "annotations"}
    if not set(payload).issubset(allowed):
        raise ProgramRequestError("unknown circuit configuration field")
    qubits = payload.get("logical_qubits")
    if type(qubits) is not int or not 1 <= qubits <= MAX_EDITOR_LOGICAL_QUBITS:
        raise ProgramRequestError("logical_qubits is outside the current editor range")
    states = payload.get("initial_states")
    if not isinstance(states, list) or len(states) != qubits or any(
        not ((type(item) is int and item in (0, 1)) or item in ("+", "-"))
        for item in states
    ):
        raise ProgramRequestError("invalid initial_states")
    bits = payload.get("classical_bits", 0)
    if type(bits) is not int or not 0 <= bits <= 32:
        raise ProgramRequestError("invalid classical_bits")
    columns = payload.get("columns")
    if not isinstance(columns, list) or not 1 <= len(columns) <= MAX_CIRCUIT_COLUMNS:
        raise ProgramRequestError("invalid circuit columns")
    steps: set[int] = set()
    total_gates = 0
    for column in columns:
        if not isinstance(column, dict) or set(column) != {"step", "gates"}:
            raise ProgramRequestError("invalid circuit column")
        step, gates = column["step"], column["gates"]
        if type(step) is not int or step < 0 or step in steps:
            raise ProgramRequestError("invalid or duplicate circuit step")
        steps.add(step)
        if not isinstance(gates, list) or len(gates) > MAX_GATES_PER_COLUMN:
            raise ProgramRequestError("invalid gate list")
        total_gates += len(gates)
        if total_gates > MAX_CIRCUIT_GATES:
            raise ProgramRequestError("too many circuit gates")
        for gate in gates:
            _check_gate_shape(gate)
    annotations = payload.get("annotations", [])
    if not isinstance(annotations, list) or len(annotations) > MAX_CIRCUIT_ANNOTATIONS:
        raise ProgramRequestError("invalid circuit annotations")
    for annotation in annotations:
        if not isinstance(annotation, dict) or not set(annotation).issubset({
            "kind", "id", "source_id", "column_index", "qubits",
        }):
            raise ProgramRequestError("invalid circuit annotation")
        if annotation.get("kind") not in ("MESSAGE", "RECEIVED"):
            raise ProgramRequestError("invalid circuit annotation kind")
        if type(annotation.get("column_index")) is not int or annotation["column_index"] not in steps:
            raise ProgramRequestError("invalid circuit annotation column")
        targets = annotation.get("qubits")
        if not isinstance(targets, list) or any(
            type(target) is not int or not 0 <= target < qubits for target in targets
        ):
            raise ProgramRequestError("invalid circuit annotation qubits")
        for key in ("id", "source_id"):
            if key in annotation and annotation[key] is not None and not isinstance(annotation[key], str):
                raise ProgramRequestError("invalid circuit annotation identifier")


def _check_gate_shape(gate: Any) -> None:
    allowed = {
        "type", "targets", "controls", "params", "classical_targets", "condition", "conditions",
    }
    if not isinstance(gate, dict) or not set(gate).issubset(allowed):
        raise ProgramRequestError("invalid gate fields")
    if not isinstance(gate.get("type"), str):
        raise ProgramRequestError("invalid gate type")
    for field in ("targets", "controls", "classical_targets"):
        value = gate.get(field, [] if field != "targets" else None)
        if not isinstance(value, list) or any(type(item) is not int for item in value):
            raise ProgramRequestError(f"invalid gate {field}")
    params = gate.get("params", {})
    if not isinstance(params, dict) or any(
        not isinstance(key, str)
        or type(value) not in (int, float)
        or not _is_finite_number(value)
        for key, value in params.items()
    ):
        raise ProgramRequestError("invalid gate params")
    for field in ("condition", "conditions"):
        value = gate.get(field)
        if value is None:
            continue
        items = value if field == "conditions" else [value]
        if not isinstance(items, list):
            raise ProgramRequestError(f"invalid gate {field}")
        for condition in items:
            if (
                not isinstance(condition, dict)
                or set(condition) != {"bit", "value"}
                or type(condition["bit"]) is not int
                or type(condition["value"]) is not int
                or condition["value"] not in (0, 1)
            ):
                raise ProgramRequestError(f"invalid gate {field}")


def _reject_nonfinite_constant(_: str) -> None:
    raise ValueError("nonfinite JSON number")


def _is_finite_number(value: int | float) -> bool:
    try:
        return math.isfinite(value)
    except OverflowError:
        return False
