"""Validate the zero-temperature thermal-excitation limit."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._common import git_commit
from core.constants import BOLTZMANN_CONSTANT, PLANCK_CONSTANT
from core.gates import SIGMA_MINUS, multi_qubit_environment_collapse_operators
from core.physical_environment import compute_environment_rates
from tests.test_validation_zero_temperature_thermal_excitation import (
    DETAILED_BALANCE_ABS_TOLERANCE,
    DETAILED_BALANCE_REL_TOLERANCE,
    _environment,
    analytic_thermal_occupation,
)


CSV_FIELDS = [
    "case",
    "temperature_mk",
    "frequency_ghz",
    "n_th",
    "gamma0_per_us",
    "gamma_up_per_us",
    "gamma_down_per_us",
    "detailed_balance_error",
    "finite",
    "result",
]


def main() -> int:
    args = _parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []

    def add_rate_case(
        name: str,
        temperature_mk: float,
        frequency_ghz: float,
        *,
        expected_zero_temperature: bool = False,
        expected_n_th: float | None = None,
        detailed_balance: bool = False,
    ) -> None:
        try:
            rates = compute_environment_rates(
                _environment(
                    temperature_mk=temperature_mk,
                    frequency_ghz=frequency_ghz,
                )
            )
            gamma0 = rates.gamma0_per_us
            finite = all(math.isfinite(value) for value in (
                rates.n_th,
                gamma0,
                rates.gamma_up_per_us,
                rates.gamma_down_per_us,
            ))
            nonnegative = all(value >= 0.0 for value in (
                rates.n_th,
                gamma0,
                rates.gamma_up_per_us,
                rates.gamma_down_per_us,
            ))
            detail_error = None
            if detailed_balance:
                expected_ratio = math.exp(
                    -PLANCK_CONSTANT * frequency_ghz * 1e9
                    / (BOLTZMANN_CONSTANT * temperature_mk * 1e-3)
                )
                actual_ratio = rates.gamma_up_per_us / rates.gamma_down_per_us
                detail_error = abs(actual_ratio - expected_ratio)
            checks = [finite, nonnegative]
            if expected_zero_temperature:
                checks.extend([
                    rates.n_th == 0.0,
                    rates.gamma_up_per_us == 0.0,
                    rates.gamma_down_per_us == gamma0,
                    rates.gamma_population_relaxation_per_us == gamma0,
                ])
            if expected_n_th is not None:
                checks.append(abs(rates.n_th - expected_n_th) <= 1e-12)
            if detailed_balance:
                checks.append(
                    detail_error is not None
                    and detail_error <= DETAILED_BALANCE_ABS_TOLERANCE
                    + DETAILED_BALANCE_REL_TOLERANCE * math.exp(
                        -PLANCK_CONSTANT * frequency_ghz * 1e9
                        / (BOLTZMANN_CONSTANT * temperature_mk * 1e-3)
                    )
                )
            result = "PASS" if all(checks) else "FAIL"
            rows.append({
                "case": name,
                "temperature_mk": temperature_mk,
                "frequency_ghz": frequency_ghz,
                "n_th": rates.n_th,
                "gamma0_per_us": gamma0,
                "gamma_up_per_us": rates.gamma_up_per_us,
                "gamma_down_per_us": rates.gamma_down_per_us,
                "detailed_balance_error": detail_error,
                "finite": finite,
                "result": result,
            })
        except (ArithmeticError, ValueError, OverflowError) as exc:
            rows.append({
                "case": name,
                "temperature_mk": temperature_mk,
                "frequency_ghz": frequency_ghz,
                "n_th": None,
                "gamma0_per_us": None,
                "gamma_up_per_us": None,
                "gamma_down_per_us": None,
                "detailed_balance_error": None,
                "finite": False,
                "result": f"FAIL: {type(exc).__name__}",
            })

    add_rate_case("V2-1 exact T=0", 0.0, 5.0, expected_zero_temperature=True)
    for frequency_ghz in (1.0, 5.0, 10.0):
        add_rate_case(
            f"V2-2 T=0 f={frequency_ghz:g}GHz",
            0.0,
            frequency_ghz,
            expected_zero_temperature=True,
        )
    for temperature_mk in (1e-9, 1e-6, 0.001):
        add_rate_case(
            f"V2-3 low T={temperature_mk:g}mK",
            temperature_mk,
            5.0,
            expected_n_th=analytic_thermal_occupation(temperature_mk, 5.0),
        )
    for temperature_mk in (0.0, 1.0, 10.0, 20.0, 100.0, 1000.0):
        add_rate_case(f"V2-4 temperature sweep T={temperature_mk:g}mK", temperature_mk, 5.0)
    for frequency_ghz in (1.0, 3.0, 5.0, 10.0):
        add_rate_case(f"V2-5 frequency sweep f={frequency_ghz:g}GHz", 100.0, frequency_ghz)
    for temperature_mk, frequency_ghz in (
        (20.0, 10.0),
        (50.0, 5.0),
        (100.0, 1.0),
        (200.0, 5.0),
    ):
        add_rate_case(
            f"V2-6 detailed balance T={temperature_mk:g}mK f={frequency_ghz:g}GHz",
            temperature_mk,
            frequency_ghz,
            detailed_balance=True,
        )

    rows.append(_collapse_operator_row())
    rows.append(_ideal_reference_row())
    rows.append(_monotonicity_row("V2-4 temperature monotonicity", "temperature"))
    rows.append(_monotonicity_row("V2-5 frequency monotonicity", "frequency"))

    print("case | temperature_mk | frequency_ghz | n_th | gamma0_per_us | gamma_up_per_us | gamma_down_per_us | detailed_balance_error | finite | result")
    for row in rows:
        print(" | ".join([
            str(row["case"]),
            _format(row["temperature_mk"]),
            _format(row["frequency_ghz"]),
            _format(row["n_th"]),
            _format(row["gamma0_per_us"]),
            _format(row["gamma_up_per_us"]),
            _format(row["gamma_down_per_us"]),
            _format(row["detailed_balance_error"]),
            str(row["finite"]),
            str(row["result"]),
        ]))

    report = {
        "validation": "VALIDATION-2",
        "description": "Zero-temperature thermal excitation limit and rate consistency",
        "git_commit": git_commit(ROOT),
        "formula_convention": "ordinary frequency: n_th = 1 / (exp(h*f_q/(k_B*T)) - 1)",
        "unit_convention": {
            "temperature_input": "mK",
            "temperature_internal": "K",
            "frequency_input": "GHz",
            "frequency_internal": "Hz",
            "rates": "1/us",
            "times": "us",
        },
        "implementation_functions_inspected": [
            "core.physical_environment.compute_thermal_occupation",
            "core.physical_environment._compute_rates_from_physical_inputs",
            "core.physical_environment.compute_environment_rates",
            "core.gates.multi_qubit_environment_collapse_operators",
        ],
        "tolerances": {
            "detailed_balance_absolute": DETAILED_BALANCE_ABS_TOLERANCE,
            "detailed_balance_relative": DETAILED_BALANCE_REL_TOLERANCE,
            "analytic_n_th_absolute": 1e-12,
        },
        "t1_convention": "t1_effective_us = 1 / gamma_population_relaxation_per_us",
        "canonical_rate_names": {
            "gamma0_per_us": "1 / t1_zero_temperature_us",
            "gamma_population_relaxation_per_us": "gamma_down_per_us + gamma_up_per_us",
            "t1_effective_us": "1 / gamma_population_relaxation_per_us",
        },
        "gamma1_alias": "gamma1_per_us is a legacy alias for gamma_down_per_us",
        "edge_case_policy": "T <= 0 returns n_th=0 explicitly; exponent > 700 returns n_th=0 to avoid overflow",
        "cases": rows,
        "overall_pass": all(str(row["result"]) == "PASS" for row in rows),
        "ambiguities": [
            "gamma1_per_us is retained only as a compatibility alias for gamma_down_per_us.",
        ],
    }
    json_path = args.output_dir / "validation2_zero_temperature.json"
    csv_path = args.output_dir / "validation2_zero_temperature.csv"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"artifacts | {json_path} | {csv_path}")
    return 0 if report["overall_pass"] else 1


def _collapse_operator_row() -> dict[str, object]:
    rates = compute_environment_rates(_environment(temperature_mk=0.0))
    operators = multi_qubit_environment_collapse_operators(1, rates)
    expected_relaxation = tuple(
        tuple(math.sqrt(rates.gamma_down_per_us) * value for value in row)
        for row in SIGMA_MINUS
    )
    passed = (
        rates.gamma_down_per_us > 0.0
        and rates.gamma_up_per_us == 0.0
        and len(operators) == 2
        and operators[0] == expected_relaxation
    )
    return {
        "case": "V2-7 collapse operators T=0",
        "temperature_mk": 0.0,
        "frequency_ghz": 5.0,
        "n_th": rates.n_th,
        "gamma0_per_us": rates.gamma0_per_us,
        "gamma_up_per_us": rates.gamma_up_per_us,
        "gamma_down_per_us": rates.gamma_down_per_us,
        "detailed_balance_error": None,
        "finite": True,
        "result": "PASS" if passed else "FAIL",
    }


def _ideal_reference_row() -> dict[str, object]:
    physical = compute_environment_rates(_environment(temperature_mk=0.0))
    ideal = compute_environment_rates(_environment(temperature_mk=0.0, ideal_reference=True))
    passed = (
        physical.gamma_down_per_us > 0.0
        and physical.gamma_up_per_us == 0.0
        and ideal.gamma_down_per_us == 0.0
        and ideal.gamma_up_per_us == 0.0
        and ideal.gamma_phi_per_us == 0.0
    )
    return {
        "case": "V2-8 ideal_reference separation",
        "temperature_mk": 0.0,
        "frequency_ghz": 5.0,
        "n_th": ideal.n_th,
        "gamma0_per_us": physical.gamma0_per_us,
        "gamma_up_per_us": ideal.gamma_up_per_us,
        "gamma_down_per_us": ideal.gamma_down_per_us,
        "detailed_balance_error": None,
        "finite": True,
        "result": "PASS" if passed else "FAIL",
    }


def _monotonicity_row(name: str, axis: str) -> dict[str, object]:
    if axis == "temperature":
        rates = [
            compute_environment_rates(_environment(temperature_mk=value))
            for value in (0.0, 1.0, 10.0, 20.0, 100.0, 1000.0)
        ]
        passed = (
            _nondecreasing([rate.n_th for rate in rates])
            and _nondecreasing([rate.gamma_up_per_us for rate in rates])
            and _nondecreasing([rate.gamma_down_per_us for rate in rates])
        )
    else:
        rates = [
            compute_environment_rates(
                _environment(temperature_mk=100.0, frequency_ghz=value)
            )
            for value in (1.0, 3.0, 5.0, 10.0)
        ]
        passed = (
            _nonincreasing([rate.n_th for rate in rates])
            and _nonincreasing([rate.gamma_up_per_us for rate in rates])
        )
    return {
        "case": name,
        "temperature_mk": None,
        "frequency_ghz": None,
        "n_th": None,
        "gamma0_per_us": None,
        "gamma_up_per_us": None,
        "gamma_down_per_us": None,
        "detailed_balance_error": None,
        "finite": True,
        "result": "PASS" if passed else "FAIL",
    }


def _nondecreasing(values: list[float]) -> bool:
    return all(left <= right for left, right in zip(values, values[1:]))


def _nonincreasing(values: list[float]) -> bool:
    return all(left >= right for left, right in zip(values, values[1:]))


def _format(value: object) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.6e}"
    return str(value)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "validation_results",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
