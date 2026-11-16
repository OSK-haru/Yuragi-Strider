"""A pulse a tenant plays directly: the provider's policy, its own qubit, and the leak it puts on a neighbour (D91)."""

import math
import unittest

import numpy as np

from core.attack_simulation.circuit_program import LocalCircuitCompiler
from core.attack_simulation.concurrent_lanes import ConcurrentRegions, execution_window, kicks_for
from core.attack_simulation.coupled_regions import CoupledTransmonSettings
from core.attack_simulation.direct_pulse import DirectPulse, DirectPulsePolicy, pulse_rotations, pulse_unitaries
from core.attack_simulation.kick_application import kick_unitary
from core.attack_simulation.program_request import CircuitProgram, ProgramRequestError, ProgramSubmitRequest
from core.attack_simulation.pulse_shape import GAUSSIAN_SHAPE, SQUARE_SHAPE, PulseShape
from core.attack_simulation.shot_activity import shot_activity
from core.capabilities import SUPPORTED_GATES
from core.circuit_model import CircuitConfig
from core.circuit_validation import validate_circuit_config
from core.experimental_isolation_physics.coherent_crosstalk import DriveCrosstalkTransfer
from core.experimental_isolation_physics.transmon_cluster import ClusterDrive, TransmonCluster, TransmonSpec
from core.attack_simulation.jobs import JobCommand
from core.attack_simulation.resource_binding import PhysicalQubitLayout
from tests.test_concurrent_regions import pos, run_in_order
from tests.test_coupled_regions import build, register
from tests.test_local_program_runtime import program_json

TWO_PI = 2.0 * math.pi
RESET_US, MEASURE_US, GATE_US = 1.0, 1.0, 0.02
POLICY = DirectPulsePolicy(max_frequency_offset_mhz=400.0, max_rabi_mhz=50.0, max_duration_us=2.0)
SIGMA = (np.array([[0, 1], [1, 0]], dtype=complex), np.array([[0, -1j], [1j, 0]], dtype=complex))


def compiler(policy=POLICY, qubits=(0, 1, 2, 3)):
    natives = {str(q): {"x": {"duration_us": GATE_US, "gate_error": 0.0},
                        "sx": {"duration_us": GATE_US, "gate_error": 0.0},
                        "rz": {"duration_us": 0.0, "gate_error": 0.0}} for q in qubits}
    return LocalCircuitCompiler(max_program_qubits=2, physical_qubit_ids=tuple(qubits),
                                single_qubit_native_gates=natives,
                                single_qubit_gate_decompositions={"X": ("x",), "H": ("rz", "sx", "rz")},
                                direct_pulse=policy)


def pulse_gate(target=0, **params):
    return {"type": "PULSE", "targets": [target], "params": params}


def compile_columns(columns, physical=(0, 1), policy=POLICY):
    request = ProgramSubmitRequest(CircuitProgram.from_editor_json(program_json(len(physical), columns)),
                                   "qpu-1", 1, "r")
    return compiler(policy).compile(request, sampling_seed=1, physical_qubit_ids=physical)


def channel(unitary):
    """The superoperator of a unitary, in the column-major convention of ``TransmonCluster.propagator``."""
    return np.kron(np.conj(unitary), unitary)


def reference_qubit(pulse, start_us):
    """The same pulse on a two-level transmon of the reference kernel, as a superoperator."""
    cluster = TransmonCluster((TransmonSpec(5.0, None, 2),))
    offset = pulse.frequency_offset_rad_per_us
    drive = ClusterDrive(0, start_us, pulse.duration_us, pulse.angle_rad, pulse.shape,
                         phase_rad=pulse.phase_rad + offset * start_us, frequency_offset_rad_per_us=offset)
    # The kernel's default slices leave about 1e-5 on a detuned Gaussian; it closes in 16-fold per halving.
    return cluster.propagator(start_us, start_us + pulse.duration_us, (drive,), step_angle_rad=0.125)


def exact_qubit(rate, begin, end, steps=20000):
    """Ordered product of a two-level qubit under ``H = 1/2 (Re rate sigma_x + Im rate sigma_y)``."""
    result = np.eye(2, dtype=complex)
    edges = np.linspace(begin, end, steps + 1)
    width = edges[1] - edges[0]
    for value in rate(0.5 * (edges[:-1] + edges[1:])):
        angle = abs(value) * width
        axis = (value.real * SIGMA[0] + value.imag * SIGMA[1]) / abs(value) if angle else np.eye(2)
        result = (math.cos(angle / 2) * np.eye(2) - 1j * math.sin(angle / 2) * axis) @ result
    return result


def infidelity(left, right):
    return 1.0 - abs(np.trace(left.conj().T @ right)) ** 2 / 4.0


class PolicyTests(unittest.TestCase):
    def test_without_a_policy_the_instruction_does_not_exist(self):
        columns = [[pulse_gate(angle_rad=1.0, duration_us=0.1)]]
        with self.assertRaisesRegex(ProgramRequestError, "does not accept direct pulses"):
            compile_columns(columns, policy=None)
        self.assertEqual(compile_columns(columns).instructions[1].kind, "unitary")

    def test_the_gate_aware_simulator_does_not_know_the_instruction(self):
        self.assertNotIn("PULSE", SUPPORTED_GATES)
        config = CircuitConfig.from_dict({"logical_qubits": 1, "initial_states": [0], "classical_bits": 0,
                                          "columns": [{"step": 0, "gates": [pulse_gate(angle_rad=1.0,
                                                                                         duration_us=0.1)]}]})
        self.assertEqual([issue.code for issue in validate_circuit_config(config)], ["INVALID_GATE_TYPE"])

    def test_each_limit_of_the_policy_is_enforced(self):
        for params, message in (
                ({"angle_rad": 1.0, "duration_us": 0.1, "frequency_offset_mhz": 401.0}, "400 MHz"),
                ({"angle_rad": 1.0, "duration_us": 0.1, "frequency_offset_mhz": -401.0}, "400 MHz"),
                ({"angle_rad": 1.0, "duration_us": 2.5}, "longer than 2 us"),
                ({"angle_rad": math.pi, "duration_us": 0.009}, "peak drive strength"),
                # The same area and length fit as a square pulse but not as a Gaussian, whose peak is higher.
                ({"angle_rad": math.pi, "duration_us": 0.0101, "gaussian_truncation_sigma": 2.0},
                 "peak drive strength")):
            with self.subTest(params=params), self.assertRaisesRegex(ProgramRequestError, message):
                compile_columns([[pulse_gate(**params)]])
        compile_columns([[pulse_gate(angle_rad=math.pi, duration_us=0.0101)]])
        compile_columns([[pulse_gate(angle_rad=1.0, duration_us=0.1, frequency_offset_mhz=-400.0)]])

    def test_malformed_pulses_are_rejected(self):
        for gate in (pulse_gate(angle_rad=1.0), pulse_gate(duration_us=0.1),
                     pulse_gate(angle_rad=1.0, duration_us=0.0), pulse_gate(angle_rad=1.0, duration_us=0.1, theta=1.0),
                     pulse_gate(angle_rad=1.0, duration_us=0.1, gaussian_truncation_sigma=0.0),
                     {"type": "PULSE", "targets": [0], "controls": [1],
                      "params": {"angle_rad": 1.0, "duration_us": 0.1}}):
            with self.subTest(gate=gate), self.assertRaises(ProgramRequestError):
                compile_columns([[gate]])

    def test_the_policy_is_a_frozen_part_of_the_compiler_profile(self):
        self.assertEqual(DirectPulsePolicy.from_config(POLICY.to_config()), POLICY)
        for bad in ({"max_frequency_offset_mhz": 1.0}, {**POLICY.to_config(), "max_rabi_mhz": 0.0},
                    {**POLICY.to_config(), "max_duration_us": -1.0}, {**POLICY.to_config(), "extra": 1.0}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                DirectPulsePolicy.from_config(bad)

    def test_a_pulse_takes_its_own_time_and_carries_no_calibrated_error(self):
        operation = compile_columns([[pulse_gate(0, angle_rad=1.0, duration_us=0.3), {"type": "X", "targets": [1]}]])
        pulse, gate = operation.instructions[1], operation.instructions[2]
        self.assertEqual((pulse.duration_us, pulse.intrinsic_us, pulse.depolarizing_probability), (0.3, 0.3, 0.0))
        self.assertEqual((gate.duration_us, gate.intrinsic_us), (0.0, GATE_US))


class OwnQubitTests(unittest.TestCase):
    def test_a_resonant_pulse_turns_the_qubit_about_the_axis_at_its_phase(self):
        for shape in (SQUARE_SHAPE, GAUSSIAN_SHAPE):
            unitary = DirectPulse(0.7, 0.05, phase_rad=0.4, shape=shape).unitary()
            axis = math.cos(0.4) * SIGMA[0] + math.sin(0.4) * SIGMA[1]
            np.testing.assert_allclose(unitary, math.cos(0.35) * np.eye(2) - 1j * math.sin(0.35) * axis, atol=1e-12)

    def test_the_two_level_evolution_equals_the_reference_kernel(self):
        for shape, offset_mhz, start in ((SQUARE_SHAPE, 7.0, 0.0), (SQUARE_SHAPE, -120.0, 3.7),
                                         (GAUSSIAN_SHAPE, 7.0, 3.7), (GAUSSIAN_SHAPE, -40.0, 12.3),
                                         (PulseShape("gaussian", 3.0), 15.0, 0.4)):
            pulse = DirectPulse(2.1, 0.08, phase_rad=0.6, frequency_offset_rad_per_us=TWO_PI * offset_mhz,
                                shape=shape)
            with self.subTest(shape=shape.kind, offset_mhz=offset_mhz):
                np.testing.assert_allclose(channel(pulse.unitary()), reference_qubit(pulse, start), atol=2e-7)

    def test_the_path_inside_a_pulse_is_the_pulse_cut_there(self):
        angle, duration, offset = 1.9, 0.06, TWO_PI * 22.0
        fractions = np.array([0.0, 0.13, 0.5, 0.77, 1.0])
        path = pulse_unitaries(angle, duration, offset, SQUARE_SHAPE, fractions)
        for fraction, unitary in zip(fractions[1:], path[1:]):
            cut = pulse_unitaries(fraction * angle, fraction * duration, offset, SQUARE_SHAPE, (1.0,))[0]
            np.testing.assert_allclose(unitary, cut, atol=1e-12)
        np.testing.assert_allclose(path[0], np.eye(2), atol=1e-12)
        # A Gaussian has no such closed form: compare the path with a fine ordered product.
        path = pulse_unitaries(angle, duration, offset, GAUSSIAN_SHAPE, fractions)
        rate = lambda t: angle / duration * GAUSSIAN_SHAPE.density(t / duration) * np.exp(-1j * offset * t)  # noqa: E731
        for fraction, unitary in zip(fractions[1:], path[1:]):
            self.assertLess(infidelity(exact_qubit(rate, 0.0, fraction * duration), unitary), 1e-9)

    def test_rotations_are_the_unitaries_on_the_bloch_vector(self):
        fractions = (0.3, 1.0)
        unitaries = pulse_unitaries(1.2, 0.05, TWO_PI * 9.0, GAUSSIAN_SHAPE, fractions, 0.2)
        rotations = pulse_rotations(1.2, 0.05, TWO_PI * 9.0, GAUSSIAN_SHAPE, fractions, 0.2)
        paulis = (*SIGMA, np.diag([1.0, -1.0]).astype(complex))
        bloch = np.array([0.3, -0.5, 0.6])
        state = 0.5 * (np.eye(2) + sum(c * p for c, p in zip(bloch, paulis)))
        for unitary, rotation in zip(unitaries, rotations):
            turned = unitary @ state @ unitary.conj().T
            np.testing.assert_allclose([np.trace(turned @ p).real for p in paulis], rotation @ bloch, atol=1e-12)
            np.testing.assert_allclose(rotation @ rotation.T, np.eye(3), atol=1e-12)

    def test_the_compiled_matrix_and_the_activity_record_describe_the_same_pulse(self):
        params = {"angle_rad": 1.1, "duration_us": 0.05, "phase_rad": 0.3, "frequency_offset_mhz": 12.0,
                  "gaussian_truncation_sigma": 2.5}
        operation = compile_columns([[{"type": "H", "targets": [0]}], [pulse_gate(0, **params)]])
        instruction = operation.instructions[2]
        pulse = DirectPulse.from_params(params)
        np.testing.assert_allclose(np.array(instruction.unitary), pulse.unitary(), atol=1e-14)
        self.assertEqual([step.name for step in instruction.natives], ["frame", "pulse", "frame"])
        shot = shot_activity(operation, reset_duration_us=RESET_US, measurement_duration_us=MEASURE_US)
        gate_drive, played = [item for item in shot.of_qubit(0) if item.kind == "drive"]
        frame_before = sum(step.angle_rad for step in operation.instructions[1].natives if step.is_virtual)
        self.assertEqual((gate_drive.frequency_offset_rad_per_us, gate_drive.shape), (0.0, SQUARE_SHAPE))
        # The program states the phase in its own frame; the line carries it minus the earlier virtual Z.
        self.assertAlmostEqual(played.phase_rad, 0.3 - frame_before)
        self.assertAlmostEqual(played.frequency_offset_rad_per_us, TWO_PI * 12.0)
        self.assertEqual((played.angle_rad, played.shape), (1.1, PulseShape("gaussian", 2.5)))
        self.assertAlmostEqual(played.end_us - played.start_us, 0.05)
        # The frame shifts of the pulse cancel: later gates see the frame the H left.
        self.assertAlmostEqual(sum(item.angle_rad for item in shot.of_qubit(0) if item.kind == "frame"), frame_before)


class NeighbourTests(unittest.TestCase):
    """The leak of a direct pulse onto the other region, against the reference kernel."""

    FREQUENCIES = (5.0, 5.1)                         # the source (physical 1) and the receiver (physical 2)
    DETUNING = TWO_PI * 1e3 * (FREQUENCIES[1] - FREQUENCIES[0])
    COEFFICIENT = 0.05 + 0.02j
    START_US = 10.0

    def kick(self, pulse_params, shape=SQUARE_SHAPE, delay_us=0.0):
        """The receiver's rotation, from the windows of the two jobs, and the absolute start of the pulse."""
        source_columns = ([[{"type": "I", "targets": [1], "params": {"duration_us": delay_us}}]] if delay_us else [])
        source_operation = compile_columns(source_columns + [[pulse_gate(1, **pulse_params)]], (0, 1))
        target_operation = compile_columns([[{"type": "I", "targets": [0], "params": {"duration_us": 5.0}}]], (2, 3))
        common = dict(reset_duration_us=RESET_US, measurement_duration_us=MEASURE_US, repetition_delay_us=0.0,
                      pulse_shape=shape)
        source = execution_window("s", "attacker", source_operation, (0, 1), (0, 1), pos(self.START_US),
                                  pos(self.START_US + 20.0, 1), **common)
        target = execution_window("t", "victim", target_operation, (2, 3), (2, 3), pos(self.START_US, 2),
                                  pos(self.START_US + 20.0, 3), **common)
        transfer = DriveCrosstalkTransfer(1, 2, self.COEFFICIENT.real, self.COEFFICIENT.imag, self.DETUNING)
        plan = kicks_for(target, None, [source], ConcurrentRegions((transfer,)), target_operation, (2, 3),
                         RESET_US, MEASURE_US)
        result = np.eye(2, dtype=complex)
        for placed in plan.per_shot[0]:
            self.assertEqual(placed.qubit, 2)
            result = kick_unitary(placed) @ result
        return result, self.START_US + RESET_US + delay_us

    def reference(self, pulse, start_us):
        """The receiver's evolution over the pulse, from two uncoupled two-level transmons and the classical leak."""
        cluster = TransmonCluster(tuple(TransmonSpec(f, None, 2) for f in self.FREQUENCIES),
                                  line_transfers=((0, 1, self.COEFFICIENT),))
        offset = pulse.frequency_offset_rad_per_us
        drive = ClusterDrive(0, start_us, pulse.duration_us, pulse.angle_rad, pulse.shape,
                             phase_rad=pulse.phase_rad + offset * start_us, frequency_offset_rad_per_us=offset)
        return cluster.propagator(start_us, start_us + pulse.duration_us, (drive,), step_angle_rad=0.125)

    def check(self, params, shape=SQUARE_SHAPE, delay_us=0.0, atol=1e-6):
        kick, start = self.kick(params, shape, delay_us)
        pulse = DirectPulse.from_params(params)
        propagator = self.reference(pulse, start)
        own = pulse.unitary()
        for receiver in (np.array([1.0, 0.0]), np.array([1.0, 1.0]) / math.sqrt(2.0),
                         np.array([1.0, 1.0j]) / math.sqrt(2.0)):
            state = np.kron(np.array([1.0, 0.0]), receiver)                 # the source's transmon is index 0
            joint = (propagator @ np.outer(state, state.conj()).reshape(-1, order="F")).reshape(4, 4, order="F")
            reduced = np.einsum("abac->bc", joint.reshape(2, 2, 2, 2))
            np.testing.assert_allclose(kick @ np.outer(receiver, receiver.conj()) @ kick.conj().T, reduced, atol=atol)
            # The source's own qubit in the same reference run is what the compiled matrix says.
            np.testing.assert_allclose(np.einsum("abcb->ac", joint.reshape(2, 2, 2, 2)),
                                       own[:, :1] @ own[:, :1].conj().T, atol=2e-7)
        return kick

    def test_a_pulse_at_the_neighbours_frequency_turns_it_by_the_leak_coefficient(self):
        params = {"angle_rad": 2.0, "duration_us": 0.1, "phase_rad": 0.3, "frequency_offset_mhz": 100.0}
        kick = self.check(params)
        angle = 2.0 * math.acos(min(1.0, abs(kick[0, 0])))
        self.assertAlmostEqual(angle, 2.0 * abs(self.COEFFICIENT), places=9)
        # The same pulse on the source's own frequency is 100 MHz off the neighbour and hardly moves it.
        # (The kick is a second-order expansion cut into at most 32 slices: 1e-4 is what that leaves here.)
        far = self.check({**params, "frequency_offset_mhz": 0.0}, atol=1e-4)
        self.assertLess(2.0 * math.acos(min(1.0, abs(far[0, 0]))), 0.05 * angle)

    def test_the_carrier_phase_is_counted_from_the_start_of_the_pulse(self):
        params = {"angle_rad": 2.0, "duration_us": 0.1, "frequency_offset_mhz": 100.0}
        first = self.check(params)
        later = self.check(params, delay_us=0.0137)
        # Seen from the neighbour, the source's frame has turned by the frequency difference meanwhile.
        turn = self.DETUNING * 0.0137
        expected = np.diag([np.exp(-0.5j * turn), np.exp(0.5j * turn)])
        self.assertLess(infidelity(expected @ first @ expected.conj().T, later), 1e-12)

    def test_a_partly_detuned_gaussian_pulse_matches_the_reference(self):
        # The leak turns the receiver by 0.16 rad while its axis turns by 3.8 rad. The reference agrees with a
        # fine ordered product to 1e-11; the 2e-4 left is the kick's third-order term.
        self.check({"angle_rad": 3.0, "duration_us": 0.2, "phase_rad": -0.4, "frequency_offset_mhz": 97.0,
                    "gaussian_truncation_sigma": 2.0}, atol=5e-4)
        self.check({"angle_rad": 3.0, "duration_us": 0.2, "frequency_offset_mhz": 103.0}, GAUSSIAN_SHAPE,
                   delay_us=0.31, atol=5e-4)

    def test_a_leak_onto_a_qubit_playing_its_own_detuned_pulse_is_carried_along_its_path(self):
        """The receiver plays a pulse off its frequency while a leak lands on it: kick, then gate, is the pair."""
        own_params = {"angle_rad": 1.7, "duration_us": 0.1, "phase_rad": 0.5, "frequency_offset_mhz": 6.0,
                      "gaussian_truncation_sigma": 2.0}
        leak_params = {"angle_rad": 0.6, "duration_us": 0.1, "phase_rad": 1.1, "frequency_offset_mhz": 100.0}
        source_operation = compile_columns([[pulse_gate(1, **leak_params)]], (0, 1))
        target_operation = compile_columns([[pulse_gate(0, **own_params)]], (2, 3))
        common = dict(reset_duration_us=RESET_US, measurement_duration_us=MEASURE_US, repetition_delay_us=0.0,
                      pulse_shape=SQUARE_SHAPE)
        source = execution_window("s", "attacker", source_operation, (0, 1), (0, 1), pos(self.START_US),
                                  pos(self.START_US + 20.0, 1), **common)
        target = execution_window("t", "victim", target_operation, (2, 3), (2, 3), pos(self.START_US, 2),
                                  pos(self.START_US + 20.0, 3), **common)
        transfer = DriveCrosstalkTransfer(1, 2, self.COEFFICIENT.real, self.COEFFICIENT.imag, self.DETUNING)
        plan = kicks_for(target, None, [source], ConcurrentRegions((transfer,)), target_operation, (2, 3),
                         RESET_US, MEASURE_US)
        (placed,) = plan.per_shot[0]
        self.assertEqual(placed.position, 1)                       # before the receiver's own pulse
        own, leak = DirectPulse.from_params(own_params), DirectPulse.from_params(leak_params)
        begin = self.START_US + RESET_US

        def rate(t):
            mine = (own.angle_rad / own.duration_us * own.shape.density((t - begin) / own.duration_us)
                    * np.exp(1j * (own.phase_rad - own.frequency_offset_rad_per_us * (t - begin))))
            theirs = (self.COEFFICIENT * leak.angle_rad / leak.duration_us
                      * np.exp(1j * (leak.phase_rad - leak.frequency_offset_rad_per_us * (t - begin)
                                     + self.DETUNING * t)))
            return mine + theirs

        exact = exact_qubit(rate, begin, begin + own.duration_us)
        together = np.array(target_operation.instructions[1].unitary) @ kick_unitary(placed)
        self.assertLess(infidelity(exact, together), 1e-8)
        self.assertGreater(infidelity(exact, np.array(target_operation.instructions[1].unitary)), 1e-5)


class RuntimeTests(unittest.TestCase):
    """The whole path: a submitted program with a direct pulse, two regions running side by side."""

    def victim_counts(self, offset_mhz):
        transfer = DriveCrosstalkTransfer(2, 1, 0.5, 0.0, -TWO_PI * 100.0)        # physical 1 lies 100 MHz below 2
        runtime, executor = build(transfers=(transfer,))
        victim_op = compile_columns([[{"type": "I", "targets": [1], "params": {"duration_us": 5.0}}]], (0, 1))
        attacker_op = compile_columns([[pulse_gate(0, angle_rad=TWO_PI, duration_us=0.1,
                                                   frequency_offset_mhz=offset_mhz)]], (2, 3))
        victim = runtime.submit(JobCommand("victim", victim_op, "v", layout=PhysicalQubitLayout("qpu-1", (0, 1))))
        attacker = runtime.submit(JobCommand("attacker", attacker_op, "a",
                                             layout=PhysicalQubitLayout("qpu-1", (2, 3))))
        register(executor, victim, 1.0, 0)
        register(executor, attacker, 1.0, 4)
        run_in_order(executor)
        own = runtime.scheduler.get(attacker.job_id).result["counts"]
        return runtime.scheduler.get(victim.job_id).result["counts"], own

    def test_a_pulse_shifted_onto_the_victims_frequency_flips_the_victim_through_the_leak(self):
        # Half of a 2 pi pulse reaches the victim: a pi rotation, when the carrier sits on the victim.
        victim, attacker = self.victim_counts(-100.0)
        self.assertEqual({bits: n for bits, n in victim.items() if n}, {"01": 1})
        # 100 MHz off its own transition, the attacker's qubit stays where it was.
        self.assertEqual({bits: n for bits, n in attacker.items() if n}, {"00": 1})

    def test_the_same_pulse_on_the_attackers_own_frequency_leaves_the_victim_alone(self):
        victim, attacker = self.victim_counts(0.0)
        self.assertEqual({bits: n for bits, n in victim.items() if n}, {"00": 1})
        self.assertEqual({bits: n for bits, n in attacker.items() if n}, {"00": 1})       # a full turn


class DeviceValueTests(unittest.TestCase):
    def test_a_transfers_detuning_must_agree_with_the_declared_frequencies(self):
        transmons = CoupledTransmonSettings(((1, TransmonSpec(5.0, -330.0)), (2, TransmonSpec(5.1, -310.0))),
                                            ((1, 2, TWO_PI * 2.0),))
        right = DriveCrosstalkTransfer(1, 2, 0.05, 0.0, TWO_PI * 100.0)
        back = DriveCrosstalkTransfer(2, 1, 0.05, 0.0, -TWO_PI * 100.0)
        ConcurrentRegions((right, back), transmons=transmons)
        for wrong in (DriveCrosstalkTransfer(1, 2, 0.05, 0.0, 0.0), DriveCrosstalkTransfer(1, 2, 0.05, 0.0, -TWO_PI * 100.0)):
            with self.assertRaisesRegex(ValueError, "declared frequency"):
                ConcurrentRegions((wrong,), transmons=transmons)
        # A qubit without device values keeps the detuning its transfer declares.
        ConcurrentRegions((DriveCrosstalkTransfer(0, 2, 0.05, 0.0, 1.0),), transmons=transmons)


if __name__ == "__main__":
    unittest.main()
