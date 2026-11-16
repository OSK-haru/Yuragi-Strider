"""Reading one qubit out disturbs a neighbour and picks up its state (D91)."""

import cmath
import math
import unittest

import numpy as np

from core.attack_simulation.concurrent_lanes import ConcurrentRegions
from core.attack_simulation.coupled_regions import (
    CoupledRegionWorld, RegionSide, ResidualZZSettings, UncoupledSettings, WorldJob,
)
from core.experimental_isolation_physics.coherent_crosstalk import ResidualZZCoupling
from core.experimental_isolation_physics.idle_evolution import IdleQubitSpec, IndependentIdleModel
from core.experimental_isolation_physics.readout_crosstalk import ReadoutCrosstalk
from tests.test_concurrent_regions import run_in_order
from tests.test_coupled_regions import build, operation, register, registry, submit

RATE, SHIFT = 0.3, 0.7                     # Gamma (1/us) and the frequency shift (rad/us) of the neighbour
H = {"type": "H"}


def gate(kind, target):
    return [{"type": kind, "targets": [target]}]


def wait(target, duration_us):
    return [{"type": "I", "targets": [target], "params": {"duration_us": duration_us}}]


def sides(*, reset_b=0.0, measurement_b=1.0, t1=math.inf, tphi=math.inf):
    model, reg = registry()
    idle = IndependentIdleModel((IdleQubitSpec(t1, tphi, 0.0),) * 2, 2)
    return (RegionSide((0, 1), idle, reg, model.model_id, 0.0, 1.0),
            RegionSide((2, 3), idle, reg, model.model_id, reset_b, measurement_b))


def world_of(crosstalk, settings=None, **side_options):
    zero = np.zeros((4, 4), dtype=complex)
    zero[0, 0] = 1.0
    return CoupledRegionWorld(sides(**side_options), settings or UncoupledSettings(), (zero, zero),
                              repetition_delay_us=0.0, readout_crosstalk=crosstalk)


def victim(columns, start=0.25):
    """Region (0, 1); it is still waiting to be measured when the tests look at it."""
    return WorldJob("a", 0, operation(columns + [wait(1, 50.0)], (0, 1), gate_us=0.0), (0, 1), start, 99.0)


def reader(columns=(), start=1.0, seed=1):
    """Region (2, 3): its final measurements begin at ``start`` plus the length of ``columns``."""
    return WorldJob("b", 1, operation(list(columns), (2, 3), gate_us=0.0, seed=seed), (0, 1), start, 99.0)


def neighbour_coherence(world, until, jobs):
    """``<1| rho |0>`` of physical 1 once everything up to ``until`` has happened."""
    world.advance_to(until, jobs)
    world.settle(0, until)
    return world.reduced_state(0)[1, 0]


def joint(world):
    a, b = world.factors
    return np.einsum("kij,klm->iljm", a, b).reshape(a.shape[1] * b.shape[1], -1)


class DisturbanceTests(unittest.TestCase):
    CROSSTALK = (ReadoutCrosstalk(2, 1, dephasing_per_us=RATE, frequency_shift_rad_per_us=SHIFT),)

    def test_the_neighbour_loses_coherence_and_turns_while_the_readout_lasts(self):
        jobs = (victim([gate("H", 1)]), reader())                       # the readout runs over [1, 2)
        for until, exposed in ((0.9, 0.0), (1.4, 0.4), (2.0, 1.0), (7.0, 1.0)):
            with self.subTest(until=until):
                value = neighbour_coherence(world_of(self.CROSSTALK), until, jobs)
                self.assertAlmostEqual(value, 0.5 * cmath.exp(-(RATE + 1j * SHIFT) * exposed), places=12)
        # The Ramsey contrast of the cited measurement: exp(-Gamma tau).
        self.assertAlmostEqual(2.0 * abs(neighbour_coherence(world_of(self.CROSSTALK), 3.0, jobs)), math.exp(-RATE))

    def test_only_the_declared_neighbour_of_the_qubit_being_read_is_disturbed(self):
        jobs = (victim([gate("H", 0), gate("H", 1)]), reader())
        world = world_of(self.CROSSTALK)
        world.advance_to(3.0, jobs)
        world.settle(0, 3.0)
        state = world.reduced_state(0)
        self.assertAlmostEqual(state[2, 0], 0.25)                        # physical 0: untouched
        # Reading physical 3 alone leaves physical 1 alone.
        other = (ReadoutCrosstalk(3, 0, dephasing_per_us=RATE),)
        self.assertAlmostEqual(neighbour_coherence(world_of(other), 3.0, (victim([gate("H", 1)]), reader())), 0.5)

    def test_an_echo_on_the_neighbour_undoes_the_shift_but_not_the_dephasing(self):
        # X on the neighbour at the middle of the readout: the two halves turn it in opposite senses.
        jobs = (victim([gate("H", 1), wait(1, 1.25), gate("X", 1)]), reader())
        value = neighbour_coherence(world_of(self.CROSSTALK), 3.0, jobs)
        self.assertAlmostEqual(value, 0.5 * math.exp(-RATE), places=12)

    def test_a_reset_disturbs_for_its_measurement_and_not_for_its_feedback(self):
        # The reader's job: initializing reset over [1, 2) with a 0.4 us measurement, then the final readout.
        jobs = (victim([gate("H", 1)]), reader())
        options = dict(reset_b=1.0, measurement_b=0.4)
        for until, exposed in ((1.2, 0.2), (1.9, 0.4), (2.1, 0.5), (5.0, 0.8)):
            with self.subTest(until=until):
                value = neighbour_coherence(world_of(self.CROSSTALK, **options), until, jobs)
                self.assertAlmostEqual(value, 0.5 * cmath.exp(-(RATE + 1j * SHIFT) * exposed), places=12)

    def test_it_acts_together_with_relaxation_and_a_coupling(self):
        """With T1, T_phi and ZZ on the same qubit the three commute: the coherence is their product."""
        zeta = 0.2
        settings = ResidualZZSettings((ResidualZZCoupling(1, 2, zeta),), model="exact")
        jobs = (victim([gate("H", 1)]), reader())
        value = neighbour_coherence(world_of(self.CROSSTALK, settings, t1=30.0, tphi=50.0), 3.0, jobs)
        # Physical 2 stays in |0>, so the ZZ does nothing; the neighbour idles from 0.25 to 3.
        idle = math.exp(-2.75 * (0.5 / 30.0 + 1.0 / 50.0))
        self.assertAlmostEqual(value, 0.5 * idle * cmath.exp(-(RATE + 1j * SHIFT)), places=10)


class AssignmentTests(unittest.TestCase):
    def counts(self, crosstalk, victim_columns, seed=1, until=3.0):
        world = world_of(crosstalk)
        jobs = (victim(victim_columns), reader(seed=seed))
        world.advance_to(until, jobs)
        world.settle(0, until)
        return world, {bits: n for bits, n in world.progress["b"].counts.items() if n}

    def test_a_result_that_follows_the_neighbour_reads_it(self):
        crosstalk = (ReadoutCrosstalk(2, 1, excited_assignment_shift=(1.0, 0.0)),)
        self.assertEqual(self.counts(crosstalk, [])[1], {"00": 1})
        self.assertEqual(self.counts(crosstalk, [gate("X", 1)])[1], {"10": 1})
        # Only the declared reader's bit moves, and only for the declared neighbour.
        self.assertEqual(self.counts(crosstalk, [gate("X", 0)])[1], {"00": 1})

    def test_reading_a_superposed_neighbour_collapses_it_onto_the_result(self):
        crosstalk = (ReadoutCrosstalk(2, 1, excited_assignment_shift=(1.0, 0.0)),)
        seen = set()
        for seed in range(1, 9):
            world, counts = self.counts(crosstalk, [gate("H", 1)], seed=seed)
            (bits,) = counts
            seen.add(bits)
            expected = np.zeros((4, 4))
            expected[int(bits[0]), int(bits[0])] = 1.0                    # physical 1 is the low bit of region (0, 1)
            np.testing.assert_allclose(world.reduced_state(0), expected, atol=1e-12)
        self.assertEqual(seen, {"00", "10"})

    def test_a_partial_dependence_is_a_weak_measurement(self):
        """P(assigned 1) = 0.4 with the neighbour in |1>, 0 in |0>: a 1 proves |1>; a 0 only makes |0> likelier."""
        crosstalk = (ReadoutCrosstalk(2, 1, excited_assignment_shift=(0.4, 0.4)),)
        seen = {}
        for seed in range(1, 25):
            world, counts = self.counts(crosstalk, [gate("H", 1)], seed=seed)
            (bits,) = counts
            seen[bits] = seen.get(bits, 0) + 1
            state = world.reduced_state(0)[:2, :2]
            if bits == "10":
                np.testing.assert_allclose(state, [[0, 0], [0, 1]], atol=1e-12)
            else:
                root = math.sqrt(0.6)
                np.testing.assert_allclose(state, np.array([[1.0, root], [root, 0.6]]) / 1.6, atol=1e-12)
        self.assertEqual(set(seen), {"00", "10"})                            # P(1) = 0.2 over the 24 seeds
        self.assertLess(seen["10"], seen["00"])

    def test_neighbours_in_both_regions_match_a_dense_calculation(self):
        """The reader's result depends on a qubit of the other region and on one of its own; the regions are entangled."""
        crosstalk = (ReadoutCrosstalk(2, 1, excited_assignment_shift=(0.3, -0.2)),
                     ReadoutCrosstalk(2, 3, excited_assignment_shift=(0.2, -0.1)),
                     ReadoutCrosstalk(2, 0, excited_assignment_shift=(0.1, -0.05)))
        settings = ResidualZZSettings((ResidualZZCoupling(1, 2, 0.9), ResidualZZCoupling(0, 3, 0.4)), model="exact")
        columns_a = [gate("H", 0) + gate("H", 1)]
        columns_b = [gate("H", 0) + gate("H", 1), wait(0, 1.5)]            # the readout begins at 2.5
        outcomes = set()
        for seed in range(1, 7):
            jobs = (victim(columns_a), reader(columns_b, seed=seed))
            before = world_of(crosstalk, settings)
            before.advance_to(2.5 - 1e-6, jobs)
            before.settle(0, 2.5)
            before.settle(1, 2.5)
            after = world_of(crosstalk, settings)
            after.advance_to(2.5, jobs)
            after.settle(0, 2.5)
            after.settle(1, 2.5)
            (bits,) = [label for label, n in after.progress["b"].counts.items() if n]
            assigned = int(bits[0])
            # The true outcome of the two measured qubits is the basis state region (2, 3) was projected onto.
            true = int(np.argmax(np.diag(after.reduced_state(1)).real))
            outcomes.add((true, bits))
            labels = np.arange(16)
            keep = (labels & 3) == true
            projected = np.where(keep[:, None] & keep[None, :], joint(before), 0.0)
            projected = projected / np.trace(projected).real
            bit2, bit3 = true >> 1, true & 1
            excited = ((0.0 if bit2 == 0 else 1.0) + (0.3, -0.2)[bit2] * ((labels >> 2) & 1)
                       + (0.2, -0.1)[bit2] * bit3 + (0.1, -0.05)[bit2] * ((labels >> 3) & 1))
            likelihood = excited if assigned else 1.0 - excited
            root = np.sqrt(likelihood)
            expected = root[:, None] * projected * root[None, :]
            np.testing.assert_allclose(joint(after), expected / np.trace(expected).real, atol=1e-10)
        self.assertGreater(len(outcomes), 2)

    def test_the_readers_own_errors_are_the_values_with_the_neighbour_in_zero(self):
        """Without a shift the declared pair changes nothing: counts and states equal the undeclared run."""
        plain = self.counts((), [gate("H", 1)], seed=5)
        declared = self.counts((ReadoutCrosstalk(2, 1),), [gate("H", 1)], seed=5)
        self.assertEqual(plain[1], declared[1])
        np.testing.assert_allclose(joint(plain[0]), joint(declared[0]), atol=1e-14)

    def test_shifts_that_leave_the_unit_interval_are_refused(self):
        crosstalk = (ReadoutCrosstalk(2, 1, excited_assignment_shift=(-0.5, 0.0)),)
        world = world_of(crosstalk)
        with self.assertRaisesRegex(ValueError, "outside"):
            world.advance_to(3.0, (victim([gate("X", 1)]), reader()))


class ExecutorTests(unittest.TestCase):
    def run_pair(self, crosstalk, victim_columns):
        runtime, executor = build(readout_crosstalk=crosstalk)
        victim_job = submit(runtime, "victim", victim_columns + [wait(1, 5.0)], "v")
        attacker_job = submit(runtime, "attacker", [wait(0, 0.5)], "a")
        register(executor, victim_job, 1.0, 0)
        register(executor, attacker_job, 1.5, 4)
        run_in_order(executor)
        return tuple({bits: n for bits, n in runtime.scheduler.get(job.job_id).result["counts"].items() if n}
                     for job in (victim_job, attacker_job))

    def test_the_attackers_result_follows_the_victims_qubit_without_any_coupling(self):
        crosstalk = (ReadoutCrosstalk(2, 1, excited_assignment_shift=(1.0, 0.0)),)
        self.assertEqual(self.run_pair(crosstalk, []), ({"00": 1}, {"00": 1}))
        self.assertEqual(self.run_pair(crosstalk, [gate("X", 1)]), ({"01": 1}, {"10": 1}))

    def test_the_attackers_readout_spoils_the_victims_ramsey_fringe(self):
        """A frequency shift that adds up to pi over the attacker's readouts flips the victim's H - wait - H."""
        columns = [gate("H", 1), wait(1, 5.0), gate("H", 1)]
        quiet = self.run_pair((), columns)
        # The victim is in superposition from 2.0; the attacker's initializing reset reads until 2.5 and its
        # final readout runs over [3, 4): 1.5 us in all.
        turned = self.run_pair((ReadoutCrosstalk(2, 1, frequency_shift_rad_per_us=math.pi / 1.5),), columns)
        self.assertEqual((quiet[0], turned[0]), ({"00": 1}, {"01": 1}))

    def test_the_declaration_is_checked(self):
        with self.assertRaises(ValueError):
            ConcurrentRegions(readout_crosstalk=(ReadoutCrosstalk(2, 1), ReadoutCrosstalk(2, 1, dephasing_per_us=1.0)))
        with self.assertRaises(TypeError):
            ConcurrentRegions(readout_crosstalk=((2, 1),))
        self.assertIsNone(ConcurrentRegions().world_settings)
        self.assertIsInstance(ConcurrentRegions(readout_crosstalk=(ReadoutCrosstalk(2, 1),)).world_settings,
                              UncoupledSettings)
        for bad in (dict(reader=1, neighbour=1), dict(reader=-1, neighbour=1),
                    dict(reader=2, neighbour=1, dephasing_per_us=-0.1),
                    dict(reader=2, neighbour=1, frequency_shift_rad_per_us=math.inf),
                    dict(reader=2, neighbour=1, excited_assignment_shift=(1.5, 0.0)),
                    dict(reader=2, neighbour=1, excited_assignment_shift=(0.1,))):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ReadoutCrosstalk(**bad)
        with self.assertRaisesRegex(ValueError, "two regions"):
            world_of((ReadoutCrosstalk(2, 9),))
        with self.assertRaisesRegex(ValueError, "twice"):
            world_of((ReadoutCrosstalk(2, 1), ReadoutCrosstalk(2, 1, dephasing_per_us=1.0)))


if __name__ == "__main__":
    unittest.main()
