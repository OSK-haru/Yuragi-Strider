"""The joint world of two regions with a qubit that holds a third level (D91)."""

import math
import unittest

import numpy as np
from scipy.linalg import expm

from core.attack_simulation.coupled_regions import (
    CoupledRegionWorld, CoupledTransmonSettings, RegionSide, ResidualZZSettings, UncoupledSettings, WorldJob,
)
from core.attack_simulation.shot_activity import instruction_starts
from core.experimental_isolation_physics import ImperfectResetSpec, PhenomenologicalResetModel, ResetModelRegistry
from core.experimental_isolation_physics.coherent_crosstalk import ResidualZZCoupling
from core.experimental_isolation_physics.idle_evolution import (
    IdleQubitSpec, IndependentIdleModel, ladder_relaxation_operators,
)
from core.experimental_isolation_physics.readout_crosstalk import ReadoutCrosstalk
from core.experimental_isolation_physics.transmon_cluster import TransmonCluster, TransmonSpec
from core.pulse_transmon_network import TransmonExchangeCoupling
from core.register_operations import embed_qubit_operator, level_digits
from tests.test_coupled_regions import operation

T1, TPHI = 30.0, 50.0
DIMS_A, DIMS_B = (3, 2), (2, 2)
UNION = DIMS_A + DIMS_B
SPECS = (TransmonSpec(5.0, -330.0), TransmonSpec(5.1, -310.0))     # physical 0 (region A) and 2 (region B)
J = 2 * math.pi * 2.0                                                 # rad/us


def sides(read_two_as_zero=0.2):
    model = PhenomenologicalResetModel(ImperfectResetSpec(1.0), observed_zero_given_higher=(read_two_as_zero,))
    registry = ResetModelRegistry()
    registry.register(model)
    idle_a = IndependentIdleModel((IdleQubitSpec(T1, TPHI, 0.0, levels=3), IdleQubitSpec(T1, TPHI, 0.0)), 2)
    idle_b = IndependentIdleModel((IdleQubitSpec(T1, TPHI, 0.0),) * 2, 2)
    return (RegionSide((0, 1), idle_a, registry, model.model_id, 0.0, 1.0),
            RegionSide((2, 3), idle_b, registry, model.model_id, 0.0, 1.0))


def initial_a(leaked=0.3):
    """Physical 0 in |0> or |2> (a mixture), physical 1 in |0>."""
    state = np.zeros((6, 6), dtype=complex)
    state[0, 0], state[4, 4] = 1.0 - leaked, leaked
    return state


def zero(dimension):
    state = np.zeros((dimension, dimension), dtype=complex)
    state[0, 0] = 1.0
    return state


def jobs():
    a = [[{"type": "H", "targets": [0]}], [{"type": "I", "targets": [0], "params": {"duration_us": 1.3}}],
         [{"type": "X", "targets": [1]}], [{"type": "H", "targets": [0]}],
         [{"type": "I", "targets": [1], "params": {"duration_us": 50.0}}]]
    b = [[{"type": "H", "targets": [0]}], [{"type": "I", "targets": [0], "params": {"duration_us": 2.1}}],
         [{"type": "X", "targets": [1]}], [{"type": "H", "targets": [0]}],
         [{"type": "I", "targets": [1], "params": {"duration_us": 50.0}}]]
    return (WorldJob("a", 0, operation(a, (0, 1), gate_us=0.0), (0, 1), 0.25, 99.0),
            WorldJob("b", 1, operation(b, (2, 3), gate_us=0.0), (0, 1), 0.6, 99.0))


def cluster_energies():
    cluster = TransmonCluster(SPECS, (TransmonExchangeCoupling(0, 1, J),))
    return cluster, {tuple(label): cluster.frame_energies[cluster.label_index(tuple(int(v) for v in label))]
                     for label in level_digits((3, 2))}


def dense_reference(until_us, initial):
    """Union-register Lindblad evolution between instantaneous gates and resets, independent of the world."""
    dimension = math.prod(UNION)
    digits = level_digits(UNION)
    _, energy_of = cluster_energies()
    hamiltonian = np.diag([energy_of[(label[0], label[2])] for label in digits]).astype(complex)

    def embed(local, index):
        result = np.eye(1, dtype=complex)
        for position, level in enumerate(UNION):
            result = np.kron(result, local if position == index else np.eye(level))
        return result

    collapse = [embed(operator, index)
                for index, levels in enumerate(UNION)
                for operator in ladder_relaxation_operators(IdleQubitSpec(T1, TPHI, 0.0, levels=levels))]
    eye = np.eye(dimension)
    generator = -1j * (np.kron(eye, hamiltonian) - np.kron(hamiltonian.T, eye))
    for c in collapse:
        cc = c.conj().T @ c
        generator += np.kron(c.conj(), c) - 0.5 * (np.kron(eye, cc) + np.kron(cc.T, eye))

    def reset(rho, index):
        """IBM's reset with success 1: |1> -> |0>, |0> and a leaked level stay, the coherences go."""
        levels = UNION[index]
        out = np.zeros_like(rho)
        for level in range(levels):
            kraus = np.zeros((levels, levels))
            kraus[0 if level == 1 else level, level] = 1.0
            full = embed(kraus, index)
            out += full @ rho @ full.conj().T
        return out

    events = []
    for job, offset in zip(jobs(), (0, 2)):
        starts, _ = instruction_starts(job.operation, reset_duration_us=0.0)
        for instruction, start in zip(job.operation.instructions, starts):
            if instruction.kind in {"initialize", "unitary"}:
                events.append((job.start_us + start, offset, instruction))
    events = sorted((item for item in events if item[0] <= until_us), key=lambda item: item[0])
    rho, now = initial, 0.0
    for time, offset, instruction in events + [(until_us, None, None)]:
        rho = (expm(generator * (time - now)) @ rho.reshape(-1, order="F")).reshape(dimension, dimension, order="F")
        now = time
        if instruction is None:
            break
        for q in instruction.qubits:
            index = offset + q
            if instruction.kind == "initialize":
                rho = reset(rho, index)
            else:
                full = embed(embed_qubit_operator(instruction.unitary, (UNION[index],)), index)
                rho = full @ rho @ full.conj().T
    return rho


def joint(world):
    a, b = world.factors
    return np.einsum("kij,klm->iljm", a, b).reshape(a.shape[1] * b.shape[1], -1)


def run_world(settings, until, initial=None):
    world = CoupledRegionWorld(sides(), settings, (initial_a() if initial is None else initial, zero(4)),
                               repetition_delay_us=0.0)
    world.advance_to(until, jobs())
    world.settle(0, until)
    world.settle(1, until)
    return world


DEVICE = CoupledTransmonSettings(((0, SPECS[0]), (2, SPECS[1])), ((0, 2, J),), model="exact")


class LeakedWorldTests(unittest.TestCase):
    def test_a_leaked_qubit_coupled_by_device_values_matches_an_independent_lindblad_solution(self):
        for until in (1.0, 2.2, 4.0):
            with self.subTest(until=until):
                world = run_world(DEVICE, until)
                expected = dense_reference(until, np.kron(initial_a(), zero(4)))
                np.testing.assert_allclose(joint(world), expected, atol=1e-10)

    def test_the_neighbours_fringe_tells_whether_the_qubit_has_leaked(self):
        """The |2> of physical 0 shifts physical 2 by a different amount than |0>: B's Ramsey sees it."""
        _, energy_of = cluster_energies()
        shift = energy_of[(2, 1)] - energy_of[(2, 0)] - (energy_of[(0, 1)] - energy_of[(0, 0)])
        self.assertGreater(abs(shift), 2 * math.pi * 0.05)              # tens of kHz at J / 2 pi = 2 MHz
        populations = {}
        for leaked in (0.0, 1.0):
            world = run_world(DEVICE, 4.0, initial_a(leaked))
            populations[leaked] = np.diag(world.reduced_state(1)).real.reshape(2, 2).sum(axis=1)
        self.assertGreater(abs(populations[1.0][1] - populations[0.0][1]), 0.02)

    def test_the_prepare_reset_leaves_the_leaked_level_and_the_readout_reads_it(self):
        """Prepare, X on both, measure at once: |2> survives the reset and the X, and reads as 1 (p02 = 0)."""
        for read_two_as_zero, expected in ((0.0, "10"), (1.0, "00")):
            side_a, side_b = sides(read_two_as_zero)
            world = CoupledRegionWorld((side_a, side_b), UncoupledSettings(model="exact"),
                                       (initial_a(1.0), zero(4)), repetition_delay_us=0.0)
            job = WorldJob("a", 0, operation([[{"type": "X", "targets": [0]}, {"type": "X", "targets": [1]}]],
                                             (0, 1), gate_us=0.0), (0, 1), 0.25, 99.0)
            world.advance_to(5.0, (job,))
            with self.subTest(read_two_as_zero=read_two_as_zero):
                # Physical 1 was flipped to |1>; the leaked qubit decayed for at most the 1 us readout.
                self.assertEqual({bits: n for bits, n in world.progress["a"].counts.items() if n},
                                 {expected[0] + "1": 1})

    def test_two_level_descriptions_are_refused_on_a_leaked_qubit(self):
        zz = ResidualZZSettings((ResidualZZCoupling(0, 2, 0.1),), model="exact")
        with self.assertRaisesRegex(ValueError, "leaked level"):
            CoupledRegionWorld(sides(), zz, (initial_a(), zero(4)), repetition_delay_us=0.0)
        with self.assertRaisesRegex(ValueError, "two-level"):
            CoupledRegionWorld(sides(), UncoupledSettings(), (initial_a(), zero(4)), repetition_delay_us=0.0,
                               readout_crosstalk=(ReadoutCrosstalk(2, 0, dephasing_per_us=0.1),))
        # A measured ZZ between two-level qubits of the same world is fine.
        CoupledRegionWorld(sides(), ResidualZZSettings((ResidualZZCoupling(1, 3, 0.1),), model="exact"),
                           (initial_a(), zero(4)), repetition_delay_us=0.0)
        two_level = CoupledTransmonSettings(((0, TransmonSpec(5.0, None, 2)), (2, SPECS[1])), ((0, 2, J),))
        with self.assertRaisesRegex(ValueError, "at least the levels"):
            CoupledRegionWorld(sides(), two_level, (initial_a(), zero(4)), repetition_delay_us=0.0)


if __name__ == "__main__":
    unittest.main()
