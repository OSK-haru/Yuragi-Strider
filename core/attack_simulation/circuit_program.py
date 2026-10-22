"""Bounded local circuit IR, independent of actor roles and physical placement."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import TYPE_CHECKING

from core.attack_simulation.execution_regime import ExecutionRegime
from core.attack_simulation.program_request import (
    MAX_EDITOR_LOGICAL_QUBITS, PROGRAM_INITIALIZATIONS, ProgramRequestError, ProgramSubmitRequest,
    interleave_operation_id,
)
from core.attack_simulation.resource_binding import PhysicalQubitLayout
from core.circuit_model import CircuitConfig, GateOperation
from core.attack_simulation.native_pulses import NativeStep, UnsupportedNativeSequence, solve_native_steps
from core.attack_simulation.backend_declaration import BackendDeclaration, KINGSTON_BACKEND
from core.gates import Matrix, gate_duration_us, gate_unitary
from core.gate_specs import PROGRAM_GATE_NAMES

if TYPE_CHECKING:
    from core.attack_simulation.scenario_compiler import ResolvedScenarioManifest


NATIVE_GATES = PROGRAM_GATE_NAMES
CIRCUIT_INSTRUCTION_KINDS = frozenset({"initialize", "reset", "unitary", "measure", "delay"})


MAX_DELAY_US = 10_000.0


def _delay_us(gate: GateOperation) -> float:
    """The declared wait of an identity gate; no duration means no wait."""
    value = (gate.params or {}).get("duration_us", 0.0)
    if (type(value) not in (int, float) or not math.isfinite(float(value))
            or not 0.0 <= float(value) <= MAX_DELAY_US):
        raise ProgramRequestError(f"a delay must be between 0 and {MAX_DELAY_US:g} us")
    return float(value)


@dataclass(frozen=True)
class CircuitInstruction:
    kind: str
    duration_us: float
    unitary: Matrix | None = None
    qubits: tuple[int, ...] = (0,)
    classical_targets: tuple[int, ...] = ()
    time_group: int = 0
    depolarizing_probability: float = 0.0
    # A declared wait (delay instruction): the qubits idle for this long. ``duration_us`` is the
    # accounted time under the layer rule, so it can differ from the wait itself.
    wait_us: float = 0.0
    # The gate's own duration, whatever the layer rule charged to this instruction: a layer's first
    # instruction carries the layer's length and the others carry 0, so ``duration_us`` is accounting.
    intrinsic_us: float = 0.0
    # The native steps (virtual Z and pulses) of a calibrated single-qubit gate, in time order.
    natives: tuple[NativeStep, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in CIRCUIT_INSTRUCTION_KINDS:
            raise ValueError("unknown circuit instruction")
        if not math.isfinite(self.duration_us) or self.duration_us < 0:
            raise ValueError("invalid instruction duration")
        probability = float(self.depolarizing_probability)
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise ValueError("depolarizing probability must be between zero and one")
        if self.kind != "unitary" and probability != 0.0:
            raise ValueError("only unitary instructions may carry a gate error channel")
        wait = float(self.wait_us)
        if not math.isfinite(wait) or wait < 0.0 or (self.kind != "delay" and wait != 0.0):
            raise ValueError("only delay instructions may carry a finite nonnegative wait")
        object.__setattr__(self, "wait_us", wait)
        intrinsic = float(self.intrinsic_us)
        if not math.isfinite(intrinsic) or intrinsic < 0.0:
            raise ValueError("intrinsic duration must be finite and nonnegative")
        object.__setattr__(self, "intrinsic_us", intrinsic)
        natives = tuple(self.natives)
        if natives and (self.kind != "unitary" or len(self.qubits) != 1
                        or any(type(step) is not NativeStep for step in natives)):
            raise ValueError("only single-qubit unitary instructions carry native steps")
        object.__setattr__(self, "natives", natives)
        object.__setattr__(self, "depolarizing_probability", probability)
        qubits = tuple(self.qubits)
        classical = tuple(self.classical_targets)
        if (not qubits or len(set(qubits)) != len(qubits)
                or any(type(q) is not int or q < 0 for q in qubits)):
            raise ValueError("instruction qubits must be unique non-negative indices")
        if type(self.time_group) is not int:
            raise ValueError("time_group must be an integer")
        if self.kind == "unitary":
            dimension = 1 << len(qubits)
            if (self.unitary is None or len(self.unitary) != dimension
                    or any(len(row) != dimension for row in self.unitary)):
                raise ValueError("unitary dimension must match its operands")
            matrix = tuple(tuple(complex(value) for value in row) for row in self.unitary)
            if any(not math.isfinite(v.real) or not math.isfinite(v.imag) for row in matrix for v in row):
                raise ValueError("unitary must be finite")
            object.__setattr__(self, "unitary", matrix)
        elif self.unitary is not None:
            raise ValueError("only unitary instructions have matrices")
        if self.kind == "measure":
            if (len(classical) != len(qubits) or len(set(classical)) != len(classical)
                    or any(type(c) is not int or c < 0 for c in classical)):
                raise ValueError("measurement needs one unique classical target per qubit")
        elif classical:
            raise ValueError("only measurements write classical bits")
        object.__setattr__(self, "qubits", qubits)
        object.__setattr__(self, "classical_targets", classical)


@dataclass(frozen=True)
class InterleavedVictim:
    """Server-owned victim run that precedes every shot of an interleaved Program.

    The attacker only names a public reference; the victim circuit, its tenant,
    its layout and any secret input come from the frozen scenario. ``reference``
    is ``victim`` for the scenario's real victim and ``training:0``/``training:1``
    for a known-label calibration victim.
    """

    reference: str
    tenant_id: str
    operation: "CircuitExecutionOperation"
    layout: PhysicalQubitLayout

    def __post_init__(self) -> None:
        interleave_operation_id(self.reference)
        if not isinstance(self.tenant_id, str) or not self.tenant_id.strip():
            raise ValueError("interleaved victim tenant is required")
        if (type(self.operation) is not CircuitExecutionOperation or self.operation.shots != 1
                or self.operation.interleave is not None or self.operation.initialization != "prepare"):
            raise ValueError("interleaved victim must be one prepared, non-interleaved shot")
        if not isinstance(self.layout, PhysicalQubitLayout):
            raise TypeError("interleaved victim layout is required")


@dataclass(frozen=True)
class CircuitExecutionOperation:
    instructions: tuple[CircuitInstruction, ...]
    shots: int
    sampling_seed: int
    logical_qubits: int = 1
    classical_bits: int = 1
    initialization: str = "prepare"
    interleave: InterleavedVictim | None = None

    def __post_init__(self) -> None:
        if self.interleave is not None:
            if type(self.interleave) is not InterleavedVictim:
                raise TypeError("interleave must be an InterleavedVictim")
            if self.initialization != "inherit":
                raise ValueError("interleaved programs must inherit the carried-over state")
        if not isinstance(self.initialization, str) or self.initialization not in PROGRAM_INITIALIZATIONS:
            raise ValueError("initialization must be prepare or inherit")
        if type(self.logical_qubits) is not int or not 1 <= self.logical_qubits <= MAX_EDITOR_LOGICAL_QUBITS:
            raise ValueError("logical qubits exceed circuit input support")
        if type(self.classical_bits) is not int or not 1 <= self.classical_bits <= 32:
            raise ValueError("invalid classical register width")
        instructions = tuple(self.instructions)
        if (not instructions or any(not isinstance(item, CircuitInstruction) for item in instructions)
                or instructions[-1].kind != "measure"):
            raise ValueError("sampling circuit needs final measurements")
        initializations = tuple(i for i in instructions if i.kind == "initialize")
        if self.initialization == "prepare":
            if (len(initializations) != 1 or instructions[0].kind != "initialize"
                    or instructions[0].qubits != tuple(range(self.logical_qubits))):
                raise ValueError("preparation must initialize the declared logical register once")
        elif initializations:
            raise ValueError("inherited circuits must not initialize the register")
        measuring = False
        measured: set[int] = set()
        written: set[int] = set()
        for item in instructions:
            if any(q >= self.logical_qubits for q in item.qubits):
                raise ValueError("instruction operand is outside the circuit")
            if item.kind == "measure":
                measuring = True
                if measured.intersection(item.qubits) or written.intersection(item.classical_targets):
                    raise ValueError("final measurement targets must not repeat")
                if any(c >= self.classical_bits for c in item.classical_targets):
                    raise ValueError("measurement writes outside the classical register")
                measured.update(item.qubits)
                written.update(item.classical_targets)
            elif measuring:
                raise ValueError("only final measurement blocks are supported")
        if type(self.shots) is not int or not 1 <= self.shots <= 4096:
            raise ValueError("compiled circuit shots exceed runtime support")
        if self.shots * len(instructions) > 65536:
            raise ValueError("compiled circuit instruction work exceeds runtime support")
        if type(self.sampling_seed) is not int or not 0 <= self.sampling_seed <= 2**32 - 1:
            raise ValueError("invalid internal sampling seed")
        object.__setattr__(self, "instructions", instructions)

    @property
    def catalog_operation_id(self) -> str:
        return "program.submit"


@dataclass(frozen=True)
class LocalCircuitCompiler:
    """Native gates on compact operand matrices; no full-register gate copies."""

    supported_gates: tuple[str, ...] = NATIVE_GATES
    max_shots: int = 4096
    max_instruction_executions: int = 16384
    max_program_qubits: int = MAX_EDITOR_LOGICAL_QUBITS
    max_unitary_elements: int = 262144
    supported_initializations: tuple[str, ...] = ("prepare",)
    gate_durations_us: Mapping[str, float] | None = None
    physical_qubit_ids: tuple[int, ...] = ()
    cz_gate_calibrations: Mapping[str, Mapping[str, float]] | None = None
    single_qubit_native_gates: Mapping[str, Mapping[str, Mapping[str, float]]] | None = None
    single_qubit_gate_decompositions: Mapping[str, tuple[str, ...]] | None = None
    additional_physical_qubit_ids: tuple[int, ...] = ()
    backend_declaration: BackendDeclaration = KINGSTON_BACKEND
    two_qubit_gate_calibrations: Mapping[str, Mapping[str, Mapping[str, float]]] | None = None

    @classmethod
    def from_manifest(cls, manifest: ResolvedScenarioManifest) -> LocalCircuitCompiler:
        if manifest.qpu_service_config.get("execution_regime") != ExecutionRegime.NISQ_PHYSICAL.value:
            raise ProgramRequestError("this circuit compiler requires nisq_physical")
        config = manifest.qpu_service_config.get("circuit_execution")
        required = {"compiler_id", "supported_gates", "max_shots", "max_instruction_executions", "max_program_qubits"}
        optional = {"max_unitary_elements", "supported_initializations", "gate_durations_us"}
        optional |= {"cz_gate_calibrations", "single_qubit_native_gates", "single_qubit_gate_decompositions",
                     "backend_declaration", "two_qubit_gate_calibrations"}
        if not isinstance(config, Mapping) or not required <= set(config) or set(config) - required - optional:
            raise ProgramRequestError("circuit_execution requires an explicit local service profile")
        if config["compiler_id"] != "local_sampling_v1":
            raise ProgramRequestError("circuit compiler is not supported")
        gates = config["supported_gates"]
        if not isinstance(gates, (tuple, list)) or any(not isinstance(g, str) for g in gates):
            raise ProgramRequestError("supported_gates must be a gate list")
        region = manifest.qpu_service_config.get("retained_region")
        physical_ids = region.get("physical_qubit_ids") if isinstance(region, Mapping) else ()
        alternate = manifest.qpu_service_config.get("dedicated_region")
        additional = alternate.get("physical_qubit_ids", ()) if isinstance(alternate, Mapping) else ()
        return cls(tuple(gates), config["max_shots"], config["max_instruction_executions"],
                   config["max_program_qubits"], config.get("max_unitary_elements", 262144),
                   config.get("supported_initializations", ("prepare",)),
                   config.get("gate_durations_us"), tuple(physical_ids or ()),
                   config.get("cz_gate_calibrations"), config.get("single_qubit_native_gates"),
                   config.get("single_qubit_gate_decompositions"), tuple(additional),
                   BackendDeclaration.from_config(config["backend_declaration"])
                   if "backend_declaration" in config else KINGSTON_BACKEND,
                   config.get("two_qubit_gate_calibrations"))

    def __post_init__(self) -> None:
        declaration = self.backend_declaration
        if not isinstance(declaration, BackendDeclaration):
            raise ValueError("invalid backend declaration")
        modes = self.supported_initializations
        if (not isinstance(modes, (tuple, list)) or not modes
                or any(not isinstance(mode, str) or mode not in PROGRAM_INITIALIZATIONS for mode in modes)
                or len(set(modes)) != len(modes)):
            raise ValueError("invalid compiler initialization capabilities")
        object.__setattr__(self, "supported_initializations", tuple(modes))
        gates = tuple(self.supported_gates)
        if not gates or len(set(gates)) != len(gates) or not set(gates) <= set(NATIVE_GATES) or "MEASURE" not in gates:
            raise ValueError("invalid compiler gate capabilities")
        for name, maximum in (("max_shots", 4096), ("max_instruction_executions", 65536),
                              ("max_program_qubits", MAX_EDITOR_LOGICAL_QUBITS), ("max_unitary_elements", 262144)):
            if type(getattr(self, name)) is not int or not 1 <= getattr(self, name) <= maximum:
                raise ValueError(f"{name} exceeds compiler support")
        object.__setattr__(self, "supported_gates", gates)
        physical_ids = tuple(self.physical_qubit_ids)
        if (physical_ids and (len(physical_ids) < self.max_program_qubits
                              or len(set(physical_ids)) != len(physical_ids)
                              or any(type(value) is not int or value < 0 for value in physical_ids))):
            raise ValueError("compiler physical allocation must cover its program limit")
        object.__setattr__(self, "physical_qubit_ids", physical_ids)
        additional = tuple(self.additional_physical_qubit_ids)
        if (len(set(additional)) != len(additional)
                or any(type(value) is not int or value < 0 for value in additional)
                or set(additional) & set(physical_ids)):
            raise ValueError("additional physical allocation must be disjoint")
        object.__setattr__(self, "additional_physical_qubit_ids", additional)
        calibrated_ids = physical_ids + additional
        if (self.single_qubit_native_gates is None) != (self.single_qubit_gate_decompositions is None):
            raise ValueError("single-qubit calibrations and decompositions must be declared together")
        if self.single_qubit_native_gates is not None:
            raw = self.single_qubit_native_gates
            decompositions = self.single_qubit_gate_decompositions
            if (not isinstance(raw, Mapping) or not physical_ids
                    or set(raw) != {str(qid) for qid in calibrated_ids}
                    or not isinstance(decompositions, Mapping) or not decompositions):
                raise ValueError("single-qubit calibration must cover the frozen physical allocation")
            frozen = {}
            for qid, entries in raw.items():
                if not isinstance(entries, Mapping) or set(entries) != set(declaration.native_single_qubit_gates):
                    raise ValueError("native single-qubit calibration does not match backend declaration")
                values = {}
                for native, entry in entries.items():
                    if not isinstance(entry, Mapping) or set(entry) != {"duration_us", "gate_error"}:
                        raise ValueError("invalid native single-qubit calibration entry")
                    duration, error = entry["duration_us"], entry["gate_error"]
                    if (type(duration) not in (int, float) or not math.isfinite(float(duration)) or duration < 0
                            or type(error) not in (int, float) or not math.isfinite(float(error))
                            or not 0.0 <= error <= 1.0
                            or native in declaration.virtual_single_qubit_gates and duration != 0.0
                            or native not in declaration.virtual_single_qubit_gates and error > 0.5):
                        raise ValueError("native single-qubit calibration cannot represent its replacement channel")
                    values[native] = MappingProxyType({"duration_us": float(duration), "gate_error": float(error)})
                frozen[qid] = MappingProxyType(values)
            sequences = {}
            for gate, sequence in decompositions.items():
                if (gate not in set(gates) & {"H", "X", "Y", "Z", "S", "T", "RX", "RY", "RZ"}
                        or not isinstance(sequence, (list, tuple)) or not 1 <= len(sequence) <= 16
                        or any(type(native) is not str or native not in declaration.native_single_qubit_gates
                               for native in sequence)):
                    raise ValueError("invalid declared single-qubit gate decomposition")
                sequences[gate] = tuple(sequence)
            for gate, sequence in sequences.items():
                theta = 0.371
                probe = GateOperation(gate, [0], params={"theta_rad": theta}
                                      if gate in {"RX", "RY", "RZ"} else {})
                try:
                    solve_native_steps(
                        gate_unitary(probe, 1), sequence,
                        {name: 1.0 for name in declaration.native_pulse_angles_rad},
                        virtual_gates=declaration.virtual_single_qubit_gates,
                        pulse_angles=declaration.native_pulse_angles_rad)
                except UnsupportedNativeSequence as error:
                    raise ValueError(f"declared decomposition for {gate} cannot be executed") from error
            logical_single_qubit_gates = set(gates) & {
                "H", "X", "Y", "Z", "S", "T", "RX", "RY", "RZ",
            }
            if self.gate_durations_us is not None and set(sequences) != logical_single_qubit_gates:
                raise ValueError("every calibrated logical single-qubit gate needs an executable decomposition")
            object.__setattr__(self, "single_qubit_native_gates", MappingProxyType(frozen))
            object.__setattr__(self, "single_qubit_gate_decompositions", MappingProxyType(sequences))
        declared_two = {name.upper() for name in declaration.two_qubit_gates}
        if ((self.cz_gate_calibrations is not None or self.two_qubit_gate_calibrations is not None)
                and (set(gates) & {"CZ", "ECR"}) - declared_two):
            raise ValueError("compiler exposes a native two-qubit gate not declared by backend")
        if ("CNOT" in gates and (self.two_qubit_gate_calibrations is not None
                                  or self.cz_gate_calibrations is not None)):
            single_decompositions = self.single_qubit_gate_decompositions or {}
            cz_path = ("cz" in declaration.two_qubit_gates and not declaration.two_qubit_gates["cz"]
                       and "H" in single_decompositions)
            ecr_path = ("ecr" in declaration.two_qubit_gates
                        and {"S", "X", "RX"} <= set(single_decompositions))
            if not (cz_path or ecr_path):
                raise ValueError("CNOT has no executable decomposition in the declared two-qubit basis")
        if self.gate_durations_us is not None:
            if not isinstance(self.gate_durations_us, Mapping):
                raise ValueError("gate_durations_us must be a gate-to-duration object")
            durations = dict(self.gate_durations_us)
            calibrated_gates = set(self.single_qubit_gate_decompositions or {})
            # A delay ("I") takes its time from the program, so it has no declared gate duration.
            if set(durations) != set(gates) - {"MEASURE", "I"} - calibrated_gates:
                raise ValueError("gate_durations_us must specify every supported unitary gate")
            for gate, duration in durations.items():
                if (type(gate) is not str or type(duration) not in (int, float)
                        or not math.isfinite(float(duration)) or duration < 0):
                    raise ValueError("gate durations must be finite nonnegative numbers")
                durations[gate] = float(duration)
            object.__setattr__(self, "gate_durations_us", MappingProxyType(durations))
        two_calibrations: dict[str, Mapping[str, Mapping[str, float]]] = {}
        if self.cz_gate_calibrations is not None:
            if "cz" not in declaration.two_qubit_gates:
                raise ValueError("CZ calibration is not declared by this backend")
            if not isinstance(self.cz_gate_calibrations, Mapping) or not physical_ids:
                raise ValueError("per-edge CZ calibration requires a frozen physical allocation")
            calibrations = {}
            for edge_id, entry in self.cz_gate_calibrations.items():
                if (type(edge_id) is not str or not isinstance(entry, Mapping)
                        or set(entry) != {"duration_us", "depolarizing_probability"}):
                    raise ValueError("invalid per-edge CZ calibration entry")
                try:
                    left, right = (int(part) for part in edge_id.split(":"))
                except (ValueError, TypeError):
                    raise ValueError("CZ edge IDs must use 'low:high' form") from None
                if left >= right or left not in calibrated_ids or right not in calibrated_ids:
                    raise ValueError("CZ calibration edge is outside the retained physical region")
                duration, probability = entry["duration_us"], entry["depolarizing_probability"]
                if (type(duration) not in (int, float) or not math.isfinite(float(duration)) or duration < 0
                        or type(probability) not in (int, float) or not math.isfinite(float(probability))
                        or not 0.0 <= probability <= 1.0):
                    raise ValueError("CZ calibration values must be finite and bounded")
                calibrations[edge_id] = MappingProxyType({
                    "duration_us": float(duration),
                    "depolarizing_probability": float(probability),
                })
            object.__setattr__(self, "cz_gate_calibrations", MappingProxyType(calibrations))
            two_calibrations["cz"] = MappingProxyType(calibrations)
        if self.two_qubit_gate_calibrations is not None:
            if not isinstance(self.two_qubit_gate_calibrations, Mapping) or not calibrated_ids:
                raise ValueError("two-qubit calibrations require a frozen physical allocation")
            raw_two = self.two_qubit_gate_calibrations
            if set(raw_two) != set(declaration.two_qubit_gates):
                raise ValueError("two-qubit calibrations do not match backend declaration")
            two_calibrations = {}
            for gate, edge_map in raw_two.items():
                if not isinstance(edge_map, Mapping) or not edge_map:
                    raise ValueError("each declared two-qubit gate requires calibrated edges")
                directed = declaration.two_qubit_gates[gate]
                frozen_edges = {}
                for edge_id, entry in edge_map.items():
                    if (type(edge_id) is not str or not isinstance(entry, Mapping)
                            or set(entry) != {"duration_us", "depolarizing_probability"}):
                        raise ValueError("invalid declared two-qubit calibration entry")
                    try:
                        left, right = (int(part) for part in edge_id.split(":"))
                    except (ValueError, TypeError):
                        raise ValueError("two-qubit edge IDs must use 'left:right' form") from None
                    if (left == right or left not in calibrated_ids or right not in calibrated_ids
                            or not directed and left >= right):
                        raise ValueError("two-qubit calibration edge violates backend directionality")
                    duration, probability = entry["duration_us"], entry["depolarizing_probability"]
                    if (type(duration) not in (int, float) or not math.isfinite(float(duration)) or duration <= 0.0
                            or type(probability) not in (int, float) or not math.isfinite(float(probability))
                            or not 0.0 <= probability <= 1.0):
                        raise ValueError("two-qubit calibration values must be finite and bounded")
                    frozen_edges[edge_id] = MappingProxyType({
                        "duration_us": float(duration),
                        "depolarizing_probability": float(probability),
                    })
                two_calibrations[gate] = MappingProxyType(frozen_edges)
        if ((self.two_qubit_gate_calibrations is not None or self.cz_gate_calibrations is not None)
                and set(two_calibrations) != set(declaration.two_qubit_gates)):
            raise ValueError("two-qubit calibration inventory differs from backend declaration")
        object.__setattr__(self, "two_qubit_gate_calibrations", MappingProxyType(two_calibrations))

    def compile(self, request: ProgramSubmitRequest, *, sampling_seed: int,
                physical_qubit_ids: tuple[int, ...] | None = None) -> CircuitExecutionOperation:
        if request.initialization not in self.supported_initializations:
            raise ProgramRequestError("initialization is not supported by this service")
        if request.shots > self.max_shots:
            raise ProgramRequestError(f"shots exceed the service limit: {request.shots} > {self.max_shots}")
        circuit = CircuitConfig.from_dict(request.program.to_circuit_dict())
        if circuit.logical_qubits > self.max_program_qubits or circuit.classical_bits < 1:
            raise ProgramRequestError(
                f"circuit register exceeds service capabilities: {circuit.logical_qubits} qubits > "
                f"{self.max_program_qubits}" if circuit.logical_qubits > self.max_program_qubits
                else "circuit register exceeds service capabilities: no classical bit")
        allocation = self.physical_qubit_ids if physical_qubit_ids is None else tuple(physical_qubit_ids)
        if (self.physical_qubit_ids and (len(allocation) < circuit.logical_qubits
                or len(set(allocation)) != len(allocation)
                or not set(allocation) <= set(self.physical_qubit_ids + self.additional_physical_qubit_ids))):
            raise ProgramRequestError("compiler allocation is outside the frozen physical calibration")
        instructions = ([CircuitInstruction("initialize", 0.0, qubits=tuple(range(circuit.logical_qubits)), time_group=-3)]
                        if request.initialization == "prepare" else [])
        unitary_elements = 0

        def gate_calibration(gate: GateOperation) -> tuple[float, float]:
            if gate.type not in self.supported_gates or gate.conditions:
                raise ProgramRequestError(f"gate or classical condition is not supported by this service: {gate.type}")
            if self.single_qubit_gate_decompositions is not None and gate.type in self.single_qubit_gate_decompositions:
                if gate.controls or len(gate.targets) != 1:
                    raise ProgramRequestError("calibrated single-qubit gates require exactly one operand")
                physical_id = str(allocation[gate.targets[0]])
                natives = self.single_qubit_native_gates[physical_id]
                sequence = self.single_qubit_gate_decompositions[gate.type]
                duration = sum(natives[native]["duration_us"] for native in sequence
                               if native not in self.backend_declaration.virtual_single_qubit_gates)
                # Local complete-mixing channels commute with all one-qubit
                # unitaries. Their exact composition is another such channel;
                # no intermediate physical dynamics is modeled inside a gate.
                survival = math.prod(1.0 - 2.0 * natives[native]["gate_error"]
                                     for native in sequence
                                     if native not in self.backend_declaration.virtual_single_qubit_gates)
                return duration, 1.0 - survival
            if gate.type == "I":
                return _delay_us(gate), 0.0
            two_qubit_name = gate.type.lower()
            if two_qubit_name in {"cz", "ecr"}:
                if two_qubit_name not in self.backend_declaration.two_qubit_gates:
                    raise ProgramRequestError(f"{gate.type} is not declared by this backend")
                calibration_map = self.two_qubit_gate_calibrations.get(two_qubit_name, {})
                operands = tuple((gate.controls or []) + gate.targets)
                if len(operands) != 2:
                    raise ProgramRequestError(f"{gate.type} requires exactly two physical operands")
                physical = tuple(allocation[index] for index in operands)
                if not self.backend_declaration.two_qubit_gates[two_qubit_name]:
                    physical = tuple(sorted(physical))
                edge_id = f"{physical[0]}:{physical[1]}"
                calibration = calibration_map.get(edge_id)
                if calibration is not None:
                    return calibration["duration_us"], calibration["depolarizing_probability"]
                if calibration_map:
                    raise ProgramRequestError(f"{gate.type} edge has no eligible snapshot calibration")
                if self.gate_durations_us is not None and gate.type in self.gate_durations_us:
                    return self.gate_durations_us[gate.type], 0.0
                raise ProgramRequestError(f"{gate.type} has no declared calibration or fallback duration")
            if gate.type not in self.supported_gates:
                raise ProgramRequestError(f"gate is not supported by this service: {gate.type}")
            if self.gate_durations_us is None and gate.type not in {"I", "MEASURE"}:
                duration = gate_duration_us(gate)
                return duration, 0.0
            if self.gate_durations_us is not None and gate.type not in self.gate_durations_us:
                raise ProgramRequestError("gate has no declared service duration")
            duration = (self.gate_durations_us[gate.type]
                        if self.gate_durations_us is not None else gate_duration_us(gate))
            return duration, 0.0

        def append_gate(gate: GateOperation, duration: float, group: int,
                        depolarizing_probability: float = 0.0, intrinsic: float = 0.0) -> None:
            nonlocal unitary_elements
            if gate.type not in self.supported_gates or gate.conditions:
                raise ProgramRequestError(f"gate or classical condition is not supported by this service: {gate.type}")
            if gate.controls and gate.type not in {"CNOT", "CZ", "CP", "CCX"}:
                raise ProgramRequestError("this gate does not support control operands")
            allowed_params = {"I": {"duration_us"}, "RX": {"theta_rad"}, "RY": {"theta_rad"}, "RZ": {"theta_rad"},
                              "CP": {"theta_rad"}, "CNOT": {"control_value", "control_state"},
                              "ORACLE": {"marked_index"}}.get(gate.type, set())
            if not set(gate.params or {}) <= allowed_params:
                raise ProgramRequestError("gate parameters are not supported by this service")
            if gate.type == "MEASURE":
                if len(gate.classical_targets or ()) != len(gate.targets):
                    raise ProgramRequestError("sampling measurements need classical targets")
                instructions.append(CircuitInstruction("measure", duration, qubits=tuple(gate.targets),
                                                       classical_targets=tuple(gate.classical_targets or ()), time_group=group))
                return
            if gate.type == "I":
                # A delay: the qubit idles; nothing is applied, and no gate error is charged.
                instructions.append(CircuitInstruction(
                    "delay", duration, qubits=tuple(gate.targets), time_group=group,
                    wait_us=_delay_us(gate), intrinsic_us=_delay_us(gate)))
                return
            operands = tuple((gate.controls or []) + gate.targets)
            local_indices = {q: i for i, q in enumerate(operands)}
            unitary_elements += 4 ** len(operands)
            if unitary_elements > self.max_unitary_elements:
                raise ProgramRequestError("compiled unitary storage exceeds the service limit")
            local_gate = GateOperation(gate.type, [local_indices[q] for q in gate.targets],
                                       [local_indices[q] for q in gate.controls or []], gate.params)
            matrix = gate_unitary(local_gate, len(operands))
            natives: tuple[NativeStep, ...] = ()
            if (self.single_qubit_gate_decompositions is not None and not gate.controls
                    and len(gate.targets) == 1 and gate.type in self.single_qubit_gate_decompositions):
                steps = self.single_qubit_native_gates[str(allocation[gate.targets[0]])]
                natives = solve_native_steps(
                    matrix, self.single_qubit_gate_decompositions[gate.type],
                    {name: spec["duration_us"] for name, spec in steps.items()},
                    virtual_gates=self.backend_declaration.virtual_single_qubit_gates,
                    pulse_angles=self.backend_declaration.native_pulse_angles_rad)
            instructions.append(CircuitInstruction(
                "unitary", duration, matrix, operands, time_group=group,
                depolarizing_probability=depolarizing_probability, intrinsic_us=intrinsic, natives=natives,
            ))

        def expand_service_gate(gate: GateOperation) -> tuple[GateOperation, ...]:
            """Lower CNOT through a declared CZ or ECR basis with calibrated one-qubit gates."""
            if gate.type != "CNOT":
                return (gate,)
            if self.single_qubit_gate_decompositions is None:
                return (gate,)
            if gate.type not in self.supported_gates or gate.conditions:
                raise ProgramRequestError(f"gate or classical condition is not supported by this service: {gate.type}")
            if len(gate.controls or ()) != 1 or len(gate.targets) != 1:
                raise ProgramRequestError("this service supports CNOT with one control and one target")
            if gate.controls[0] == gate.targets[0]:
                raise ProgramRequestError("CNOT control and target must be different")
            if not set(gate.params or {}) <= {"control_value", "control_state"}:
                raise ProgramRequestError("CNOT parameters are not supported by this service")
            control_value = (gate.params or {}).get("control_value", 1.0)
            control_state = (gate.params or {}).get("control_state")
            if control_value not in {0.0, 1.0}:
                raise ProgramRequestError("CNOT control value must be zero or one")
            if control_state is not None and control_state not in {0.0, 1.0}:
                raise ProgramRequestError("single-control CNOT state must be zero or one")
            open_control = (control_state if control_state is not None else control_value) == 0.0
            control, target = gate.controls[0], gate.targets[0]
            before = [GateOperation("X", [control])] if open_control else []
            after = [GateOperation("X", [control])] if open_control else []
            if "cz" in self.two_qubit_gate_calibrations:
                decomposition = (GateOperation("H", [target]),
                                 GateOperation("CZ", [target], [control]),
                                 GateOperation("H", [target]))
            elif "ecr" in self.two_qubit_gate_calibrations:
                # For the standard echoed-cross-resonance unitary, CNOT =
                # ECR · (X·S on control ⊗ RX(pi/2) on target), up to global phase.
                decomposition = (GateOperation("S", [control]), GateOperation("X", [control]),
                                 GateOperation("RX", [target], params={"theta_rad": math.pi / 2.0}),
                                 GateOperation("ECR", [control, target]))
            else:
                return (gate,)
            return tuple((*before, *decomposition, *after))

        for group, kind, states in ((-2, "X", {"1", "-"}), (-1, "H", {"+", "-"})):
            prepared = ([q for q, state in enumerate(circuit.initial_states) if state in states]
                        if request.initialization == "prepare" else [])
            prepared_gates = [GateOperation(kind, [q]) for q in prepared]
            calibrations = [gate_calibration(gate) for gate in prepared_gates]
            duration = max((value[0] for value in calibrations), default=0.0)
            for index, (gate, (own, probability)) in enumerate(zip(prepared_gates, calibrations)):
                append_gate(gate, duration if index == 0 else 0.0, group, probability, own)
        for column in sorted(circuit.columns, key=lambda c: c.step):
            expanded = [expand_service_gate(gate) for gate in column.gates
                        if gate.type != "MEASURE"]
            layer_count = max((len(gates) for gates in expanded), default=0)
            for layer_index in range(layer_count):
                layer = [gates[layer_index] for gates in expanded if layer_index < len(gates)]
                calibrated = [(gate, *gate_calibration(gate)) for gate in layer]
                layer_duration = max((item[1] for item in calibrated), default=0.0)
                for index, (gate, own, error_probability) in enumerate(calibrated):
                    append_gate(gate, layer_duration if index == 0 else 0.0,
                                column.step, error_probability, own)
            for gate in column.gates:
                if gate.type == "MEASURE":
                    append_gate(gate, 0.0, column.step)
        if request.shots * len(instructions) > self.max_instruction_executions:
            raise ProgramRequestError(
                f"program exceeds the service instruction-work limit: {request.shots} shots x "
                f"{len(instructions)} instructions > {self.max_instruction_executions} "
                f"(this limit protects the host and is not a property of the simulated service; "
                f"this program allows at most {self.max_instruction_executions // len(instructions)} shots)")
        try:
            return CircuitExecutionOperation(tuple(instructions), request.shots, sampling_seed,
                                             circuit.logical_qubits, circuit.classical_bits, request.initialization)
        except ValueError as error:
            raise ProgramRequestError(str(error)) from error
