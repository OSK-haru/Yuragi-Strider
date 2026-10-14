"""Reproducible pilot for local shared-QPU approximations, not a runtime backend.

The two-qubit references use the current internal physical primitives.  The
400-node test measures only sparse topology lookup and independent eight-qubit
statevector updates; it does not measure noisy shared-QPU execution.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import median
import sys
from time import perf_counter

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.experimental_isolation_physics import (  # noqa: E402
    CorrelatedReadoutChannel,
    DriveCrosstalkTransfer,
    ExtendedTransmonNetworkHamiltonian,
    ImperfectResetSpec,
    ResidualZZCoupling,
    apply_correlated_readout,
    apply_local_imperfect_reset,
)
from core.pulse_envelopes import SquarePulseEnvelope  # noqa: E402
from core.pulse_transmon_network import (  # noqa: E402
    CoupledTransmonNetworkHamiltonian,
    ScheduledTransmonDrive,
)


ZERO = np.array((1.0, 0.0), dtype=np.complex128)
ONE = np.array((0.0, 1.0), dtype=np.complex128)
PLUS = (ZERO + ONE) / np.sqrt(2.0)
X = np.array(((0.0, 1.0), (1.0, 0.0)), dtype=np.complex128)
NUMBER = np.diag((0.0, 1.0)).astype(np.complex128)


def _evolve(state: np.ndarray, hamiltonian: np.ndarray, duration_us: float) -> np.ndarray:
    eigenvalues, eigenvectors = np.linalg.eigh(hamiltonian)
    return eigenvectors @ (np.exp(-1j * duration_us * eigenvalues) * (eigenvectors.conj().T @ state))


def _p_plus_first(state: np.ndarray) -> float:
    spectator_dimension = state.size // 2
    projector = np.kron((np.eye(2) + X) / 2.0, np.eye(spectator_dimension))
    return float(np.real(np.vdot(state, projector @ state)))


def _p_one_second(state: np.ndarray) -> float:
    projector = np.kron(np.eye(2), NUMBER)
    return float(np.real(np.vdot(state, projector @ state)))


def _base_network(drives: tuple[ScheduledTransmonDrive, ...] = ()) -> CoupledTransmonNetworkHamiltonian:
    return CoupledTransmonNetworkHamiltonian(
        anharmonicities_rad_per_us=(0.0, 0.0),
        detunings_rad_per_us=(0.0, 0.0),
        couplings=(),
        drives=drives,
        local_dimension=2,
    )


def reset_case() -> dict[str, float]:
    spec = ImperfectResetSpec(success_probability=0.9)
    outputs = []
    for input_state in (ZERO, ONE):
        density = np.outer(input_state, input_state.conj())
        reset = apply_local_imperfect_reset(
            tuple(tuple(complex(value) for value in row) for row in density),
            subsystem_count=1,
            target=0,
            spec=spec,
        )
        outputs.append(float(np.real(reset[1][1])))
    return {
        "p_one_after_victim_zero": outputs[0],
        "p_one_after_victim_one": outputs[1],
        "naive_fresh_zero_error_victim_one": outputs[1],
    }


def drive_case() -> dict[str, float]:
    duration_us = 0.1
    amplitude = 10.0
    transfer = 0.2
    drive = ScheduledTransmonDrive(
        target=0,
        start_time_us=0.0,
        envelope=SquarePulseEnvelope(amplitude, duration_us),
    )
    reference = ExtendedTransmonNetworkHamiltonian(
        _base_network((drive,)),
        drive_crosstalk=(DriveCrosstalkTransfer(0, 1, transfer),),
    )
    exact = _evolve(np.kron(ZERO, ZERO), reference.evaluate_array(duration_us / 2), duration_us)
    local_source = _evolve(ZERO, amplitude * X / 2.0, duration_us)
    local_target = _evolve(ZERO, transfer * amplitude * X / 2.0, duration_us)
    local = np.kron(local_source, local_target)
    return {
        "p_target_one_exact": _p_one_second(exact),
        "p_target_one_local": _p_one_second(local),
        "state_error_norm": float(np.linalg.norm(exact - local)),
    }


def zz_case() -> dict[str, object]:
    duration_us = 0.1
    phase = np.pi / 2.0
    shift = phase / duration_us
    hamiltonian = _zz_hamiltonian(shift)
    cases: dict[str, dict[str, float]] = {}
    for label, spectator, expectation in (
        ("spectator_zero", ZERO, 0.0),
        ("spectator_one", ONE, 1.0),
        ("spectator_plus", PLUS, 0.5),
    ):
        exact = _evolve(np.kron(PLUS, spectator), hamiltonian, duration_us)
        local_first = _evolve(PLUS, shift * expectation * NUMBER, duration_us)
        local_second = _evolve(spectator, shift * 0.5 * NUMBER, duration_us)
        approximation = np.kron(local_first, local_second)
        p_exact = _p_plus_first(exact)
        p_local = _p_plus_first(approximation)
        cases[label] = {
            "p_first_plus_exact": p_exact,
            "p_first_plus_mean_field": p_local,
            "measurement_tvd": abs(p_exact - p_local),
        }
    sweep = []
    for phase in (0.0, np.pi / 8.0, np.pi / 4.0, np.pi / 2.0, 3.0 * np.pi / 4.0, np.pi):
        exact = _evolve(np.kron(PLUS, PLUS), _zz_hamiltonian(phase / duration_us), duration_us)
        local = np.kron(
            _evolve(PLUS, phase * NUMBER / (2.0 * duration_us), duration_us),
            _evolve(PLUS, phase * NUMBER / (2.0 * duration_us), duration_us),
        )
        sweep.append({
            "phase_rad": float(phase),
            "measurement_tvd": abs(_p_plus_first(exact) - _p_plus_first(local)),
        })
    cluster_sizes = []
    for count in (2, 3, 4):
        network = CoupledTransmonNetworkHamiltonian(
            anharmonicities_rad_per_us=(0.0,) * count,
            detunings_rad_per_us=(0.0,) * count,
            couplings=(),
            drives=(),
            local_dimension=2,
        )
        coupled = ExtendedTransmonNetworkHamiltonian(
            network,
            residual_zz=tuple(ResidualZZCoupling(0, target, shift) for target in range(1, count)),
        )
        initial = PLUS
        for _ in range(1, count):
            initial = np.kron(initial, PLUS)
        exact = _evolve(initial, coupled.evaluate_array(0.0), duration_us)
        mean_field_first = _evolve(
            PLUS,
            (count - 1) * shift * NUMBER / 2.0,
            duration_us,
        )
        p_exact = _p_plus_first(exact)
        p_local = float(np.real(np.vdot(
            mean_field_first,
            ((np.eye(2) + X) / 2.0) @ mean_field_first,
        )))
        cluster_sizes.append({
            "qubits": count,
            "p_first_plus_exact": p_exact,
            "p_first_plus_mean_field": p_local,
            "measurement_tvd": abs(p_exact - p_local),
        })
    return {
        "fixed_phase_controls": cases,
        "coherent_spectator_phase_sweep": sweep,
        "coherent_cluster_size_sweep": cluster_sizes,
    }


def _zz_hamiltonian(shift_rad_per_us: float) -> np.ndarray:
    reference = ExtendedTransmonNetworkHamiltonian(
        _base_network(),
        residual_zz=(ResidualZZCoupling(0, 1, shift_rad_per_us),),
    )
    return reference.evaluate_array(0.0)


def readout_case() -> dict[str, float]:
    shared_flip_probability = 0.12
    matrix = np.zeros((4, 4), dtype=np.float64)
    for true in range(4):
        matrix[true, true] += 1.0 - shared_flip_probability
        matrix[true ^ 3, true] += shared_flip_probability
    input_probabilities = {format(index, "02b"): float(index == 0) for index in range(4)}
    joint = apply_correlated_readout(
        input_probabilities,
        2,
        (CorrelatedReadoutChannel((0, 1), tuple(map(tuple, matrix))),),
    )
    p = shared_flip_probability
    independent = {
        "00": (1.0 - p) ** 2,
        "01": p * (1.0 - p),
        "10": p * (1.0 - p),
        "11": p**2,
    }
    tvd = 0.5 * sum(abs(joint[key] - independent[key]) for key in joint)
    return {
        "p_observed_11_joint": joint["11"],
        "p_observed_11_independent": independent["11"],
        "joint_vs_independent_tvd": tvd,
    }


def _grid_adjacency(side: int) -> tuple[tuple[int, ...], ...]:
    adjacency: list[tuple[int, ...]] = []
    for row in range(side):
        for column in range(side):
            neighbors = []
            if row > 0:
                neighbors.append((row - 1) * side + column)
            if row + 1 < side:
                neighbors.append((row + 1) * side + column)
            if column > 0:
                neighbors.append(row * side + column - 1)
            if column + 1 < side:
                neighbors.append(row * side + column + 1)
            adjacency.append(tuple(neighbors))
    return tuple(adjacency)


def _rotate_local(state: np.ndarray, qubit: int, rotation: np.ndarray) -> np.ndarray:
    count = state.size.bit_length() - 1
    tensor = state.reshape((2,) * count)
    moved = np.moveaxis(tensor, qubit, 0)
    updated = rotation @ moved.reshape((2, -1))
    return np.moveaxis(updated.reshape((2,) * count), 0, qubit).reshape(-1)


def topology_pilot(*, side: int, events: int, job_qubits: int, repetitions: int = 5) -> dict[str, float | int]:
    if side < job_qubits or events < 1 or repetitions < 1 or not 1 <= job_qubits <= 8:
        raise ValueError("require side >= job_qubits, events >= 1, repetitions >= 1, 1 <= job_qubits <= 8")
    adjacency = _grid_adjacency(side)
    jobs = (tuple(range(job_qubits)), tuple(side + index for index in range(job_qubits)))
    active = set(jobs[0] + jobs[1])
    topology_samples = []
    evolution_samples = []
    affected: set[int] = set()
    angle = 0.02
    rotation = np.cos(angle / 2.0) * np.eye(2) - 1j * np.sin(angle / 2.0) * X
    for _ in range(repetitions):
        started = perf_counter()
        for _ in range(events):
            affected = set(active)
            for qubit in active:
                affected.update(adjacency[qubit])
        topology_samples.append(perf_counter() - started)

        states = [np.zeros(1 << job_qubits, dtype=np.complex128) for _ in jobs]
        for state in states:
            state[0] = 1.0
        started = perf_counter()
        for event in range(events):
            for index, state in enumerate(states):
                states[index] = _rotate_local(state, event % job_qubits, rotation)
        evolution_samples.append(perf_counter() - started)
    return {
        "topology_nodes": side * side,
        "active_qubits": len(active),
        "one_hop_affected_qubits": len(affected),
        "events": events,
        "repetitions": repetitions,
        "topology_lookup_median_seconds": median(topology_samples),
        "independent_statevector_updates_median_seconds": median(evolution_samples),
        "local_statevector_bytes": sum(state.nbytes for state in states),
        "hypothetical_joint_density_bytes": (1 << (4 * job_qubits)) * 16,
    }


def run_experiment(*, side: int = 20, events: int = 1000, job_qubits: int = 8, repetitions: int = 5) -> dict[str, object]:
    return {
        "method": "deterministic_pilot_not_runtime_validation",
        "reset": reset_case(),
        "drive": drive_case(),
        "zz": zz_case(),
        "readout": readout_case(),
        "topology_pilot": topology_pilot(side=side, events=events, job_qubits=job_qubits, repetitions=repetitions),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topology-side", type=int, default=20)
    parser.add_argument("--events", type=int, default=1000)
    parser.add_argument("--job-qubits", type=int, default=8)
    parser.add_argument("--repetitions", type=int, default=5)
    args = parser.parse_args()
    result = run_experiment(
        side=args.topology_side,
        events=args.events,
        job_qubits=args.job_qubits,
        repetitions=args.repetitions,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
