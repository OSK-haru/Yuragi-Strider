"""Machine-readable C10 freeze audit for explicit CPTP evolution."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import math
from pathlib import Path
import platform
import subprocess
from typing import Any

from api.main import app
from api.pulse_models import (
    PulseSimulateRequest,
    QutritPulseSimulateRequest,
)
from api.pulse_qutrit_service import run_qutrit_pulse_request
from api.pulse_service import run_pulse_request
from core.cptp import (
    CHOI_CONVENTION_ID,
    DEFAULT_CP_TOLERANCE,
    DEFAULT_TP_TOLERANCE,
)
from core.cptp_evolution import EXPLICIT_CPTP_EVOLUTION_ID
from core.cptp_liouvillian import (
    LIOUVILLIAN_VECTORIZATION_ID,
    MATRIX_EXPONENTIAL_METHOD,
)
from core.cptp_piecewise import PIECEWISE_SAMPLING_ID
from core.cptp_rust import RUST_EXPONENTIAL_METHOD
from core.rust_dense_kernel import is_rust_kernel_available


ROOT = Path(__file__).resolve().parents[1]
CPTP_MODEL_FREEZE_ID = "yuragi_strider_explicit_cptp_v1"

CRITICAL_SOURCE_FILES = (
    "core/cptp.py",
    "core/cptp_qutrit.py",
    "core/cptp_liouvillian.py",
    "core/cptp_piecewise.py",
    "core/cptp_rust.py",
    "core/cptp_evolution.py",
    "core/pulse_open_system.py",
    "core/pulse_qutrit_open_system.py",
    "core/rust_dense_kernel.py",
    "rust_kernels/yuragi_strider_core/Cargo.toml",
    "rust_kernels/yuragi_strider_core/src/lib.rs",
    "rust_kernels/yuragi_strider_core/src/reset.rs",
    "rust_kernels/yuragi_strider_rust/Cargo.toml",
    "rust_kernels/yuragi_strider_rust/Cargo.lock",
    "rust_kernels/yuragi_strider_rust/src/lib.rs",
    "api/pulse_models.py",
    "api/pulse_service.py",
    "api/pulse_qutrit_service.py",
    "frontend/src/types/pulse.ts",
    "frontend/src/utils/pulseLab.ts",
    "frontend/src/components/PulseEnvironmentPanel.tsx",
    "frontend/src/pages/PulseLabPage.tsx",
    "tests/test_cptp_qubit_channels.py",
    "tests/test_cptp_qutrit_channels.py",
    "tests/test_cptp_choi_audit.py",
    "tests/test_cptp_composition.py",
    "tests/test_cptp_liouvillian.py",
    "tests/test_cptp_piecewise.py",
    "tests/test_cptp_rust_parity.py",
    "tests/test_cptp_rk4_comparison.py",
    "tests/test_pulse_cptp_api_integration.py",
    "tests/test_cptp_model_freeze.py",
    "validation_cptp/model_freeze.py",
    "scripts/freeze_cptp_model.py",
)

REQUIRED_EVIDENCE_FILES = (
    "validation_results/cptp_rk4_comparison.json",
    "docs_for_develop/validation/cptp-rk4-comparison.md",
    "docs_for_develop/development/physical-model-finalization/"
    "phase2-explicit-cptp-path.md",
    "docs_for_develop/physics/model_identity.md",
    "docs_for_develop/validation/cptp-model-freeze.md",
)


def build_freeze_report(root: Path = ROOT) -> dict[str, Any]:
    """Build the C10 audit without writing the artifact."""

    sources = [_audit_file(root, path) for path in CRITICAL_SOURCE_FILES]
    evidence = [_audit_file(root, path) for path in REQUIRED_EVIDENCE_FILES]
    comparison = _load_json(
        root / "validation_results/cptp_rk4_comparison.json"
    )
    comparison_pass = bool(
        comparison
        and comparison.get("summary", {}).get("all_cases_pass") is True
    )
    openapi = app.openapi()
    method_contract = _method_openapi_contract(openapi)
    python_smokes = _backend_smokes("python")
    rust_available = is_rust_kernel_available()
    rust_smokes = _backend_smokes("rust") if rust_available else None

    source_pass = all(item["exists"] for item in sources)
    evidence_pass = all(item["exists"] for item in evidence)
    api_pass = (
        method_contract["two_level"]["default"] == "fixed_step_rk4"
        and method_contract["qutrit"]["default"] == "fixed_step_rk4"
        and method_contract["two_level"]["enum"]
        == ["fixed_step_rk4", "explicit_cptp"]
        and method_contract["qutrit"]["enum"]
        == ["fixed_step_rk4", "explicit_cptp"]
        and _smokes_pass(python_smokes)
        and (rust_smokes is None or _smokes_pass(rust_smokes))
    )
    overall_pass = (
        source_pass and evidence_pass and comparison_pass and api_pass
    )
    return {
        "validation": "CPTP-MODEL-FREEZE-C10",
        "freeze_schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "decision": "PASS WITH RESTRICTIONS" if overall_pass else "FAIL",
        "overall_pass": overall_pass,
        "frozen_contract": {
            "freeze_id": CPTP_MODEL_FREEZE_ID,
            "public_evolution_method": "explicit_cptp",
            "evolution_method_id": EXPLICIT_CPTP_EVOLUTION_ID,
            "rk4_reference_method_id": "fixed_step_rk4_v1",
            "supported_pulse_models": [
                "driven_two_level_rwa_experimental_v1",
                "driven_transmon_qutrit_rwa_experimental_v1",
            ],
            "choi_convention_id": CHOI_CONVENTION_ID,
            "choi_normalization": "unnormalized",
            "choi_basis_order": "input_tensor_output",
            "cp_tolerance": DEFAULT_CP_TOLERANCE,
            "tp_tolerance": DEFAULT_TP_TOLERANCE,
            "liouvillian_vectorization_id": (
                LIOUVILLIAN_VECTORIZATION_ID
            ),
            "python_exponential_method": MATRIX_EXPONENTIAL_METHOD,
            "rust_exponential_method": RUST_EXPONENTIAL_METHOD,
            "time_dependent_sampling_id": PIECEWISE_SAMPLING_ID,
            "units": {
                "time": "us",
                "hamiltonian": "rad/us",
                "collapse_operator": "sqrt(1/us)",
                "liouvillian": "1/us",
            },
            "cleanup_applied": False,
            "pulse_api_default": "fixed_step_rk4",
        },
        "guarantee_boundary": {
            "guaranteed": [
                "each frozen-interval GKSL exponential is Choi-audited CPTP",
                "each composed checkpoint map is Choi-audited CPTP",
                "Python and Rust use the same Choi convention and tolerances",
                "state application does not use density-matrix cleanup",
            ],
            "approximated": [
                "time-dependent Hamiltonians are frozen at interval midpoints",
                "accuracy to the time-ordered continuous solution depends on "
                "interval refinement",
            ],
            "not_claimed": [
                "calibrated hardware prediction",
                "non-Markovian dynamics",
                "laboratory-frame carrier resolution",
                "multi-qubit pulse control",
                "CPTP gate-aware run_simulation execution",
            ],
        },
        "api_contract": {
            "openapi_method_contract": method_contract,
            "openapi_method_contract_sha256": _canonical_sha256(
                method_contract
            ),
            "python_smokes": python_smokes,
            "rust_extension_available": rust_available,
            "rust_smokes": rust_smokes,
        },
        "comparison_evidence": {
            "file": "validation_results/cptp_rk4_comparison.json",
            "all_cases_pass": comparison_pass,
            "summary": (
                None if comparison is None else comparison.get("summary")
            ),
        },
        "source_revision": {
            "git_commit": _git_commit(root),
            "critical_source_tree_sha256": _tree_sha256(sources),
            "files": sources,
        },
        "evidence_files": evidence,
        "environment": {
            "python": platform.python_version(),
            "packages": {
                package: _package_version(package)
                for package in ("numpy", "fastapi", "pydantic")
            },
        },
        "phase2_complete": overall_pass,
        "next_phase": (
            "Phase 3A CPTP-to-QuTiP audit extension"
            if overall_pass
            else "Resolve C10 blocking checks"
        ),
    }


def write_freeze_report(
    output_path: Path,
    *,
    root: Path = ROOT,
) -> dict[str, Any]:
    """Write the canonical C10 JSON artifact."""

    report = build_freeze_report(root)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def _backend_smokes(backend: str) -> dict[str, Any]:
    qubit = run_pulse_request(PulseSimulateRequest.model_validate(
        _qubit_payload(backend)
    ))
    qutrit = run_qutrit_pulse_request(
        QutritPulseSimulateRequest.model_validate(
            _qutrit_payload(backend)
        )
    )
    return {
        "qubit": _response_summary(qubit),
        "qutrit": _response_summary(qutrit),
    }


def _response_summary(response: dict[str, Any]) -> dict[str, Any]:
    evolution = response["diagnostics"]["evolution"]
    audits = [
        evolution["open_pulse_audit"],
        evolution["open_idle_audit"],
        evolution["closed_pulse_audit"],
        evolution["closed_idle_audit"],
    ]
    present_audits = [item for item in audits if item is not None]
    return {
        "model_id": response["model"]["model_id"],
        "contract_version": response["contract_version"],
        "method": evolution["resolved"],
        "method_id": evolution["method_id"],
        "cptp_guaranteed_by_construction": (
            evolution["cptp_guaranteed_by_construction"]
        ),
        "cleanup_applied": evolution["cleanup_applied"],
        "all_maps_cptp": all(
            item["all_maps_cptp"] for item in present_audits
        ),
        "minimum_choi_eigenvalue": min(
            item["minimum_choi_eigenvalue"] for item in present_audits
        ),
        "maximum_tp_frobenius_error": max(
            item["maximum_trace_preservation_frobenius_error"]
            for item in present_audits
        ),
        "maximum_state_trace_error": response["diagnostics"][
            "maximum_cleaned_trace_error"
        ],
        "minimum_state_eigenvalue": response["diagnostics"][
            "minimum_cleaned_eigenvalue"
        ],
    }


def _smokes_pass(smokes: dict[str, Any]) -> bool:
    return all(
        item["method_id"] == EXPLICIT_CPTP_EVOLUTION_ID
        and item["cptp_guaranteed_by_construction"] is True
        and item["cleanup_applied"] is False
        and item["all_maps_cptp"] is True
        and item["maximum_state_trace_error"] <= 1e-10
        and item["minimum_state_eigenvalue"] >= -1e-10
        for item in smokes.values()
    )


def _method_openapi_contract(openapi: dict[str, Any]) -> dict[str, Any]:
    schemas = openapi["components"]["schemas"]
    return {
        "two_level": schemas["PulseSimulateRequest"]["properties"][
            "evolution_method"
        ],
        "qutrit": schemas["QutritPulseSimulateRequest"]["properties"][
            "evolution_method"
        ],
    }


def _audit_file(root: Path, relative_path: str) -> dict[str, Any]:
    path = root / relative_path
    return {
        "file": relative_path,
        "exists": path.is_file(),
        "sha256": (
            hashlib.sha256(path.read_bytes()).hexdigest()
            if path.is_file()
            else None
        ),
    }


def _tree_sha256(files: list[dict[str, Any]]) -> str:
    return _canonical_sha256([
        {"file": item["file"], "sha256": item["sha256"]}
        for item in files
    ])


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _package_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return "not-installed"


def _git_commit(root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"


def _qubit_payload(backend: str) -> dict[str, Any]:
    return {
        "model_id": "driven_two_level_rwa_experimental_v1",
        "initial_state": "0",
        "backend": backend,
        "evolution_method": "explicit_cptp",
        "pulse": {
            "shape": "square",
            "amplitude_mode": "target_rotation_angle",
            "target_rotation_angle_rad": math.pi / 2.0,
            "pulse_duration_us": 0.02,
            "phase_rad": 0.2,
            "detuning_rad_per_us": 0.1,
            "drag_beta_us": 0.0,
        },
        "total_simulation_time_us": 0.04,
        "environment": {
            "input_mode": "direct_rates",
            "gamma_down_per_us": 0.1,
            "gamma_up_per_us": 0.02,
            "gamma_phi_per_us": 0.05,
        },
        "snapshot_options": {
            "uniform_count": 5,
            "custom_times_us": [0.02],
        },
    }


def _qutrit_payload(backend: str) -> dict[str, Any]:
    return {
        "model_id": "driven_transmon_qutrit_rwa_experimental_v1",
        "initial_state": "0",
        "anharmonicity_mhz": -100.0,
        "backend": backend,
        "evolution_method": "explicit_cptp",
        "pulse": {
            "shape": "gaussian",
            "amplitude_mode": "target_rotation_angle",
            "target_rotation_angle_rad": math.pi / 2.0,
            "sigma_us": 0.002,
            "truncation_sigma": 4.0,
            "phase_rad": 0.2,
            "detuning_rad_per_us": 0.1,
            "drag_beta_us": 0.001,
        },
        "total_simulation_time_us": 0.02,
        "environment": {
            "input_mode": "direct_rates",
            "gamma_10_down_per_us": 0.2,
            "gamma_01_up_per_us": 0.02,
            "gamma_21_down_per_us": 0.4,
            "gamma_12_up_per_us": 0.03,
            "gamma_phi_adjacent_per_us": 0.08,
        },
        "snapshot_options": {
            "uniform_count": 5,
            "custom_times_us": [0.016],
        },
    }
