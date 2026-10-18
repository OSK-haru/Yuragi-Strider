"""Reading a compiled Program into timed activity records, with the virtual-Z phase of each pulse."""

import json
import math
import unittest

import numpy as np

from api.attack_product_profiles import product_free_scenarios
from api.attack_settings_resolver import ScenarioSettingsResolver
from attack_client.circuit import Circuit
from core.attack_simulation.circuit_program import LocalCircuitCompiler
from core.attack_simulation.declared_service_timing import compiled_job_duration
from core.attack_simulation.jobs import JobCommand, JobRecord, JobStatus
from core.attack_simulation.resource_binding import PhysicalQubitLayout
from core.attack_simulation.native_pulses import (
    NativeStep, UnsupportedNativeSequence, product_of, solve_native_steps,
)
from core.attack_simulation.program_request import CircuitProgram, ProgramSubmitRequest
from core.attack_simulation.shared_qpu_runtime import SharedQpuRuntimeConfig
from core.attack_simulation.shot_activity import ActivityInterval, shot_activity
from core.circuit_model import GateOperation
from core.gates import gate_unitary
from tests.test_interleaved_product import settings_request

RESET_US, MEASURE_US = 1.0, 1.0
_MANIFEST = ScenarioSettingsResolver(free_scenarios=product_free_scenarios()).resolve(settings_request({})).establish()
COMPILER = LocalCircuitCompiler.from_manifest(_MANIFEST)
IDS = tuple(_MANIFEST.qpu_service_config["retained_region"]["physical_qubit_ids"])
# Region (2,3,4,5,16,23): logical 0-1, 1-2, 1-4, 2-3, 4-5 are the coupled pairs.


def compile_circuit(circuit, shots=1):
    program = CircuitProgram.from_editor_json(json.dumps(circuit.to_dict()))
    return COMPILER.compile(ProgramSubmitRequest(program, "ibm_kingston", shots, "k", "prepare"),
                            sampling_seed=1, physical_qubit_ids=IDS)


def activity(circuit):
    return shot_activity(compile_circuit(circuit), reset_duration_us=RESET_US, measurement_duration_us=MEASURE_US)


def drives(shot, qubit):
    return [item for item in shot.of_qubit(qubit) if item.kind == "drive"]


def logical_unitary(shot, qubit):
    """Rebuild the gate from the hardware view: pulses at their carrier phase, then the final frame."""
    matrix, frame = np.eye(2, dtype=complex), 0.0
    for item in shot.of_qubit(qubit):
        if item.kind == "frame":
            frame += item.angle_rad
        elif item.kind == "drive":
            c, s, phase = math.cos(item.angle_rad / 2), math.sin(item.angle_rad / 2), item.phase_rad
            axis = np.array([[0, np.exp(-1j * phase)], [np.exp(1j * phase), 0]])
            matrix = (c * np.eye(2) - 1j * s * axis) @ matrix
    rz = np.diag([np.exp(-0.5j * frame), np.exp(0.5j * frame)])
    return rz @ matrix


def same_up_to_phase(left, right):
    return abs(abs(np.trace(left.conj().T @ right)) / 2.0 - 1.0) < 1e-9


class NativeStepTests(unittest.TestCase):
    TABLE = {"Z": ("rz",), "S": ("rz",), "T": ("rz",), "RZ": ("rz",), "X": ("x",), "H": ("rz", "sx", "rz"),
             "Y": ("x", "rz"), "RX": ("rz", "sx", "rz", "sx", "rz"), "RY": ("rz", "sx", "rz", "sx", "rz")}

    def test_every_declared_gate_is_reproduced_by_its_native_steps(self):
        for name, sequence in self.TABLE.items():
            for theta in ([None] if name in "ZSTXHY" else [0.0, 0.3, 1.0, math.pi / 2, math.pi, -2.2, 5.9]):
                gate = GateOperation(name, [0], params=None if theta is None else {"theta_rad": theta})
                unitary = np.array(gate_unitary(gate, 1))
                steps = solve_native_steps(unitary, sequence, {"sx": 0.03, "x": 0.04})
                self.assertTrue(same_up_to_phase(product_of(steps), unitary), (name, theta))

    def test_an_unsolvable_sequence_is_refused_not_guessed(self):
        unitary = np.array(gate_unitary(GateOperation("RX", [0], params={"theta_rad": 0.4}), 1))
        with self.assertRaises(UnsupportedNativeSequence):
            solve_native_steps(unitary, ("rz", "sx", "rz"), {"sx": 0.03})   # needs two pulses
        with self.assertRaises(UnsupportedNativeSequence):
            solve_native_steps(unitary, ("sx", "sx"), {"sx": 0.03})
        self.assertEqual(NativeStep("rz", 1.0, 0.0).name, "rz")


class ShotActivityTests(unittest.TestCase):
    def test_the_shot_duration_matches_what_the_planner_charges(self):
        circuits = [
            Circuit(2).x(0).h(1).cnot(0, 1).measure_all(),
            Circuit(3).rx(0, 0.7).ry(1, 1.9).delay(2, 3.0).cz(0, 1).measure_all(),
            Circuit(1).h(0).measure(0, 0),
        ]
        config = SharedQpuRuntimeConfig(reset_model_id="m", reset_model_configuration_id="c",
                                        reset_duration_us=RESET_US, program_measurement_duration_us=MEASURE_US)
        for circuit in circuits:
            operation = compile_circuit(circuit, shots=3)
            shot = shot_activity(operation, reset_duration_us=RESET_US, measurement_duration_us=MEASURE_US)
            command = JobCommand("t", operation, "timing-test", layout=PhysicalQubitLayout("qpu-test", IDS))
            job = JobRecord("job-test", 1, command, JobStatus.QUEUED, (JobStatus.QUEUED,))
            self.assertAlmostEqual(compiled_job_duration(job, config, "t"), 3 * shot.duration_us)

    def test_gates_of_one_layer_start_together_and_a_dependent_gate_starts_after(self):
        shot = activity(Circuit(2).x(0).h(1).x(0).measure_all())
        first_x, second_x = drives(shot, 0)
        (h_pulse,) = drives(shot, 1)
        self.assertEqual(first_x.start_us, h_pulse.start_us)            # same layer, different qubits
        self.assertAlmostEqual(second_x.start_us, first_x.end_us)       # same qubit, next layer
        self.assertEqual(shot.of_qubit(0)[0].kind, "reset")             # the register Reset comes first
        self.assertGreaterEqual(first_x.start_us, RESET_US)

    def test_the_virtual_z_phase_of_each_pulse_matches_the_simulated_gate(self):
        for build in (lambda c: c.h(0), lambda c: c.x(0), lambda c: c.rx(0, 0.7), lambda c: c.ry(0, 2.3),
                      lambda c: c.rz(0, 1.1).x(0), lambda c: c.x(0).rz(0, 0.4).h(0).ry(0, -1.0)):
            circuit = build(Circuit(1)).measure(0, 0)
            operation = compile_circuit(circuit)
            shot = shot_activity(operation, reset_duration_us=RESET_US, measurement_duration_us=MEASURE_US)
            expected = np.eye(2, dtype=complex)
            for item in operation.instructions:
                if item.kind == "unitary":
                    expected = np.array(item.unitary) @ expected
            self.assertTrue(same_up_to_phase(logical_unitary(shot, 0), expected))

    def test_a_virtual_z_before_a_pulse_shifts_its_carrier_phase(self):
        shot = activity(Circuit(1).rz(0, 0.5).x(0).measure(0, 0))
        (pulse,) = drives(shot, 0)
        self.assertAlmostEqual(pulse.phase_rad, -0.5)
        self.assertAlmostEqual(pulse.angle_rad, math.pi)

    def test_a_delay_is_an_idle_interval_that_starts_with_its_layer(self):
        shot = activity(Circuit(2).x(0).delay(1, 5.0).measure_all())
        (pulse,) = drives(shot, 0)
        (idle,) = [item for item in shot.of_qubit(1) if item.kind == "idle"]
        self.assertEqual(idle.start_us, pulse.start_us)
        self.assertAlmostEqual(idle.end_us - idle.start_us, 5.0)

    def test_measurements_are_simultaneous_and_last(self):
        shot = activity(Circuit(3).x(0).measure_all())
        measures = [item for item in shot.intervals if item.kind == "measure"]
        self.assertEqual(len(measures), 3)
        self.assertEqual(len({item.start_us for item in measures}), 1)
        self.assertAlmostEqual(measures[0].end_us, shot.duration_us)

    def test_a_coupled_two_qubit_gate_marks_both_qubits(self):
        shot = activity(Circuit(2).cz(0, 1).measure_all())
        pairs = [item for item in shot.intervals if item.kind == "two_qubit"]
        self.assertEqual(sorted(item.qubit for item in pairs), [0, 1])
        self.assertEqual(len({(item.start_us, item.end_us) for item in pairs}), 1)

    def test_records_validate_their_values(self):
        with self.assertRaises(ValueError):
            ActivityInterval(0, "nope", 0.0, 1.0)
        with self.assertRaises(ValueError):
            ActivityInterval(0, "drive", 2.0, 1.0)
        with self.assertRaises(ValueError):
            ActivityInterval(0, "frame", 0.0, 1.0)


if __name__ == "__main__":
    unittest.main()
