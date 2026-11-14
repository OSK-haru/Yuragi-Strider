"""Read and write Yuragi-Strider simulation configs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.errors import ValidationIssue
from core.results import EnvironmentConfig, SimulationConfig
from core.validation import has_blocking_issues, validate_simulation_config


# 2.0: environment = measurable coherence times (D89).
# 3.0: Tphi at zero temperature plus the readout resonator (D90).
SCHEMA_VERSION = "3.0"
REMOVED_SCHEMA_VERSIONS = frozenset({"1.0", "1.1"})
REPLACED_SCHEMA_VERSIONS = frozenset({"2.0"})
CONFIG_KIND = "yuragi_strider.config"


class ConfigValidationError(ValueError):
    """Raised when a loaded config fails core validation."""

    def __init__(self, issues: list[ValidationIssue]) -> None:
        self.issues = issues
        details = "; ".join(f"{issue.code}: {issue.message}" for issue in issues)
        super().__init__(f"invalid Yuragi-Strider config: {details}")


def config_to_dict(
    config: SimulationConfig,
    metadata: dict[str, Any] | None = None,
    ui: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the .qscope.json envelope for a simulation config."""

    if not isinstance(config, SimulationConfig):
        config = SimulationConfig.from_dict(config)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": CONFIG_KIND,
        "metadata": dict(metadata or {}),
        "circuit": config.circuit.to_dict(),
        "environment": config.environment.to_dict(),
        "simulation": {
            "duration_us": config.duration_us,
            "time_steps": config.time_steps,
            "fidelity_threshold": config.fidelity_threshold,
            "model": config.model,
            "simulation_backend": config.simulation_backend,
        },
        "ui": dict(ui or {}),
    }


def config_to_json_text(
    config: SimulationConfig,
    metadata: dict[str, Any] | None = None,
    ui: dict[str, Any] | None = None,
) -> str:
    return json.dumps(
        config_to_dict(config, metadata=metadata, ui=ui),
        indent=2,
        sort_keys=True,
    )


def config_from_dict(data: dict[str, Any]) -> SimulationConfig:
    """Parse and validate a .qscope.json envelope."""

    if not isinstance(data, dict):
        raise TypeError("config data must be a dictionary")

    if data.get("kind") == CONFIG_KIND:
        if str(data.get("schema_version")) in REMOVED_SCHEMA_VERSIONS:
            raise ValueError(
                f"config schema {data.get('schema_version')} used the removed device-quality / flux-noise / "
                "normalized environment inputs; set environment.t1_zero_temperature_us and environment.tphi_zero_temperature_us "
                f"and save it again as schema {SCHEMA_VERSION}"
            )
        if str(data.get("schema_version")) in REPLACED_SCHEMA_VERSIONS:
            raise ValueError(
                f"config schema {data.get('schema_version')} gave tphi_us, the total pure-dephasing time at "
                "the working temperature; schema 3.0 takes environment.tphi_zero_temperature_us (without the "
                "thermal-photon dephasing, which is now derived) and the readout resonator "
                "(readout_resonator_frequency_ghz, readout_resonator_kappa_mhz, readout_resonator_chi_mhz)"
            )
        simulation = dict(data.get("simulation") or {})
        config = SimulationConfig(
            circuit=data["circuit"],
            environment=data.get("environment", EnvironmentConfig().to_dict()),
            duration_us=simulation.get("duration_us", 20.0),
            time_steps=simulation.get("time_steps", 101),
            fidelity_threshold=simulation.get("fidelity_threshold", 0.9),
            model=simulation.get("model", "weak_coupling_lindblad"),
            simulation_backend=simulation.get("simulation_backend", "python_dense"),
        )
    else:
        config = SimulationConfig.from_dict(data)

    issues = validate_simulation_config(config)
    if has_blocking_issues(issues):
        raise ConfigValidationError(issues)
    return config


def config_from_json_text(text: str) -> SimulationConfig:
    return config_from_dict(json.loads(text))


def save_config(
    config: SimulationConfig,
    path: str | Path,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Write a .qscope.json config file."""

    Path(path).write_text(
        config_to_json_text(config, metadata=metadata),
        encoding="utf-8",
    )


def load_config(path: str | Path) -> SimulationConfig:
    """Load and validate a .qscope.json config file."""

    return config_from_json_text(Path(path).read_text(encoding="utf-8"))
