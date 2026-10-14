"""JSON-friendly simulation input and output models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import math
from typing import Any

from core.backend_boundary import PYTHON_DENSE_BACKEND
from core.capabilities import DEFAULT_SIMULATION_MODEL
from core.circuit_model import CircuitConfig
from core.errors import ValidationIssue
from core.evolution_methods import (
    FIXED_STEP_RK4,
    SUPPORTED_GATE_AWARE_EVOLUTION_METHODS,
)
from core.physical_environment import (
    INPUT_MODE_NORMALIZED,
    INPUT_MODE_PHYSICAL,
    NORMALIZED_ENVIRONMENT_MODEL,
    PHYSICAL_ENVIRONMENT_MODEL,
    UNIFIED_ENVIRONMENT_MODEL,
    input_mode_from_legacy_model,
    normalize_environment_model,
)
from core.state_snapshots import SnapshotOptions, StateSnapshot


@dataclass(init=False)
class EnvironmentConfig:
    """Environmental inputs for the MVP simulation model."""

    mode: str = "normalized"
    model: str = UNIFIED_ENVIRONMENT_MODEL
    input_mode: str = INPUT_MODE_NORMALIZED
    temperature: float = 0.02
    magnetic_field: float = 0.0
    noise_level: float = 0.01
    observation_strength: float | None = None
    observation_frequency: float | None = None
    device_quality: float = 0.5
    temperature_mk: float = 15.0
    flux_noise_phi0: float = 1e-6
    qubit_frequency_ghz: float = 5.0
    t1_max_us: float = 100.0
    tphi_max_us: float = 100.0
    per_qubit: list[dict[str, float]] = field(default_factory=list)
    ideal_reference: bool = False

    def __init__(
        self,
        mode: str = "normalized",
        model: str | None = None,
        input_mode: str | None = None,
        environment_model: str | None = None,
        temperature: float | None = None,
        magnetic_field: float | None = None,
        noise_level: float = 0.01,
        observation_strength: float | None = None,
        observation_frequency: float | None = None,
        temperature_kelvin: float | None = None,
        magnetic_field_tesla: float | None = None,
        device_quality: float = 0.5,
        temperature_mk: float = 15.0,
        flux_noise_phi0: float = 1e-6,
        qubit_frequency_ghz: float = 5.0,
        t1_max_us: float = 100.0,
        tphi_max_us: float = 100.0,
        per_qubit: list[dict[str, float]] | None = None,
        ideal_reference: bool = False,
    ) -> None:
        legacy_model = environment_model if environment_model is not None else model
        if input_mode is None:
            input_mode = input_mode_from_legacy_model(legacy_model)
        if temperature is None:
            temperature = 0.02 if temperature_kelvin is None else temperature_kelvin
        if magnetic_field is None:
            magnetic_field = (
                0.0
                if magnetic_field_tesla is None
                else magnetic_field_tesla
            )

        self.mode = mode
        self.model = normalize_environment_model(legacy_model)
        self.input_mode = input_mode
        self.temperature = temperature
        self.magnetic_field = magnetic_field
        self.noise_level = noise_level
        self.observation_strength = observation_strength
        self.observation_frequency = observation_frequency
        self.device_quality = device_quality
        self.temperature_mk = temperature_mk
        self.flux_noise_phi0 = flux_noise_phi0
        self.qubit_frequency_ghz = qubit_frequency_ghz
        self.t1_max_us = t1_max_us
        self.tphi_max_us = tphi_max_us
        self.per_qubit = list(per_qubit or [])
        self.ideal_reference = ideal_reference
        self.__post_init__()

    def __post_init__(self) -> None:
        self.mode = str(self.mode)
        if not self.mode:
            raise ValueError("mode must not be empty")
        self.model = normalize_environment_model(self.model)
        if not self.model:
            raise ValueError("model must not be empty")
        self.input_mode = str(self.input_mode)
        if not self.input_mode:
            raise ValueError("input_mode must not be empty")

        self.temperature = _float(self.temperature, "temperature")
        self.magnetic_field = _float(self.magnetic_field, "magnetic_field")
        self.noise_level = _float(self.noise_level, "noise_level")
        self.observation_strength = _optional_float(
            self.observation_strength,
            "observation_strength",
        )
        self.observation_frequency = _optional_float(
            self.observation_frequency,
            "observation_frequency",
        )
        self.device_quality = _float(self.device_quality, "device_quality")
        self.temperature_mk = _float(self.temperature_mk, "temperature_mk")
        self.flux_noise_phi0 = _float(self.flux_noise_phi0, "flux_noise_phi0")
        self.qubit_frequency_ghz = _float(
            self.qubit_frequency_ghz,
            "qubit_frequency_ghz",
        )
        self.t1_max_us = _float(self.t1_max_us, "t1_max_us")
        self.tphi_max_us = _float(self.tphi_max_us, "tphi_max_us")
        normalized_per_qubit: list[dict[str, float]] = []
        for index, entry in enumerate(self.per_qubit):
            entry = _require_mapping(entry, f"environment.per_qubit[{index}]")
            t1_max_us = _float(
                entry.get("t1_max_us", self.t1_max_us),
                f"environment.per_qubit[{index}].t1_max_us",
            )
            tphi_max_us = _float(
                entry.get("tphi_max_us", self.tphi_max_us),
                f"environment.per_qubit[{index}].tphi_max_us",
            )
            if t1_max_us <= 0.0 or tphi_max_us <= 0.0:
                raise ValueError("per-qubit T1 and Tphi maxima must be positive")
            normalized_per_qubit.append({
                "t1_max_us": t1_max_us,
                "tphi_max_us": tphi_max_us,
            })
        self.per_qubit = normalized_per_qubit
        self.ideal_reference = bool(self.ideal_reference)

    @property
    def temperature_kelvin(self) -> float:
        return self.temperature

    @property
    def magnetic_field_tesla(self) -> float:
        return self.magnetic_field

    @property
    def environment_model(self) -> str:
        return self.model

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "model": self.model,
            "input_mode": self.input_mode,
            "normalized": {
                "temperature_parameter": self.temperature,
                "magnetic_field_parameter": self.magnetic_field,
                "noise_level": self.noise_level,
            },
            "physical": {
                "device_quality": self.device_quality,
                "temperature_mk": self.temperature_mk,
                "flux_noise_phi0": self.flux_noise_phi0,
                "qubit_frequency_ghz": self.qubit_frequency_ghz,
                "t1_max_us": self.t1_max_us,
                "tphi_max_us": self.tphi_max_us,
                "per_qubit": [dict(entry) for entry in self.per_qubit],
                "ideal_reference": self.ideal_reference,
            },
            "observation_strength": self.observation_strength,
            "observation_frequency": self.observation_frequency,
            "temperature": self.temperature,
            "magnetic_field": self.magnetic_field,
            "noise_level": self.noise_level,
            "device_quality": self.device_quality,
            "temperature_mk": self.temperature_mk,
            "flux_noise_phi0": self.flux_noise_phi0,
            "qubit_frequency_ghz": self.qubit_frequency_ghz,
            "t1_max_us": self.t1_max_us,
            "tphi_max_us": self.tphi_max_us,
            "per_qubit": [dict(entry) for entry in self.per_qubit],
            "ideal_reference": self.ideal_reference,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EnvironmentConfig":
        data = _require_mapping(data, "environment config")
        normalized = data.get("normalized") or {}
        physical = data.get("physical") or {}
        if not isinstance(normalized, Mapping):
            raise TypeError("environment.normalized must be a mapping")
        if not isinstance(physical, Mapping):
            raise TypeError("environment.physical must be a mapping")
        legacy_model = data.get("environment_model", data.get("model"))
        input_mode = data.get("input_mode")
        if input_mode is None and legacy_model in {
            NORMALIZED_ENVIRONMENT_MODEL,
            PHYSICAL_ENVIRONMENT_MODEL,
        }:
            input_mode = input_mode_from_legacy_model(legacy_model)
        return cls(
            mode=data.get("mode", "normalized"),
            model=data.get("model"),
            input_mode=input_mode,
            environment_model=data.get("environment_model"),
            temperature=data.get(
                "temperature",
                data.get(
                    "temperature_kelvin",
                    normalized.get("temperature_parameter", 0.02),
                ),
            ),
            magnetic_field=data.get(
                "magnetic_field",
                data.get(
                    "magnetic_field_tesla",
                    normalized.get("magnetic_field_parameter", 0.0),
                ),
            ),
            noise_level=data.get(
                "noise_level",
                normalized.get("noise_level", 0.01),
            ),
            observation_strength=data.get("observation_strength"),
            observation_frequency=data.get("observation_frequency"),
            device_quality=data.get(
                "device_quality",
                physical.get("device_quality", 0.5),
            ),
            temperature_mk=data.get(
                "temperature_mk",
                physical.get("temperature_mk", 15.0),
            ),
            flux_noise_phi0=data.get(
                "flux_noise_phi0",
                physical.get("flux_noise_phi0", 1e-6),
            ),
            qubit_frequency_ghz=data.get(
                "qubit_frequency_ghz",
                physical.get("qubit_frequency_ghz", 5.0),
            ),
            t1_max_us=data.get(
                "t1_max_us",
                physical.get("t1_max_us", 100.0),
            ),
            tphi_max_us=data.get(
                "tphi_max_us",
                physical.get("tphi_max_us", 100.0),
            ),
            per_qubit=list(
                data.get("per_qubit", physical.get("per_qubit", [])) or []
            ),
            ideal_reference=data.get(
                "ideal_reference",
                physical.get("ideal_reference", False),
            ),
        )


@dataclass
class ReadoutErrorConfig:
    """Affine two-point readout model applied to observation probabilities.

    ``p10`` is P(observe 1 | prepare 0) and ``p01`` is P(observe 0 | prepare 1).
    Supply a single pair to share it across every qubit, or ``per_qubit`` to give
    each qubit its own calibrated pair. This is an observation-stage effect: it
    never touches the density matrix, so state metrics stay uncontaminated.
    """

    p10: float = 0.0
    p01: float = 0.0
    per_qubit: list[dict[str, float]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.p10 = _non_negative_float(self.p10, "readout_error.p10")
        self.p01 = _non_negative_float(self.p01, "readout_error.p01")
        normalized: list[dict[str, float]] = []
        for index, entry in enumerate(self.per_qubit):
            entry = _require_mapping(entry, f"readout_error.per_qubit[{index}]")
            normalized.append(
                {
                    "p10": _non_negative_float(
                        entry.get("p10", 0.0), f"readout_error.per_qubit[{index}].p10"
                    ),
                    "p01": _non_negative_float(
                        entry.get("p01", 0.0), f"readout_error.per_qubit[{index}].p01"
                    ),
                }
            )
        self.per_qubit = normalized
        for index, (p10, p01) in enumerate(self._pairs()):
            _validate_assignment_pair(p10, p01, f"readout_error qubit {index}")

    def _pairs(self) -> list[tuple[float, float]]:
        if self.per_qubit:
            return [(entry["p10"], entry["p01"]) for entry in self.per_qubit]
        return [(self.p10, self.p01)]

    @property
    def is_enabled(self) -> bool:
        return any(p10 > 0.0 or p01 > 0.0 for p10, p01 in self._pairs())

    def assignment_errors(self, n_qubits: int) -> list[tuple[float, float]]:
        """Expand the configuration to one (p10, p01) pair per qubit."""

        if not self.per_qubit:
            return [(self.p10, self.p01)] * n_qubits
        if len(self.per_qubit) != n_qubits:
            raise ValueError(
                "readout_error.per_qubit must provide one entry per qubit "
                f"({n_qubits}), got {len(self.per_qubit)}"
            )
        return [(entry["p10"], entry["p01"]) for entry in self.per_qubit]

    def to_dict(self) -> dict[str, Any]:
        return {
            "p10": self.p10,
            "p01": self.p01,
            "per_qubit": [dict(entry) for entry in self.per_qubit],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReadoutErrorConfig":
        data = _require_mapping(data, "readout_error")
        return cls(
            p10=data.get("p10", 0.0),
            p01=data.get("p01", 0.0),
            per_qubit=list(data.get("per_qubit") or []),
        )


def _validate_assignment_pair(p10: float, p01: float, label: str) -> None:
    if p10 > 1.0 or p01 > 1.0:
        raise ValueError(f"{label} assignment errors must lie in [0, 1]")
    if p10 + p01 >= 1.0:
        raise ValueError(
            f"{label} assignment span must be positive: p10 + p01 = {p10 + p01} >= 1"
        )


@dataclass
class SimulationConfig:
    """Complete configuration for a single simulation run."""

    circuit: CircuitConfig = field(default_factory=CircuitConfig.one_qubit_h)
    environment: EnvironmentConfig = field(default_factory=EnvironmentConfig)
    duration_us: float = 20.0
    time_steps: int = 101
    fidelity_threshold: float = 0.9
    model: str = DEFAULT_SIMULATION_MODEL
    simulation_backend: str = PYTHON_DENSE_BACKEND
    evolution_method: str = FIXED_STEP_RK4
    compilation_mode: str = "logical_direct"
    native_gate_durations_us: dict[str, float] = field(default_factory=dict)
    snapshot_options: SnapshotOptions | None = None
    measurement_shots: int = 1024
    measurement_seed: int = 0
    readout_error: ReadoutErrorConfig | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.circuit, CircuitConfig):
            self.circuit = CircuitConfig.from_dict(self.circuit)
        if not isinstance(self.environment, EnvironmentConfig):
            self.environment = EnvironmentConfig.from_dict(self.environment)
        if self.readout_error is not None and not isinstance(
            self.readout_error,
            ReadoutErrorConfig,
        ):
            self.readout_error = ReadoutErrorConfig.from_dict(self.readout_error)

        self.duration_us = _float(self.duration_us, "duration_us")
        self.time_steps = _int(self.time_steps, "time_steps")
        self.fidelity_threshold = _float(
            self.fidelity_threshold,
            "fidelity_threshold",
        )
        self.measurement_shots = _int(self.measurement_shots, "measurement_shots")
        self.measurement_seed = _int(self.measurement_seed, "measurement_seed")
        if not 1 <= self.measurement_shots <= 100_000:
            raise ValueError("measurement_shots must be between 1 and 100000")
        if not 0 <= self.measurement_seed <= 2 ** 32 - 1:
            raise ValueError("measurement_seed must be between 0 and 2**32 - 1")
        self.model = str(self.model)
        if not self.model:
            raise ValueError("model must not be empty")
        self.simulation_backend = str(self.simulation_backend)
        if not self.simulation_backend:
            raise ValueError("simulation_backend must not be empty")
        self.evolution_method = str(self.evolution_method)
        if self.evolution_method not in SUPPORTED_GATE_AWARE_EVOLUTION_METHODS:
            raise ValueError(
                "evolution_method must be one of "
                f"{SUPPORTED_GATE_AWARE_EVOLUTION_METHODS}"
            )
        from core.gate_compiler import SUPPORTED_COMPILATION_MODES

        self.compilation_mode = str(self.compilation_mode)
        if self.compilation_mode not in SUPPORTED_COMPILATION_MODES:
            raise ValueError(
                "compilation_mode must be one of "
                f"{sorted(SUPPORTED_COMPILATION_MODES)}"
            )
        self.native_gate_durations_us = {
            str(name).upper(): _non_negative_float(value, f"native duration {name}")
            for name, value in self.native_gate_durations_us.items()
        }
        if self.snapshot_options is not None and not isinstance(
            self.snapshot_options,
            SnapshotOptions,
        ):
            self.snapshot_options = SnapshotOptions.from_dict(self.snapshot_options)
        if self.snapshot_options is not None:
            for time_us in self.snapshot_options.custom_times_us:
                if time_us > self.duration_us + 1e-12:
                    raise ValueError(
                        "snapshot_options.custom_times_us values must not exceed duration_us"
                    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "circuit": self.circuit.to_dict(),
            "environment": self.environment.to_dict(),
            "duration_us": self.duration_us,
            "time_steps": self.time_steps,
            "fidelity_threshold": self.fidelity_threshold,
            "model": self.model,
            "simulation_backend": self.simulation_backend,
            "evolution_method": self.evolution_method,
            "compilation_mode": self.compilation_mode,
            "native_gate_durations_us": dict(self.native_gate_durations_us),
            "measurement_shots": self.measurement_shots,
            "measurement_seed": self.measurement_seed,
            "readout_error": (
                None if self.readout_error is None else self.readout_error.to_dict()
            ),
            "snapshot_options": (
                None
                if self.snapshot_options is None
                else self.snapshot_options.to_dict()
            ),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SimulationConfig":
        data = _require_mapping(data, "simulation config")
        return cls(
            circuit=CircuitConfig.from_dict(data["circuit"]),
            environment=EnvironmentConfig.from_dict(data["environment"]),
            duration_us=data.get("duration_us", 20.0),
            time_steps=data.get("time_steps", 101),
            fidelity_threshold=data.get("fidelity_threshold", 0.9),
            model=data.get("model", DEFAULT_SIMULATION_MODEL),
            simulation_backend=data.get("simulation_backend", PYTHON_DENSE_BACKEND),
            evolution_method=data.get("evolution_method", FIXED_STEP_RK4),
            compilation_mode=data.get("compilation_mode", "logical_direct"),
            native_gate_durations_us=dict(data.get("native_gate_durations_us") or {}),
            measurement_shots=data.get("measurement_shots", 1024),
            measurement_seed=data.get("measurement_seed", 0),
            readout_error=(
                None
                if data.get("readout_error") is None
                else ReadoutErrorConfig.from_dict(data["readout_error"])
            ),
            snapshot_options=(
                None
                if data.get("snapshot_options") is None
                else SnapshotOptions.from_dict(data["snapshot_options"])
            ),
        )


@dataclass
class SimulationResult:
    """Simulation output suitable for save/load, export, and inspection."""

    config: SimulationConfig
    times: list[float]
    fidelity: list[float]
    purity: list[float]
    effective_operation_time_us: float | None
    output_probabilities: dict[str, float] = field(default_factory=dict)
    measurement_counts: dict[str, int] = field(default_factory=dict)
    classical_branch_records: list[dict[str, Any]] = field(default_factory=list)
    classical_shot_preview: list[dict[str, Any]] = field(default_factory=list)
    derived_parameters: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)
    state_snapshots: list[StateSnapshot] = field(default_factory=list)
    physical_timeline: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.config, SimulationConfig):
            self.config = SimulationConfig.from_dict(self.config)

        self.times = [_float(time, "time") for time in self.times]
        self.fidelity = [_float(value, "fidelity") for value in self.fidelity]
        self.purity = [_float(value, "purity") for value in self.purity]

        if self.effective_operation_time_us is not None:
            self.effective_operation_time_us = _float(
                self.effective_operation_time_us,
                "effective_operation_time_us",
            )

        self.output_probabilities = {
            str(name): _float(value, f"output probability {name}")
            for name, value in self.output_probabilities.items()
        }
        self.measurement_counts = {
            str(name): _int(value, f"measurement count {name}")
            for name, value in self.measurement_counts.items()
        }
        self.classical_branch_records = [
            _json_value(record, f"classical branch {index}")
            for index, record in enumerate(self.classical_branch_records)
        ]
        self.classical_shot_preview = [
            _json_value(record, f"classical shot preview {index}")
            for index, record in enumerate(self.classical_shot_preview)
        ]
        self.derived_parameters = {
            str(name): _json_value(value, f"derived parameter {name}")
            for name, value in self.derived_parameters.items()
        }
        self.diagnostics = {
            str(name): _json_value(value, f"diagnostic {name}")
            for name, value in self.diagnostics.items()
        }
        self.warnings = [str(warning) for warning in self.warnings]
        self.issues = [
            issue
            if isinstance(issue, ValidationIssue)
            else ValidationIssue.from_dict(issue)
            for issue in self.issues
        ]
        self.state_snapshots = [
            snapshot
            if isinstance(snapshot, StateSnapshot)
            else StateSnapshot.from_dict(snapshot)
            for snapshot in self.state_snapshots
        ]
        self.physical_timeline = _json_value(
            self.physical_timeline,
            "physical timeline",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": self.config.to_dict(),
            "times": list(self.times),
            "fidelity": list(self.fidelity),
            "purity": list(self.purity),
            "effective_operation_time_us": self.effective_operation_time_us,
            "output_probabilities": dict(self.output_probabilities),
            "measurement_counts": dict(self.measurement_counts),
            "classical_branch_records": list(self.classical_branch_records),
            "classical_shot_preview": list(self.classical_shot_preview),
            "derived_parameters": dict(self.derived_parameters),
            "diagnostics": dict(self.diagnostics),
            "warnings": list(self.warnings),
            "issues": [issue.to_dict() for issue in self.issues],
            "state_snapshots": [
                snapshot.to_dict()
                for snapshot in self.state_snapshots
            ],
            "physical_timeline": dict(self.physical_timeline),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SimulationResult":
        data = _require_mapping(data, "simulation result")
        return cls(
            config=SimulationConfig.from_dict(data["config"]),
            times=list(data["times"]),
            fidelity=list(data["fidelity"]),
            purity=list(data["purity"]),
            effective_operation_time_us=data.get("effective_operation_time_us"),
            output_probabilities=dict(data.get("output_probabilities") or {}),
            measurement_counts=dict(data.get("measurement_counts") or {}),
            classical_branch_records=list(data.get("classical_branch_records") or []),
            classical_shot_preview=list(data.get("classical_shot_preview") or []),
            derived_parameters=dict(data.get("derived_parameters") or {}),
            diagnostics=dict(data.get("diagnostics") or {}),
            warnings=list(data.get("warnings") or []),
            issues=[
                issue
                if isinstance(issue, ValidationIssue)
                else ValidationIssue.from_dict(issue)
                for issue in data.get("issues", [])
            ],
            state_snapshots=[
                snapshot
                if isinstance(snapshot, StateSnapshot)
                else StateSnapshot.from_dict(snapshot)
                for snapshot in data.get("state_snapshots", [])
            ],
            physical_timeline=dict(data.get("physical_timeline") or {}),
        )


def _require_mapping(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise TypeError(f"{name} must be a mapping")
    return data


def _int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{name} must be an integer")
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{name} must be an integer") from exc
    return value


def _optional_float(value: Any, name: str) -> float | None:
    if value is None:
        return None
    return _float(value, name)


def _float(value: Any, name: str) -> float:
    try:
        converted = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{name} must be a number") from exc
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be finite")
    return converted


def _non_negative_float(value: Any, name: str) -> float:
    converted = _float(value, name)
    if converted < 0.0:
        raise ValueError(f"{name} must be non-negative")
    return converted


def _json_value(value: Any, name: str) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item, f"{name}.{key}")
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _json_value(item, f"{name}[{index}]")
            for index, item in enumerate(value)
        ]
    raise TypeError(f"{name} must be JSON-serializable")
