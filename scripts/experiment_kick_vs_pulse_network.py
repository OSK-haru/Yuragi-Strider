"""Compare the kick model of drive crosstalk with the pulse-level coupled-transmon network.

Reference ("hardware picture"): two uncoupled transmons, one per region, driven by square pulses read
from each region's activity record (a virtual Z shifts the carrier phase of later pulses), with the
declared ``DriveCrosstalkTransfer`` leaking each drive onto the other transmon. The Hamiltonian is
integrated exactly, segment by segment, with ``ExtendedTransmonNetworkHamiltonian``.

Kick model ("simulator picture"): each region evolves alone, as the runtime does it. The virtual Z is a
real rotation of its state, and the other region's drives arrive as kicks (``drive_crosstalk_kicks``
then ``place_kicks``). Its state is rotated back by the region's final frame to be compared.

The regions are uncoupled and the leak is a classical field, so both pictures keep the two transmons
in a product state. This measures the kick approximation against the project's own pulse model; it does
not test the pulse model against a real device.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
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
    DriveSchedule, ExecutionWindow, drive_crosstalk_kicks,
)
from core.attack_simulation.event_commit_boundary import EventPosition  # noqa: E402
from core.attack_simulation.kick_application import kick_unitary, place_kicks  # noqa: E402
from core.attack_simulation.native_pulses import NativeStep, product_of  # noqa: E402
from core.attack_simulation.shot_activity import frame_before, shot_activity  # noqa: E402
from core.experimental_isolation_physics import DriveCrosstalkTransfer, ExtendedTransmonNetworkHamiltonian  # noqa: E402
from core.pulse_envelopes import SquarePulseEnvelope  # noqa: E402
from core.pulse_transmon_network import CoupledTransmonNetworkHamiltonian, ScheduledTransmonDrive  # noqa: E402

PULSE_US = 0.036                      # a typical sx/x duration
SX = math.pi / 2.0
CLOCK = "world"


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

    def activity(self):
        return shot_activity(self.operation(), reset_duration_us=0.0, measurement_duration_us=0.0)

    def window(self) -> ExecutionWindow:
        shot = self.activity()
        return ExecutionWindow(
            f"job-{self.physical_id}", f"tenant-{self.physical_id}", (self.physical_id,),
            EventPosition(CLOCK, self.start_us, 0), EventPosition(CLOCK, self.start_us + shot.duration_us, 0),
            DriveSchedule(shot, 1))


def _rotation_z(angle: float) -> np.ndarray:
    return np.array([[cmath.exp(-0.5j * angle), 0.0], [0.0, cmath.exp(0.5j * angle)]], dtype=complex)


def pulse_reference(regions: tuple[Region, Region], transfers: tuple[DriveCrosstalkTransfer, ...]) -> np.ndarray:
    """The joint state vector of the two transmons (region 0 most significant), hardware picture."""
    index_of = {region.physical_id: index for index, region in enumerate(regions)}
    drives = []
    for index, region in enumerate(regions):
        for item in region.activity().of_qubit(0):
            if item.kind == "drive":
                drives.append(ScheduledTransmonDrive(
                    index, region.start_us + item.start_us,
                    SquarePulseEnvelope.from_target_rotation_angle(item.angle_rad, item.end_us - item.start_us),
                    phase_rad=item.phase_rad))
    base = CoupledTransmonNetworkHamiltonian(
        anharmonicities_rad_per_us=(0.0, 0.0), detunings_rad_per_us=(0.0, 0.0), couplings=(),
        drives=tuple(drives), local_dimension=2)
    network = ExtendedTransmonNetworkHamiltonian(base, drive_crosstalk=tuple(
        DriveCrosstalkTransfer(index_of[t.source], index_of[t.target], t.transfer_real, t.transfer_imag)
        for t in transfers))
    edges = sorted({edge for drive in drives for edge in (drive.start_time_us, drive.end_time_us)})
    state = np.zeros(4, dtype=complex)
    state[0] = 1.0
    for begin, end in zip(edges, edges[1:]):
        hamiltonian = network.for_segment(begin, end).evaluate_array(0.5 * (begin + end))
        values, vectors = np.linalg.eigh(hamiltonian)
        state = vectors @ (np.exp(-1j * (end - begin) * values) * (vectors.conj().T @ state))
    return state


def kick_model(regions: tuple[Region, Region], transfers: tuple[DriveCrosstalkTransfer, ...], *,
               frame_shift: bool = True, with_kicks: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """Each region's final state in the hardware picture, evolved alone with the kicks it receives.

    ``frame_shift=False`` leaves each kick's hardware phase as the axis (the mistake the frame shift fixes).
    """
    windows = tuple(region.window() for region in regions)
    states = []
    for index, region in enumerate(regions):
        other = 1 - index
        operation, window = region.operation(), windows[index]
        kicks = drive_crosstalk_kicks(windows[other], window, transfers) if with_kicks else ()
        plan = place_kicks(kicks, operation, window.schedule, window_start_us=region.start_us,
                           reset_duration_us=0.0, layout_qubit_ids=(region.physical_id,))
        placed = list(plan.per_shot[0]) + list(plan.trailing)
        if not frame_shift:
            placed = [replace(p, axis_rad=p.kick.phase_rad) for p in placed]
        state = np.array([1.0, 0.0], dtype=complex)
        pending = sorted(placed, key=lambda p: p.position)
        for position, instruction in enumerate(operation.instructions):
            while pending and pending[0].position <= position:
                state = kick_unitary(pending.pop(0)) @ state
            for step in instruction.natives:
                state = product_of((step,)) @ state
        for p in pending:
            state = kick_unitary(p) @ state
        end_frame = frame_before(operation, len(operation.instructions), 0)
        states.append(_rotation_z(end_frame).conj().T @ state)
    return states[0], states[1]


def infidelities(regions, transfers, **model_options) -> tuple[float, float]:
    """1 - fidelity of each region's kick-model state against the pulse-level reduced state."""
    joint = pulse_reference(regions, transfers).reshape(2, 2)
    reduced = (joint @ joint.conj().T, joint.T @ joint.conj())
    models = kick_model(regions, transfers, **model_options)
    return tuple(float(1.0 - np.real(np.vdot(m, r @ m))) for m, r in zip(models, reduced))


def _row(label: str, regions, transfers, **options) -> str:
    low, high = infidelities(regions, transfers, **options)
    return f"{label:<58} {low:10.2e} {high:10.2e}"


def _scenarios():
    """(label, regions, transfers, model options) for each case of the study; region 0 is the attacker."""
    def leak(c, phase=math.pi / 2.0, back=0.0):
        forward = (DriveCrosstalkTransfer(10, 21, c * math.cos(phase), c * math.sin(phase)),)
        reverse = (DriveCrosstalkTransfer(21, 10, back, 0.0),) if back else ()
        return forward + reverse

    cases = [("no leak, virtual Z on both sides (convention check)",
              (Region(10, 0.0, ((rz(0.7), sx(), rz(0.3)),)), Region(21, 0.0, ((rz(0.9), sx(), rz(-0.4)),))), (), {})]
    idle_victim = Region(21, 0.0, ((rz(0.9), sx()), 0.30, (rz(-0.4), sx(), rz(1.3))))
    attacker = Region(10, 0.10, ((x(),),))
    for c in (0.01, 0.03, 0.1, 0.3):
        cases.append((f"victim idle during the leak, one-way, |c|={c}", (attacker, idle_victim), leak(c), {}))
    cases.append(("same, the frame shift left out (negative control), |c|=0.1", (attacker, idle_victim), leak(0.1),
                  {"frame_shift": False}))
    cases.append(("same, leak phase 0 instead of pi/2, |c|=0.1", (attacker, idle_victim), leak(0.1, 0.0), {}))
    cases.append(("same, but no kicks at all (what the leak does), |c|=0.1", (attacker, idle_victim), leak(0.1),
                  {"with_kicks": False}))
    busy = Region(21, 0.0, ((rz(0.9), sx()), (sx(),), (rz(-0.4), sx(), rz(1.3))))
    for c in (0.01, 0.03, 0.1, 0.3):
        cases.append((f"victim pulsing at the same time, one-way, |c|={c}", (Region(10, 0.05, ((x(),),)), busy),
                      leak(c), {}))
    for offset in (0.0, 0.009, 0.018, 0.027):
        cases.append((f"pulses offset by {offset * 1000:.0f} ns, |c|=0.1", (Region(10, 0.036 + offset, ((x(),),)), busy),
                      leak(0.1), {}))
    for c in (0.03, 0.1):
        cases.append((f"both directions, victim also pulsing, |c|={c}", (Region(10, 0.05, ((sx(),), (x(),))), busy),
                      leak(c, back=c), {}))
    return cases


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args()
    print(f"{'case':<64} {'err 0':>9} {'err 1':>9} {'leak 0':>9} {'leak 1':>9}")
    print("err = 1 - fidelity of the kick model against the pulse network; leak = the same with no kicks")
    for label, regions, transfers, options in _scenarios():
        low, high = infidelities(regions, transfers, **options)
        leak_low, leak_high = infidelities(regions, transfers, **{**options, "with_kicks": False})
        print(f"{label:<64} {low:9.1e} {high:9.1e} {leak_low:9.1e} {leak_high:9.1e}")


if __name__ == "__main__":
    main()
