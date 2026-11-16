"""Coupled qubits advanced at pulse level in the joint world (``solver="reference"``, D91)."""

import math
import unittest

import numpy as np

from core.attack_simulation.circuit_program import LocalCircuitCompiler
from core.attack_simulation.concurrent_lanes import ConcurrentRegions
from core.attack_simulation.coupled_regions import CoupledRegionWorld, CoupledTransmonSettings, RegionSide, WorldJob
from core.attack_simulation.direct_pulse import DirectPulsePolicy
from core.attack_simulation.jobs import JobCommand
from core.attack_simulation.program_request import CircuitProgram, ProgramSubmitRequest
from core.attack_simulation.pulse_shape import GAUSSIAN_SHAPE, SQUARE_SHAPE
from core.attack_simulation.resource_binding import PhysicalQubitLayout
from core.experimental_isolation_physics import ImperfectResetSpec, PhenomenologicalResetModel, ResetModelRegistry
from core.experimental_isolation_physics.coherent_crosstalk import DriveCrosstalkTransfer
from core.experimental_isolation_physics.idle_evolution import (
    IdleQubitSpec, IndependentIdleModel, relax_axes_in_place,
)
from core.experimental_isolation_physics.pulse_calibration import (
    block_infidelity, calibrate_pulse, qubit_block, relaxation_error, residual_survival, rotation_x,
)
from core.experimental_isolation_physics.transmon_cluster import ClusterDrive, TransmonCluster, TransmonSpec
from core.pulse_transmon_network import TransmonExchangeCoupling
from tests.test_concurrent_regions import run_in_order
from tests.test_coupled_regions import build, register
from tests.test_local_program_runtime import program_json

TWO_PI = 2.0 * math.pi
GATE_US = 0.036
SPECS = (TransmonSpec(5.0, -330.0), TransmonSpec(5.1, -310.0))     # physical 0 (region A), 1 (region B)
J = TWO_PI * 2.0
T1, TPHI = 30.0, 50.0
POLICY = DirectPulsePolicy(max_frequency_offset_mhz=200.0, max_rabi_mhz=200.0, max_duration_us=2.0)


def compiler(qubits=(0, 1, 2, 3), gate_error=0.0, policy=POLICY):
    natives = {str(q): {"x": {"duration_us": GATE_US, "gate_error": gate_error},
                        "sx": {"duration_us": GATE_US, "gate_error": gate_error},
                        "rz": {"duration_us": 0.0, "gate_error": 0.0}} for q in qubits}
    return LocalCircuitCompiler(max_program_qubits=2, physical_qubit_ids=tuple(qubits),
                                single_qubit_native_gates=natives,
                                single_qubit_gate_decompositions={"X": ("x",), "H": ("rz", "sx", "rz")},
                                direct_pulse=policy)


def compile_on(columns, physical, gate_error=0.0, seed=1):
    request = ProgramSubmitRequest(CircuitProgram.from_editor_json(program_json(len(physical), columns)),
                                   "qpu-1", 1, f"r{seed}")
    return compiler(gate_error=gate_error).compile(request, sampling_seed=seed, physical_qubit_ids=physical)


def wait(target, duration_us):
    return [{"type": "I", "targets": [target], "params": {"duration_us": duration_us}}]


def sides(levels=3, t1=T1, tphi=TPHI):
    model = PhenomenologicalResetModel(ImperfectResetSpec(1.0), observed_zero_given_higher=(0.0,) * (levels - 2))
    registry = ResetModelRegistry()
    registry.register(model)
    idle = IndependentIdleModel((IdleQubitSpec(t1, tphi, 0.0, levels=levels),), 1)
    return (RegionSide((0,), idle, registry, model.model_id, 0.0, 1.0),
            RegionSide((1,), idle, registry, model.model_id, 0.0, 1.0))


def settings(solver="reference", specs=SPECS):
    return CoupledTransmonSettings(((0, specs[0]), (1, specs[1])), ((0, 1, J),), solver=solver, model="exact")


def ground(levels=3):
    state = np.zeros((levels, levels), dtype=complex)
    state[0, 0] = 1.0
    return state


def world_of(solver="reference", transfers=(), shape=GAUSSIAN_SHAPE, **side_options):
    levels = side_options.get("levels", 3)
    specs = SPECS if levels == 3 else tuple(TransmonSpec(spec.frequency_ghz, None, 2) for spec in SPECS)
    return CoupledRegionWorld(sides(**side_options), settings(solver, specs), (ground(levels), ground(levels)),
                              repetition_delay_us=0.0, transfers=transfers, pulse_shape=shape)


def joint(world):
    a, b = world.factors
    return np.einsum("kij,klm->iljm", a, b).reshape(a.shape[1] * b.shape[1], -1)


def run(world, columns_a, columns_b, until, start_a=0.25, start_b=0.2):
    """B starts first: its initializing reset must not land inside A's pulses (it would project what they do to B)."""
    jobs = (WorldJob("a", 0, compile_on(columns_a + [wait(0, 50.0)], (0,)), (0,), start_a, 99.0),
            WorldJob("b", 1, compile_on(columns_b + [wait(0, 50.0)], (1,)), (0,), start_b, 99.0))
    world.advance_to(until, jobs)
    world.settle(0, until)
    world.settle(1, until)
    return joint(world)


def vec(matrix):
    return matrix.reshape(-1, order="F")


def unvec(vector):
    dimension = int(round(math.sqrt(len(vector))))
    return vector.reshape(dimension, dimension, order="F")


class IntegrationTests(unittest.TestCase):
    def test_overlapping_calibrated_pulses_match_the_kernel_run_over_the_whole_schedule(self):
        """Both regions play an X on their coupled qubit, overlapping in time, with relaxation throughout."""
        until = 1.0
        world = world_of()
        state = run(world, [[{"type": "X", "targets": [0]}]], [wait(0, 0.07), [{"type": "X", "targets": [0]}]], until)
        cluster = TransmonCluster(SPECS, (TransmonExchangeCoupling(0, 1, J),))
        drives = []
        for line, start in ((0, 0.25), (1, 0.27)):
            calibration = calibrate_pulse(cluster, line, math.pi, GATE_US, GAUSSIAN_SHAPE)
            drives.append(ClusterDrive(line, start, GATE_US, calibration.amplitude_scale * math.pi, GAUSSIAN_SHAPE,
                                       drag_us=calibration.drag_us))
        relaxation = (IdleQubitSpec(T1, TPHI, 0.0, levels=3),) * 2
        initial = vec(np.kron(ground(), ground()))
        # The plumbing (cuts, frames, factorization, calibration, relaxation masks) adds nothing to the kernel's map.
        np.testing.assert_allclose(state, unvec(cluster.propagator(0.0, until, drives[::-1], relaxation) @ initial),
                                   atol=1e-12)
        # Against a finer, unsplit solution: the default slices leave ~3e-7 on two overlapping carriers.
        fine = unvec(cluster.propagator(0.0, until, drives, relaxation, exact_relaxation=True, step_angle_rad=0.125)
                     @ initial)
        np.testing.assert_allclose(state, fine, atol=5e-7)
        self.assertGreater(state[4, 4].real, 0.95)                          # |11>

    def test_a_calibrated_x_is_an_x_up_to_its_leakage(self):
        world = world_of(t1=math.inf, tphi=math.inf)
        state = run(world, [[{"type": "X", "targets": [0]}]], [], 0.5)
        cluster = TransmonCluster(SPECS, (TransmonExchangeCoupling(0, 1, J),))
        calibration = calibrate_pulse(cluster, 0, math.pi, GATE_US, GAUSSIAN_SHAPE)
        self.assertLess(calibration.infidelity, 1e-4)
        self.assertAlmostEqual(calibration.infidelity, calibration.leakage, delta=1e-6)    # no phase error left
        populations = np.diag(state).real.reshape(3, 3)
        self.assertGreater(populations[1, 0], 1.0 - 2e-4)
        self.assertGreater(populations[2, 0], 1e-6)                          # some of it leaked to |2>
        self.assertLess(populations[1, 0] + populations[2, 0] + populations[0, 0], 1.0 + 1e-12)

    def test_a_hadamard_keeps_its_virtual_z_frame(self):
        """H = rz sx rz: the pulse is played at the hardware phase the frame sets, and the state ends at |+>."""
        world = world_of(t1=math.inf, tphi=math.inf)
        state = run(world, [[{"type": "H", "targets": [0]}]], [], 0.5)
        reduced = np.einsum("ajbj->ab", state.reshape(3, 3, 3, 3))[:2, :2]
        self.assertAlmostEqual(reduced[0, 1].real, 0.5, delta=2e-3)
        self.assertAlmostEqual(reduced[0, 1].imag, 0.0, delta=2e-3)
        effective = run(world_of("effective", t1=math.inf, tphi=math.inf), [[{"type": "H", "targets": [0]}]], [], 0.5)
        np.testing.assert_allclose(np.einsum("ajbj->ab", state.reshape(3, 3, 3, 3)),
                                   np.einsum("ajbj->ab", effective.reshape(3, 3, 3, 3)), atol=2e-3)


class CrossResonanceTests(unittest.TestCase):
    ANGLE, DURATION = 20.0, 1.6                     # a 2 MHz drive on the attacker's line, 100 MHz above it

    def victim_excitation(self, attacker_excited, solver="reference", transfers=(), until=2.0):
        """The attacker drives its own line at the victim's frequency; no classical leak unless declared."""
        columns_a = ([[{"type": "X", "targets": [0]}]] if attacker_excited else []) + [[{
            "type": "PULSE", "targets": [0],
            "params": {"angle_rad": self.ANGLE, "duration_us": self.DURATION, "frequency_offset_mhz": 100.0}}]]
        world = world_of(solver, transfers=transfers, shape=SQUARE_SHAPE, t1=math.inf, tphi=math.inf)
        state = run(world, columns_a, [], until)
        return np.diag(state).real.reshape(3, 3)[:, 1].sum(), state

    def test_the_exchange_alone_lets_the_attacker_drive_the_victim_depending_on_its_own_state(self):
        cluster = TransmonCluster(SPECS, (TransmonExchangeCoupling(0, 1, J),))
        moved = {}
        for excited in (0, 1):
            moved[excited], _ = self.victim_excitation(excited)
            element = abs(cluster.drive_element(0, 1, (excited, 0)))
            # First order in the drive; the drive's own Stark shifts make the rest.
            expected = math.sin(0.5 * self.ANGLE * element) ** 2
            self.assertAlmostEqual(moved[excited] / expected, 1.0, delta=0.1)
        self.assertGreater(abs(moved[1] - moved[0]), 0.02)                   # the victim reads the attacker
        # The world plays the pulse as the kernel does (carrier phase counted from the pulse's start).
        _, state = self.victim_excitation(0)
        offset = TWO_PI * 100.0
        drive = ClusterDrive(0, 0.25, self.DURATION, self.ANGLE, SQUARE_SHAPE, phase_rad=offset * 0.25,
                             frequency_offset_rad_per_us=offset)
        expected = unvec(cluster.propagator(0.0, 2.0, (drive,)) @ vec(np.kron(ground(), ground())))
        np.testing.assert_allclose(state, expected, atol=1e-9)
        # The effective solver has only the static shift: without a classical leak nothing moves the victim.
        self.assertLess(self.victim_excitation(0, "effective")[0], 1e-9)

    def test_a_classical_leak_between_cluster_lines_is_carried_by_the_cluster_and_not_kicked(self):
        transfer = DriveCrosstalkTransfer(0, 1, 0.02, 0.0, TWO_PI * 100.0)
        with_leak, _ = self.victim_excitation(0, transfers=(transfer,))
        without, _ = self.victim_excitation(0)
        self.assertGreater(abs(with_leak - without), 1e-3)
        regions = ConcurrentRegions((transfer, DriveCrosstalkTransfer(0, 3, 0.02)),
                                    transmons=settings())
        self.assertEqual(regions.kick_transfers, (DriveCrosstalkTransfer(0, 3, 0.02),))
        self.assertEqual(ConcurrentRegions((transfer,), transmons=settings("effective")).kick_transfers, (transfer,))


class ErrorBudgetTests(unittest.TestCase):
    def test_the_relaxation_error_is_the_idle_channels_infidelity(self):
        for spec, duration in ((IdleQubitSpec(30.0, 50.0, 0.0), 0.5), (IdleQubitSpec(80.0, math.inf, 0.02), 3.0)):
            paulis = (np.array([[0, 1], [1, 0]]), np.array([[0, -1j], [1j, 0]]), np.diag([1.0, -1.0]))
            transfer = []
            for pauli in paulis:
                image = np.array(pauli, dtype=complex)
                relax_axes_in_place(image, ((0, spec, duration),))
                transfer.append([0.5 * np.trace(other @ image).real for other in paulis])
            average_fidelity = 0.5 + np.trace(np.array(transfer)) / 6.0
            self.assertAlmostEqual(relaxation_error(spec, duration), 1.0 - average_fidelity, places=14)

    def test_only_the_part_relaxation_does_not_explain_is_added(self):
        self.assertAlmostEqual(residual_survival(0.0, 1e-4), 0.0)
        self.assertAlmostEqual(residual_survival(2e-4, 1e-4), 0.0, places=12)       # all of it is relaxation
        self.assertAlmostEqual(residual_survival(1e-4, 1e-4), 0.0)                  # less than relaxation alone
        p_res = residual_survival(1e-3, 1e-4)
        self.assertAlmostEqual((1.0 - p_res) * (1.0 - 2e-4), 1.0 - 1e-3, places=14)
        with self.assertRaises(ValueError):
            residual_survival(1.5, 0.0)

    def test_the_world_adds_the_residual_once(self):
        gate_error = 5e-3                    # above the 6e-4 that relaxation explains over 36 ns at T1 = 30 us
        compiled = compile_on([[{"type": "X", "targets": [0]}]] + [wait(0, 50.0)], (0,), gate_error=gate_error)
        instruction = next(i for i in compiled.instructions if i.kind == "unitary")
        cluster_relax = relaxation_error(IdleQubitSpec(T1, TPHI, 0.0, levels=3), GATE_US)
        expected = residual_survival(instruction.depolarizing_probability, cluster_relax)
        self.assertGreater(expected, 0.0)
        noisy, clean = world_of(), world_of()
        for world, error in ((noisy, gate_error), (clean, 0.0)):
            job = WorldJob("a", 0, compile_on([[{"type": "X", "targets": [0]}]] + [wait(0, 50.0)], (0,),
                                              gate_error=error), (0,), 0.25, 99.0)
            world.advance_to(0.25, (job,))
        # Right after the gate action the qubit is still in |0>: the depolarizing residual moved p/2 to |1>.
        moved = np.einsum("ajbj->ab", joint(noisy).reshape(3, 3, 3, 3))[1, 1].real
        self.assertAlmostEqual(moved, expected / 2.0, places=12)
        self.assertAlmostEqual(np.einsum("ajbj->ab", joint(clean).reshape(3, 3, 3, 3))[1, 1].real, 0.0, places=14)


class DeclarationTests(unittest.TestCase):
    def test_the_world_must_hold_every_level_of_a_reference_transmon(self):
        two_level_sides = sides(levels=2)
        with self.assertRaisesRegex(ValueError, "every level"):
            CoupledRegionWorld(two_level_sides, settings(), (ground(2), ground(2)), repetition_delay_us=0.0)

    def test_a_gate_without_native_steps_is_refused(self):
        world = world_of()
        plain = LocalCircuitCompiler(max_program_qubits=1, physical_qubit_ids=(0, 1))
        request = ProgramSubmitRequest(CircuitProgram.from_editor_json(program_json(1, [[{"type": "X", "targets": [0]}]])),
                                       "qpu-1", 1, "r")
        operation = plain.compile(request, sampling_seed=1, physical_qubit_ids=(0,))
        with self.assertRaisesRegex(ValueError, "native steps"):
            world.advance_to(1.0, (WorldJob("a", 0, operation, (0,), 0.25, 99.0),))

    def test_the_calibration_tunes_amplitude_and_drag(self):
        cluster = TransmonCluster(SPECS, (TransmonExchangeCoupling(0, 1, J),))
        calibration = calibrate_pulse(cluster, 0, math.pi / 2, GATE_US, GAUSSIAN_SHAPE)
        nominal = qubit_block(cluster, cluster.unitary(0.0, GATE_US, (
            ClusterDrive(0, 0.0, GATE_US, math.pi / 2, GAUSSIAN_SHAPE),)), 0)
        self.assertLess(calibration.infidelity, 0.1 * block_infidelity(nominal, rotation_x(math.pi / 2)))
        self.assertGreater(calibration.drag_us, 0.0)
        self.assertAlmostEqual(calibration.amplitude_scale, 1.0, delta=0.01)


class ExecutorTests(unittest.TestCase):
    def test_the_executor_runs_a_pulse_level_cluster(self):
        """Two-level transmons: the attacker's pulse at the victim's frequency flips the victim through J."""
        specs = ((1, TransmonSpec(5.0, None, 2)), (2, TransmonSpec(5.1, None, 2)))
        strength = TWO_PI * 5.0
        cluster = TransmonCluster(tuple(spec for _, spec in specs), (TransmonExchangeCoupling(0, 1, strength),))
        element = abs(cluster.drive_element(1, 0, (0, 0)))
        angle = math.pi / element
        transmons = CoupledTransmonSettings(specs, ((1, 2, strength),), solver="reference", model="exact")
        runtime, executor = build(transmons=transmons)
        attacker_op = compile_on([[{"type": "PULSE", "targets": [0], "params": {
            "angle_rad": angle, "duration_us": 0.5, "frequency_offset_mhz": -100.0}}], wait(0, 1.0)], (2, 3))
        victim_op = compile_on([wait(1, 3.0)], (0, 1))
        victim = runtime.submit(JobCommand("victim", victim_op, "v", layout=PhysicalQubitLayout("qpu-1", (0, 1))))
        attacker = runtime.submit(JobCommand("attacker", attacker_op, "a",
                                             layout=PhysicalQubitLayout("qpu-1", (2, 3))))
        register(executor, victim, 1.0, 0)
        register(executor, attacker, 1.2, 4)
        run_in_order(executor)
        self.assertEqual({k: n for k, n in runtime.scheduler.get(victim.job_id).result["counts"].items() if n},
                         {"01": 1})


if __name__ == "__main__":
    unittest.main()
