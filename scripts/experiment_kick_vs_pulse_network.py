"""Compare the kick model of drive crosstalk with the pulse-level coupled-transmon network.

Reference ("hardware picture"): two uncoupled transmons, one per region, driven by pulses read from each
region's activity record (a virtual Z shifts the carrier phase of later pulses), with the declared
``DriveCrosstalkTransfer`` (a complex coefficient and a detuning) leaking each drive onto the other
transmon. The Hamiltonian is integrated with ``ExtendedTransmonNetworkHamiltonian`` by the midpoint
exponential on a fine time grid.

Kick model ("simulator picture"): each region evolves alone, as the runtime does it. The virtual Z is a
real rotation of its state, and the other region's drives arrive as kicks (``leaks_into`` and ``gap_leaks``
then ``place_kicks``). Its state is rotated back by the region's final frame to be compared.

The regions are uncoupled and the leak is a classical field, so both pictures keep the two transmons in a
product state. This measures the kick approximation against the project's own pulse model; it does not
test the pulse model against a real device.
"""

from __future__ import annotations

from dataclasses import dataclass
import argparse
import cmath
import math
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.attack_simulation.circuit_program import CircuitExecutionOperation, CircuitInstruction  # noqa: E402
from core.attack_simulation.concurrent_windows import (  # noqa: E402
    DriveSchedule, ExecutionWindow, gap_before, gap_leaks, leaks_into,
)
from core.attack_simulation.event_commit_boundary import EventPosition  # noqa: E402
from core.attack_simulation.kick_application import kick_unitary, place_kicks  # noqa: E402
from core.attack_simulation.native_pulses import NativeStep, product_of  # noqa: E402
from core.attack_simulation.pulse_shape import GAUSSIAN_SHAPE, SQUARE_SHAPE, PulseShape  # noqa: E402
from core.attack_simulation.shot_activity import frame_before, shot_activity  # noqa: E402
from core.experimental_isolation_physics import DriveCrosstalkTransfer, ExtendedTransmonNetworkHamiltonian  # noqa: E402
from core.pulse_envelopes import GaussianPulseEnvelope, SquarePulseEnvelope  # noqa: E402
from core.pulse_transmon_network import CoupledTransmonNetworkHamiltonian, ScheduledTransmonDrive  # noqa: E402

PULSE_US = 0.036                      # a typical sx/x duration
SX = math.pi / 2.0
CLOCK = "world"
STEP_TOLERANCE = 0.004                # the largest rotation of any term in one reference time step, rad


def rz(angle: float) -> NativeStep:
    return NativeStep("rz", angle, 0.0)


def sx() -> NativeStep:
    return NativeStep("sx", SX, PULSE_US)


def x() -> NativeStep:
    return NativeStep("x", math.pi, PULSE_US)


@dataclass(frozen=True)
class Region:
    """One qubit's job in a window starting at ``start_us``.

    Each entry of ``gates`` is a tuple of native steps, or a number: a declared wait in microseconds.
    """

    physical_id: int
    start_us: float
    gates: tuple[tuple[NativeStep, ...] | float, ...]

    def operation(self) -> CircuitExecutionOperation:
        instructions = [CircuitInstruction("initialize", 0.0, qubits=(0,))]
        for group, steps in enumerate(self.gates, start=1):
            if isinstance(steps, (int, float)):
                instructions.append(CircuitInstruction("delay", float(steps), qubits=(0,), time_group=group,
                                                       wait_us=float(steps), intrinsic_us=float(steps)))
                continue
            duration = sum(step.duration_us for step in steps)
            instructions.append(CircuitInstruction(
                "unitary", duration, product_of(steps), (0,), time_group=group,
                intrinsic_us=duration, natives=steps))
        instructions.append(CircuitInstruction("measure", 0.0, qubits=(0,), classical_targets=(0,),
                                               time_group=len(self.gates) + 1))
        return CircuitExecutionOperation(tuple(instructions), shots=1, sampling_seed=1)

    def activity(self, shape: PulseShape = SQUARE_SHAPE):
        return shot_activity(self.operation(), reset_duration_us=0.0, measurement_duration_us=0.0,
                             pulse_shape=shape)

    def window(self, shape: PulseShape = SQUARE_SHAPE) -> ExecutionWindow:
        shot = self.activity(shape)
        return ExecutionWindow(
            f"job-{self.physical_id}-{self.start_us:g}", f"tenant-{self.physical_id}", (self.physical_id,),
            EventPosition(CLOCK, self.start_us, 0), EventPosition(CLOCK, self.start_us + shot.duration_us, 0),
            DriveSchedule(shot, 1))


Qubit = tuple[Region, ...]            # the jobs one physical qubit runs, in time order


def _rotation_z(angle: float) -> np.ndarray:
    return np.array([[cmath.exp(-0.5j * angle), 0.0], [0.0, cmath.exp(0.5j * angle)]], dtype=complex)


def _envelope(shape: PulseShape, angle: float, duration: float):
    if shape.kind == "square":
        return SquarePulseEnvelope.from_target_rotation_angle(angle, duration)
    sigma = duration / (2.0 * shape.truncation_sigma)
    return GaussianPulseEnvelope.from_target_rotation_angle(angle, sigma, shape.truncation_sigma)


def pulse_reference(qubits: tuple[Qubit, Qubit], transfers: tuple[DriveCrosstalkTransfer, ...],
                    shape: PulseShape = SQUARE_SHAPE, tolerance: float = STEP_TOLERANCE) -> np.ndarray:
    """The joint state vector of the two transmons (qubit 0 most significant), hardware picture."""
    index_of = {jobs[0].physical_id: index for index, jobs in enumerate(qubits)}
    drives = []
    for index, jobs in enumerate(qubits):
        for region in jobs:
            for item in region.activity(shape).of_qubit(0):
                if item.kind == "drive":
                    drives.append(ScheduledTransmonDrive(
                        index, region.start_us + item.start_us,
                        _envelope(shape, item.angle_rad, item.end_us - item.start_us), phase_rad=item.phase_rad))
    base = CoupledTransmonNetworkHamiltonian(
        anharmonicities_rad_per_us=(0.0, 0.0), detunings_rad_per_us=(0.0, 0.0), couplings=(),
        drives=tuple(drives), local_dimension=2)
    network = ExtendedTransmonNetworkHamiltonian(base, drive_crosstalk=tuple(
        DriveCrosstalkTransfer(index_of[t.source], index_of[t.target], t.transfer_real, t.transfer_imag,
                               t.detuning_rad_per_us) for t in transfers))
    peak = max((abs(d.envelope.peak_amplitude_rad_per_us) for d in drives), default=0.0)
    strongest = max([1.0] + [abs(t.coefficient) + 1.0 for t in transfers])
    fastest = max([peak * strongest] + [abs(t.detuning_rad_per_us) for t in transfers])
    edges = sorted({edge for drive in drives for edge in (drive.start_time_us, drive.end_time_us)})
    state = np.zeros(4, dtype=complex)
    state[0] = 1.0
    for begin, end in zip(edges, edges[1:]):
        steps = max(1, math.ceil((end - begin) * fastest / tolerance))
        edges_here = np.linspace(begin, end, steps + 1)
        for low, high in zip(edges_here[:-1], edges_here[1:]):
            hamiltonian = network.for_segment(low, high).evaluate_array(0.5 * (low + high))
            if not np.any(hamiltonian):
                continue
            values, vectors = np.linalg.eigh(hamiltonian)
            state = vectors @ (np.exp(-1j * (high - low) * values) * (vectors.conj().T @ state))
    return state


def kick_model(qubits: tuple[Qubit, Qubit], transfers: tuple[DriveCrosstalkTransfer, ...],
               shape: PulseShape = SQUARE_SHAPE, *, with_kicks: bool = True,
               frame_aware: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """Each qubit's final state in the hardware picture, evolved alone with the kicks it receives.

    ``frame_aware=False`` places the leaks as if the target had no program: hardware axes, no frame shift and
    no interaction picture, the mistake those two corrections fix.
    """
    windows = [[region.window(shape) for region in jobs] for jobs in qubits]
    states = []
    for index, jobs in enumerate(qubits):
        others = windows[1 - index]
        state = np.array([1.0, 0.0], dtype=complex)
        end_frame = 0.0
        for number, region in enumerate(jobs):
            operation, window = region.operation(), windows[index][number]
            leaks = ()
            if with_kicks:
                leaks = tuple(leak for other in others for leak in leaks_into(window, [other], transfers))
                previous = windows[index][number - 1] if number else None
                leaks += gap_leaks(others, (region.physical_id,), *gap_before(window, previous), transfers)
            plan = place_kicks(leaks, operation, window.schedule, window_start_us=region.start_us,
                               reset_duration_us=0.0,
                               layout_qubit_ids=(region.physical_id,) if frame_aware else ())
            pending = list(plan.per_shot[0])
            for position, instruction in enumerate(operation.instructions):
                while pending and pending[0].position <= position:
                    state = kick_unitary(pending.pop(0)) @ state
                for step in instruction.natives:
                    state = product_of((step,)) @ state
            for placed in pending + list(plan.trailing):
                state = kick_unitary(placed) @ state
            end_frame = frame_before(operation, len(operation.instructions), 0)
        states.append(_rotation_z(end_frame).conj().T @ state)
    return states[0], states[1]


def infidelities(qubits, transfers, shape: PulseShape = SQUARE_SHAPE, **model_options) -> tuple[float, float]:
    """1 - fidelity of each qubit's kick-model state against the pulse-level reduced state."""
    joint = pulse_reference(qubits, transfers, shape).reshape(2, 2)
    reduced = (joint @ joint.conj().T, joint.T @ joint.conj())
    models = kick_model(qubits, transfers, shape, **model_options)
    return tuple(float(1.0 - np.real(np.vdot(m, r @ m))) for m, r in zip(models, reduced))


def leak_transfer(c: float, *, phase: float = math.pi / 2.0, detuning: float = 0.0, back: float = 0.0,
                  attacker: int = 10, victim: int = 21) -> tuple[DriveCrosstalkTransfer, ...]:
    forward = (DriveCrosstalkTransfer(attacker, victim, c * math.cos(phase), c * math.sin(phase), detuning),)
    reverse = (DriveCrosstalkTransfer(victim, attacker, back, 0.0, detuning),) if back else ()
    return forward + reverse


def scenarios():
    """(label, qubits, transfers, shape, model options) for each case of the study; qubit 0 is the attacker."""
    idle_victim = (Region(21, 0.0, ((rz(0.9), sx()), 0.30, (rz(-0.4), sx(), rz(1.3)))),)
    busy_victim = (Region(21, 0.0, ((rz(0.9), sx()), (sx(),), (rz(-0.4), sx(), rz(1.3)))),)
    attacker_x = (Region(10, 0.10, ((x(),),)),)
    cases = [("no leak, virtual Z on both sides (convention check)",
              ((Region(10, 0.0, ((rz(0.7), sx(), rz(0.3)),)),), (Region(21, 0.0, ((rz(0.9), sx(), rz(-0.4)),)),)),
              (), SQUARE_SHAPE, {})]
    for c in (0.03, 0.1, 0.3):
        cases.append((f"victim idle, resonant square, |c|={c}", (attacker_x, idle_victim), leak_transfer(c),
                      SQUARE_SHAPE, {}))
    cases.append(("same, gaussian pulses, |c|=0.1", (attacker_x, idle_victim), leak_transfer(0.1), GAUSSIAN_SHAPE, {}))
    for detuning in (30.0, 100.0, 300.0):
        cases.append((f"victim idle, detuned {detuning:g} rad/us, square, |c|=0.3", (attacker_x, idle_victim),
                      leak_transfer(0.3, detuning=detuning), SQUARE_SHAPE, {}))
        cases.append((f"victim idle, detuned {detuning:g} rad/us, gaussian, |c|=0.3", (attacker_x, idle_victim),
                      leak_transfer(0.3, detuning=detuning), GAUSSIAN_SHAPE, {}))
    cases.append(("victim idle, frame and interaction picture left out", (attacker_x, idle_victim),
                  leak_transfer(0.1), SQUARE_SHAPE, {"frame_aware": False}))
    attacker_early = (Region(10, 0.05, ((x(),),)),)
    for c in (0.03, 0.1, 0.3):
        cases.append((f"victim pulsing at the same time, |c|={c}", (attacker_early, busy_victim), leak_transfer(c),
                      SQUARE_SHAPE, {}))
        cases.append((f"same, left out the interaction picture, |c|={c}", (attacker_early, busy_victim),
                      leak_transfer(c), SQUARE_SHAPE, {"frame_aware": False}))
    for offset in (0.0, 0.009, 0.018, 0.027):
        cases.append((f"pulses offset by {offset * 1000:.0f} ns, |c|=0.1",
                      ((Region(10, 0.036 + offset, ((x(),),)),), busy_victim), leak_transfer(0.1), SQUARE_SHAPE, {}))
    for c in (0.03, 0.1):
        cases.append((f"both directions, both windows cover the leaks, |c|={c}",
                      ((Region(10, 0.0, ((sx(),), (x(),), (sx(),))),), busy_victim), leak_transfer(c, back=c),
                      SQUARE_SHAPE, {}))
    cases.append(("both directions, a leak before the first window (gap from time 0), |c|=0.1",
                  ((Region(10, 0.05, ((sx(),), (x(),))),), busy_victim), leak_transfer(0.1, back=0.1),
                  SQUARE_SHAPE, {}))
    cases.append(("detuned 100 rad/us, gaussian, victim pulsing, |c|=0.1",
                  (attacker_early, busy_victim), leak_transfer(0.1, detuning=100.0), GAUSSIAN_SHAPE, {}))
    gap_victim = (Region(21, 0.0, ((sx(),), (sx(),))), Region(21, 0.40, ((rz(0.6), sx(), rz(-0.2)),)))
    attacker_in_gap = (Region(10, 0.20, ((x(),),)),)
    for c in (0.1, 0.3):
        cases.append((f"leak in the gap between two victim jobs, |c|={c}", (attacker_in_gap, gap_victim),
                      leak_transfer(c), SQUARE_SHAPE, {}))
    cases.append(("same, the gap leak left out", (attacker_in_gap, gap_victim), leak_transfer(0.3), SQUARE_SHAPE,
                  {"with_kicks": False}))
    return cases


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args()
    print(f"{'case':<64} {'err 0':>9} {'err 1':>9} {'leak 0':>9} {'leak 1':>9}")
    print("err = 1 - fidelity of the kick model against the pulse network; leak = the same with no kicks")
    for label, qubits, transfers, shape, options in scenarios():
        low, high = infidelities(qubits, transfers, shape, **options)
        leak_low, leak_high = infidelities(qubits, transfers, shape, **{**options, "with_kicks": False})
        print(f"{label:<64} {low:9.1e} {high:9.1e} {leak_low:9.1e} {leak_high:9.1e}")


if __name__ == "__main__":
    main()
