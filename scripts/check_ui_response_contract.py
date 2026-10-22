"""Lightweight structural check for the UI response the backend produces.

By default the response is generated from the current code, so the check
notices drift. ``--sample`` checks the saved outputs/ui_response_example.json
instead (a stale file passes without telling you the backend has moved on).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = ROOT / "outputs" / "ui_response_example.json"


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_string(value: Any) -> bool:
    return isinstance(value, str)


def add_error(errors: list[str], path: str, message: str) -> None:
    errors.append(f"{path}: {message}")


def require_key(
    obj: dict[str, Any],
    key: str,
    path: str,
    errors: list[str],
) -> Any:
    if key not in obj:
        add_error(errors, path, f"missing key '{key}'")
        return None
    return obj[key]


def check_string(value: Any, path: str, errors: list[str]) -> None:
    if not is_string(value):
        add_error(errors, path, "expected string")


def check_number(value: Any, path: str, errors: list[str]) -> None:
    if not is_number(value):
        add_error(errors, path, "expected number")


def check_number_or_null(value: Any, path: str, errors: list[str]) -> None:
    if value is not None and not is_number(value):
        add_error(errors, path, "expected number or null")


def check_bool(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, bool):
        add_error(errors, path, "expected boolean")


def check_list(value: Any, path: str, errors: list[str]) -> list[Any] | None:
    if not isinstance(value, list):
        add_error(errors, path, "expected array")
        return None
    return value


def check_dict(value: Any, path: str, errors: list[str]) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        add_error(errors, path, "expected object")
        return None
    return value


def check_circuit(circuit: Any, errors: list[str]) -> None:
    circuit_obj = check_dict(circuit, "circuit", errors)
    if circuit_obj is None:
        return

    check_number(require_key(circuit_obj, "qubit_count", "circuit", errors), "circuit.qubit_count", errors)
    columns = check_list(require_key(circuit_obj, "columns", "circuit", errors), "circuit.columns", errors)
    if columns is None:
        return

    for index, column in enumerate(columns):
        column_obj = check_dict(column, f"circuit.columns[{index}]", errors)
        if column_obj is None:
            continue

        check_string(require_key(column_obj, "id", f"circuit.columns[{index}]", errors), f"circuit.columns[{index}].id", errors)
        check_number(require_key(column_obj, "step", f"circuit.columns[{index}]", errors), f"circuit.columns[{index}].step", errors)
        check_number_or_null(
            require_key(column_obj, "duration_us", f"circuit.columns[{index}]", errors),
            f"circuit.columns[{index}].duration_us",
            errors,
        )

        gates = check_list(require_key(column_obj, "gates", f"circuit.columns[{index}]", errors), f"circuit.columns[{index}].gates", errors)
        if gates is None:
            continue

        for gate_index, gate in enumerate(gates):
            gate_obj = check_dict(gate, f"circuit.columns[{index}].gates[{gate_index}]", errors)
            if gate_obj is None:
                continue

            check_string(
                require_key(gate_obj, "label", f"circuit.columns[{index}].gates[{gate_index}]", errors),
                f"circuit.columns[{index}].gates[{gate_index}].label",
                errors,
            )
            check_string(
                require_key(gate_obj, "type", f"circuit.columns[{index}].gates[{gate_index}]", errors),
                f"circuit.columns[{index}].gates[{gate_index}].type",
                errors,
            )
            qubits = check_list(
                require_key(gate_obj, "qubits", f"circuit.columns[{index}].gates[{gate_index}]", errors),
                f"circuit.columns[{index}].gates[{gate_index}].qubits",
                errors,
            )
            if qubits is not None:
                for qubit_index, qubit in enumerate(qubits):
                    check_number(
                        qubit,
                        f"circuit.columns[{index}].gates[{gate_index}].qubits[{qubit_index}]",
                        errors,
                    )
            check_string(
                require_key(gate_obj, "kind", f"circuit.columns[{index}].gates[{gate_index}]", errors),
                f"circuit.columns[{index}].gates[{gate_index}].kind",
                errors,
            )


def check_parameters(parameters: Any, errors: list[str]) -> None:
    params_obj = check_dict(parameters, "parameters", errors)
    if params_obj is None:
        return

    for key in (
        "environment_model",
        "input_mode",
        "temperature_k",
        "temperature_mk",
        "normalized_temperature",
        "qubit_frequency_ghz",
        "device_quality",
        "flux_noise_phi0",
        "duration_us",
        "time_steps",
        "fidelity_threshold",
        "simulation_backend",
    ):
        value = require_key(params_obj, key, "parameters", errors)
        if key in {"environment_model", "input_mode", "simulation_backend"}:
            check_string(value, f"parameters.{key}", errors)
        elif key in {"duration_us", "time_steps"}:
            check_number(value, f"parameters.{key}", errors)
        elif key == "fidelity_threshold":
            check_number(value, f"parameters.{key}", errors)
        else:
            check_number_or_null(value, f"parameters.{key}", errors)


def check_diagnostics(diagnostics: Any, errors: list[str]) -> None:
    diag_obj = check_dict(diagnostics, "diagnostics", errors)
    if diag_obj is None:
        return

    for key in (
        "simulation_model",
        "evolution_mode",
        "simulation_backend",
        "backend_name",
        "rust_kernel_mode",
    ):
        check_string(require_key(diag_obj, key, "diagnostics", errors), f"diagnostics.{key}", errors)

    for key in (
        "rust_kernel_call_count",
        "rust_kernel_sampled_batch_count",
    ):
        check_number(require_key(diag_obj, key, "diagnostics", errors), f"diagnostics.{key}", errors)

    for key in ("backend_fallback_used", "rust_kernel_fallback_used"):
        check_bool(require_key(diag_obj, key, "diagnostics", errors), f"diagnostics.{key}", errors)


def check_summary(summary: Any, errors: list[str]) -> None:
    summary_obj = check_dict(summary, "summary", errors)
    if summary_obj is None:
        return

    for key in (
        "final_fidelity",
        "final_purity",
        "completion_fidelity",
        "completion_purity",
        "effective_time_us",
    ):
        value = require_key(summary_obj, key, "summary", errors)
        check_number_or_null(value, f"summary.{key}", errors)


def check_timeline(timeline: Any, errors: list[str]) -> None:
    timeline_list = check_list(timeline, "timeline", errors)
    if timeline_list is None:
        return

    for index, point in enumerate(timeline_list):
        point_obj = check_dict(point, f"timeline[{index}]", errors)
        if point_obj is None:
            continue

        check_number(require_key(point_obj, "time_us", f"timeline[{index}]", errors), f"timeline[{index}].time_us", errors)
        check_number_or_null(require_key(point_obj, "fidelity", f"timeline[{index}]", errors), f"timeline[{index}].fidelity", errors)
        check_number_or_null(require_key(point_obj, "purity", f"timeline[{index}]", errors), f"timeline[{index}].purity", errors)


def check_output_probabilities(output_probabilities: Any, errors: list[str]) -> None:
    probs_obj = check_dict(output_probabilities, "output_probabilities", errors)
    if probs_obj is None:
        return

    for key, value in probs_obj.items():
        if not is_string(key):
            add_error(errors, "output_probabilities", "expected string keys")
        check_number(value, f"output_probabilities.{key}", errors)


def check_run(run: Any, errors: list[str]) -> None:
    run_obj = check_dict(run, "run", errors)
    if run_obj is None:
        return

    for key in ("status", "selected_backend", "last_run_label"):
        check_string(require_key(run_obj, key, "run", errors), f"run.{key}", errors)


def check_warnings_or_issues(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, list):
        add_error(errors, path, "expected array")
        return
    for index, item in enumerate(value):
        if not isinstance(item, (str, dict)):
            add_error(errors, f"{path}[{index}]", "expected string or object")


def load_live_response() -> dict[str, Any]:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    from core.simulator import run_simulation
    from core.ui_response import simulation_result_to_ui_response
    from export_ui_response_example import build_example_config

    response = simulation_result_to_ui_response(run_simulation(build_example_config()))
    return json.loads(json.dumps(response, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sample",
        action="store_true",
        help="Check the saved outputs/ui_response_example.json instead of a live response.",
    )
    args = parser.parse_args(argv)

    if args.sample:
        if not OUTPUT_PATH.exists():
            print(
                "Missing outputs/ui_response_example.json. "
                "Run .\\.venv\\Scripts\\python.exe -B scripts/export_ui_response_example.py"
            )
            return 1
        with OUTPUT_PATH.open("r", encoding="utf-8") as file:
            data = json.load(file)
    else:
        data = load_live_response()

    if not isinstance(data, dict):
        print("UI response: expected top-level object")
        return 1

    errors: list[str] = []

    for key in (
        "circuit",
        "parameters",
        "diagnostics",
        "summary",
        "timeline",
        "output_probabilities",
        "run",
        "warnings",
        "issues",
    ):
        require_key(data, key, "root", errors)

    if "circuit" in data:
        check_circuit(data["circuit"], errors)
    if "parameters" in data:
        check_parameters(data["parameters"], errors)
    if "diagnostics" in data:
        check_diagnostics(data["diagnostics"], errors)
    if "summary" in data:
        check_summary(data["summary"], errors)
    if "timeline" in data:
        check_timeline(data["timeline"], errors)
    if "output_probabilities" in data:
        check_output_probabilities(data["output_probabilities"], errors)
    if "run" in data:
        check_run(data["run"], errors)
    if "warnings" in data:
        check_warnings_or_issues(data["warnings"], "warnings", errors)
    if "issues" in data:
        check_warnings_or_issues(data["issues"], "issues", errors)

    if errors:
        print("UI response contract check failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
