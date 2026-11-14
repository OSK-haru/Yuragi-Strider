"""Write the thermal-photon dephasing check (decision D90) to validation_results."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._common import git_commit
from tests.test_validation_thermal_photon_dephasing import (
    CASES,
    FOCK_DIMENSION,
    MAX_RELATIVE_RATE_ERROR,
    run_case,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "validation_results")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    cases = [run_case(*case) for case in CASES]
    report = {
        "validation": "thermal_photon_dephasing",
        "decision": "D90",
        "commit": git_commit(ROOT),
        "claim": ("The production pure-dephasing term Gamma_th = (kappa/2) Re[sqrt((1 + 2i chi/kappa)^2 "
                  "+ 8i chi n/kappa) - 1] equals the long-time decay rate of the qubit coherence obtained by "
                  "solving the qubit and its thermal readout resonator together (H = chi a^dag a sigma_z)."),
        "reference": "QuTiP mesolve of qubit x resonator, Fock dimension "
                     f"{FOCK_DIMENSION}, log-linear fit for t > 10/kappa",
        "scope": ("Checks the rate formula only. The assumption that the resonator is at the same temperature "
                  "as the qubit bath, and that other dephasing sources do not depend on temperature, is not "
                  "tested here."),
        "tolerance": {"max_relative_rate_error": MAX_RELATIVE_RATE_ERROR},
        "cases": cases,
        "pass": all(case["pass"] for case in cases),
    }
    path = args.output_dir / "thermal_photon_dephasing.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path} pass={report['pass']}")
    for case in cases:
        print(f"  {case['name']}: n_r={case['n_th_resonator']:.3e} production={case['production_rate_per_us']:.6e} "
              f"fit={case['fitted_rate_per_us']:.6e} rel={case['relative_error']:.2e}")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
