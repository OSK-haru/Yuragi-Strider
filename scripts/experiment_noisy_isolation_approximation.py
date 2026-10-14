"""Noisy, time-resolved comparison of coupled and local shared-QPU models.

This is independent validation, not a service backend. The reference uses the
existing two-level transmon Hamiltonian, internal ZZ/drive transfer, and
network-local Lindblad jumps. The candidate is self-consistent local mean
field with the same local drives and jumps; it cannot retain entanglement.
Parameters are illustrative stress inputs, not hardware calibration.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
from typing import Protocol

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.experimental_isolation_physics import (  # noqa: E402
    DriveCrosstalkTransfer,
    ExtendedTransmonNetworkHamiltonian,
    ImperfectResetSpec,
    ResidualZZCoupling,
    apply_local_imperfect_reset,
)
from core.pulse_envelopes import SquarePulseEnvelope  # noqa: E402
from core.pulse_qutrit_open_system import (  # noqa: E402
    QutritDissipationRates,
)
from core.pulse_transmon_network import (  # noqa: E402
    CoupledTransmonNetworkHamiltonian,
    ScheduledTransmonDrive,
    network_collapse_operator_matrices,
)


ZERO = np.array((1.0, 0.0), dtype=np.complex128)
ONE = np.array((0.0, 1.0), dtype=np.complex128)
PLUS = (ZERO + ONE) / math.sqrt(2.0)
NUMBER = np.diag((0.0, 1.0)).astype(np.complex128)
HADAMARD = np.array(((1.0, 1.0), (1.0, -1.0)), dtype=np.complex128) / math.sqrt(2.0)
MEASUREMENT_X_PAIR = np.kron(HADAMARD, HADAMARD)
PULSE_START_US = 0.04
PULSE_END_US = 0.10
CHECKPOINTS_US = (0.04, 0.10, 0.20, 0.30)
DRIVE_AMPLITUDE_RAD_PER_US = 20.0
DRIVE_TRANSFER = 0.2
ZZ_SHIFT_RAD_PER_US = 8.0


class _Hamiltonian(Protocol):
    def evaluate_array(self, local_time_us: float) -> np.ndarray: ...


def _density(ket: np.ndarray) -> np.ndarray:
    return np.outer(ket, ket.conj())


def _rates(noise_scale: float) -> tuple[QutritDissipationRates, QutritDissipationRates]:
    if not math.isfinite(noise_scale) or noise_scale < 0.0:
        raise ValueError("noise_scale must be finite and non-negative")
    generated = tuple(
        QutritDissipationRates(
            input_mode="direct_rates",
            gamma_10_down_per_us=down * noise_scale,
            gamma_01_up_per_us=up * noise_scale,
            gamma_21_down_per_us=0.0,
            gamma_12_up_per_us=0.0,
            gamma_phi_adjacent_per_us=phi * noise_scale,
        )
        for down, up, phi in ((0.20, 0.01, 0.05), (0.35, 0.02, 0.08))
    )
    return generated[0], generated[1]


def _jumps(rates: tuple[QutritDissipationRates, ...]) -> tuple[np.ndarray, ...]:
    return tuple(np.asarray(operator, dtype=np.complex128) for operator in network_collapse_operator_matrices(rates, 2))


def _lindblad(rho: np.ndarray, hamiltonian: np.ndarray, jumps: tuple[np.ndarray, ...]) -> np.ndarray:
    result = -1j * (hamiltonian @ rho - rho @ hamiltonian)
    for jump in jumps:
        ldl = jump.conj().T @ jump
        result += jump @ rho @ jump.conj().T - 0.5 * (ldl @ rho + rho @ ldl)
    return result


def _rk4_state(
    rho: np.ndarray,
    time_us: float,
    step_us: float,
    hamiltonian: _Hamiltonian,
    jumps: tuple[np.ndarray, ...],
) -> np.ndarray:
    def rhs(state: np.ndarray, when: float) -> np.ndarray:
        return _lindblad(state, hamiltonian.evaluate_array(when), jumps)

    k1 = rhs(rho, time_us)
    k2 = rhs(rho + 0.5 * step_us * k1, time_us + 0.5 * step_us)
    k3 = rhs(rho + 0.5 * step_us * k2, time_us + 0.5 * step_us)
    k4 = rhs(rho + step_us * k3, time_us + step_us)
    return rho + step_us * (k1 + 2.0 * k2 + 2.0 * k3 + k4) / 6.0


def _rk4_mean_field(
    states: tuple[np.ndarray, np.ndarray],
    time_us: float,
    step_us: float,
    local_hamiltonians: tuple[_Hamiltonian, _Hamiltonian],
    local_jumps: tuple[tuple[np.ndarray, ...], tuple[np.ndarray, ...]],
    zz_shift: float,
) -> tuple[np.ndarray, np.ndarray]:
    def rhs(pair: tuple[np.ndarray, np.ndarray], when: float) -> tuple[np.ndarray, np.ndarray]:
        rho_a, rho_b = pair
        occupation_a = float(np.real(np.trace(NUMBER @ rho_a)))
        occupation_b = float(np.real(np.trace(NUMBER @ rho_b)))
        h_a = local_hamiltonians[0].evaluate_array(when) + zz_shift * occupation_b * NUMBER
        h_b = local_hamiltonians[1].evaluate_array(when) + zz_shift * occupation_a * NUMBER
        return _lindblad(rho_a, h_a, local_jumps[0]), _lindblad(rho_b, h_b, local_jumps[1])

    def add(
        pair: tuple[np.ndarray, np.ndarray],
        derivative: tuple[np.ndarray, np.ndarray],
        factor: float,
    ) -> tuple[np.ndarray, np.ndarray]:
        return pair[0] + factor * derivative[0], pair[1] + factor * derivative[1]

    k1 = rhs(states, time_us)
    k2 = rhs(add(states, k1, step_us / 2.0), time_us + step_us / 2.0)
    k3 = rhs(add(states, k2, step_us / 2.0), time_us + step_us / 2.0)
    k4 = rhs(add(states, k3, step_us), time_us + step_us)
    return (
        states[0] + step_us * (k1[0] + 2.0 * k2[0] + 2.0 * k3[0] + k4[0]) / 6.0,
        states[1] + step_us * (k1[1] + 2.0 * k2[1] + 2.0 * k3[1] + k4[1]) / 6.0,
    )


def _network(
    count: int,
    drives: tuple[ScheduledTransmonDrive, ...],
) -> CoupledTransmonNetworkHamiltonian:
    return CoupledTransmonNetworkHamiltonian(
        anharmonicities_rad_per_us=(0.0,) * count,
        detunings_rad_per_us=(0.0,) * count,
        couplings=(),
        drives=drives,
        local_dimension=2,
    )


def _probabilities(rho: np.ndarray, basis: str) -> np.ndarray:
    rotated = MEASUREMENT_X_PAIR @ rho @ MEASUREMENT_X_PAIR.conj().T if basis == "xx" else rho
    probabilities = np.real(np.diag(rotated))
    return probabilities


def _tvd(first: np.ndarray, second: np.ndarray) -> float:
    return float(0.5 * np.abs(first - second).sum())


def _physicality(rho: np.ndarray) -> dict[str, float]:
    return {
        "trace_error": float(abs(np.trace(rho) - 1.0)),
        "hermiticity_error": float(np.max(np.abs(rho - rho.conj().T))),
        "minimum_eigenvalue": float(np.min(np.linalg.eigvalsh((rho + rho.conj().T) / 2.0))),
    }


def coupled_case(
    *,
    noise_scale: float,
    zz_shift_rad_per_us: float,
    drive_enabled: bool,
    max_step_us: float = 0.002,
) -> dict[str, object]:
    if not math.isfinite(max_step_us) or max_step_us <= 0.0:
        raise ValueError("max_step_us must be finite and positive")
    source_drives = ()
    target_drives = ()
    if drive_enabled:
        duration = PULSE_END_US - PULSE_START_US
        source_drives = (ScheduledTransmonDrive(0, PULSE_START_US, SquarePulseEnvelope(DRIVE_AMPLITUDE_RAD_PER_US, duration)),)
        target_drives = (ScheduledTransmonDrive(0, PULSE_START_US, SquarePulseEnvelope(DRIVE_AMPLITUDE_RAD_PER_US * DRIVE_TRANSFER, duration)),)
    coupled = ExtendedTransmonNetworkHamiltonian(
        _network(2, source_drives),
        residual_zz=(ResidualZZCoupling(0, 1, zz_shift_rad_per_us),) if zz_shift_rad_per_us else (),
        drive_crosstalk=(DriveCrosstalkTransfer(0, 1, DRIVE_TRANSFER),) if drive_enabled else (),
    )
    local = (_network(1, source_drives), _network(1, target_drives))
    rates = _rates(noise_scale)
    joint_jumps = _jumps(rates)
    local_jumps = (_jumps((rates[0],)), _jumps((rates[1],)))
    initial = PLUS if zz_shift_rad_per_us else ZERO
    local_states = (_density(initial), _density(initial))
    joint = np.kron(local_states[0], local_states[1])
    basis = "xx" if zz_shift_rad_per_us else "zz"
    checkpoints = []
    previous = 0.0
    for endpoint in CHECKPOINTS_US:
        steps = math.ceil((endpoint - previous) / max_step_us)
        step = (endpoint - previous) / steps
        joint_h = coupled.for_segment(previous, endpoint)
        local_h = (local[0].for_segment(previous, endpoint), local[1].for_segment(previous, endpoint))
        for index in range(steps):
            when = previous + index * step
            joint = _rk4_state(joint, when, step, joint_h, joint_jumps)
            local_states = _rk4_mean_field(local_states, when, step, local_h, local_jumps, zz_shift_rad_per_us)
        previous = endpoint
        joint_probs = _probabilities(joint, basis)
        local_probs = _probabilities(np.kron(*local_states), basis)
        checkpoints.append({
            "time_us": endpoint,
            "measurement_basis": basis,
            "p_first_zero_exact": float(joint_probs[:2].sum()),
            "p_first_zero_local": float(local_probs[:2].sum()),
            "joint_outcome_tvd": _tvd(joint_probs, local_probs),
        })
    return {
        "noise_scale": noise_scale,
        "zz_shift_rad_per_us": zz_shift_rad_per_us,
        "drive_enabled": drive_enabled,
        "max_step_us": max_step_us,
        "checkpoints": checkpoints,
        "final_exact_physicality": _physicality(joint),
        "final_local_physicality": [_physicality(state) for state in local_states],
    }


def reset_decay_case(*, max_step_us: float = 0.002) -> dict[str, object]:
    if not math.isfinite(max_step_us) or max_step_us <= 0.0:
        raise ValueError("max_step_us must be finite and positive")
    spec = ImperfectResetSpec(success_probability=0.9)
    rates = QutritDissipationRates(
        input_mode="direct_rates",
        gamma_10_down_per_us=0.4,
        gamma_01_up_per_us=0.02,
        gamma_21_down_per_us=0.0,
        gamma_12_up_per_us=0.0,
        gamma_phi_adjacent_per_us=0.1,
    )
    jumps = _jumps((rates,))
    idle = _network(1, ())
    states = []
    for ket in (ZERO, ONE):
        before = _density(ket)
        after = apply_local_imperfect_reset(
            tuple(tuple(complex(value) for value in row) for row in before),
            subsystem_count=1,
            target=0,
            spec=spec,
        )
        states.append(np.asarray(after, dtype=np.complex128))
    records = [{
        "time_us": 0.0,
        "p_one_given_victim_zero": float(np.real(states[0][1, 1])),
        "p_one_given_victim_one": float(np.real(states[1][1, 1])),
    }]
    previous = 0.0
    for endpoint in (0.1, 0.5, 1.0):
        steps = math.ceil((endpoint - previous) / max_step_us)
        step = (endpoint - previous) / steps
        for index in range(steps):
            when = previous + index * step
            states = [_rk4_state(state, when, step, idle, jumps) for state in states]
        previous = endpoint
        records.append({
            "time_us": endpoint,
            "p_one_given_victim_zero": float(np.real(states[0][1, 1])),
            "p_one_given_victim_one": float(np.real(states[1][1, 1])),
        })
    for record in records:
        record["victim_dependence"] = record["p_one_given_victim_one"] - record["p_one_given_victim_zero"]
    return {"checkpoints": records, "max_step_us": max_step_us}


def shot_sampling(*, p_exact: float, p_local: float, seed: int = 20260930, trials: int = 2000) -> dict[str, object]:
    rng = np.random.default_rng(seed)
    summaries = []
    for shots in (32, 4096):
        exact_samples = rng.binomial(shots, p_exact, size=trials) / shots
        local_samples = rng.binomial(shots, p_local, size=trials) / shots
        differences = local_samples - exact_samples
        summaries.append({
            "shots_per_model": shots,
            "independent_trial_pairs": trials,
            "sampled_difference_quantiles_05_50_95": [float(value) for value in np.quantile(differences, (0.05, 0.5, 0.95))],
            "reference_binomial_standard_error": math.sqrt(p_exact * (1.0 - p_exact) / shots),
        })
    return {"seed": seed, "theoretical_difference": p_local - p_exact, "summaries": summaries}


def run_experiment(*, max_step_us: float = 0.002) -> dict[str, object]:
    cases = {
        "drive_only": [coupled_case(noise_scale=scale, zz_shift_rad_per_us=0.0, drive_enabled=True, max_step_us=max_step_us) for scale in (0.0, 1.0, 4.0)],
        "zz_only": [coupled_case(noise_scale=scale, zz_shift_rad_per_us=ZZ_SHIFT_RAD_PER_US, drive_enabled=False, max_step_us=max_step_us) for scale in (0.0, 1.0, 4.0)],
        "zz_and_drive": [coupled_case(noise_scale=scale, zz_shift_rad_per_us=ZZ_SHIFT_RAD_PER_US, drive_enabled=True, max_step_us=max_step_us) for scale in (0.0, 1.0, 4.0)],
    }
    reset = reset_decay_case(max_step_us=max_step_us)
    combined = cases["zz_and_drive"][1]["checkpoints"][-1]
    p_exact = combined["p_first_zero_exact"]
    p_local = combined["p_first_zero_local"]
    return {
        "method": "illustrative_noisy_lindblad_rk4_not_hardware_calibration",
        "rates_per_us_at_noise_scale_one": {
            "q0_down_up_phi": [0.20, 0.01, 0.05],
            "q1_down_up_phi": [0.35, 0.02, 0.08],
        },
        "cases": cases,
        "reset_decay": reset,
        "shot_sampling_for_final_combined_first_qubit": shot_sampling(p_exact=p_exact, p_local=p_local),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-step-us", type=float, default=0.002)
    args = parser.parse_args()
    print(json.dumps(run_experiment(max_step_us=args.max_step_us), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
