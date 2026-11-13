"""Joint simulation of two concurrent regions coupled by residual ZZ (D85).

Residual ZZ between qubits of two tenants' regions entangles the regions, so the regions can no longer be
advanced one job at a time from separate states (D81). Coupled regions are instead advanced together, in
time order, on one joint state.

The joint state is kept as a sum of products ``rho = sum_k A_k (x) B_k`` (operator Schmidt form). Local
operations act on each factor; only the coupling raises the number of terms, and a compression after each
coupling step keeps it bounded. ``exact`` keeps every term above round-off; ``low_rank`` also drops the
smallest terms (D79), and the dropped Hilbert-Schmidt norm is accumulated as the error bound.

Physics in this module:

* ZZ is ``H = sum zeta n_i n_j`` over the declared cross-region pairs (the conditional-shift convention of
  ``ResidualZZCoupling``): qubit ``j`` runs ``zeta`` faster in angle while qubit ``i`` is in |1>.
* The coupled qubits (the "exposed" ones) evolve under ZZ together with their idle relaxation, with the
  exact superoperator of each interval (no splitting error). A qubit inside an instruction is busy: it does
  not relax, but ZZ keeps acting on it. Instructions themselves are instantaneous at their start time.
* Every other qubit relaxes lazily, just before it is next touched, as in the single-region runtime (D85
  idle relaxation); local channels on other qubits commute with ZZ, so this is exact.

The per-shot semantics mirror ``SharedQpuRuntime._run_before_measurement`` and ``_execute_circuit``: with no
coupling, the reduced states and counts are those of the region-by-region path.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import heapq
import math
import random

import numpy as np

from core.attack_simulation.circuit_program import CircuitExecutionOperation
from core.attack_simulation.kick_application import (
    EMPTY_PLAN, KickPlan, PlacedKick, apply_readout_decisions, kick_unitary,
)
from core.attack_simulation.register_readout import register_assignment_errors
from core.attack_simulation.shot_activity import instruction_starts
from core.cptp_liouvillian import dense_matrix_exponential, gksl_liouvillian_superoperator
from core.experimental_isolation_physics.coherent_crosstalk import ResidualZZCoupling
from core.experimental_isolation_physics.idle_evolution import IndependentIdleModel, relax_axes_in_place
from core.experimental_isolation_physics.reset_model import ResetModelRegistry, ResetModelRequest
from core.register_operations import (
    apply_local_replacement, apply_reset_with_feedback_kick, freeze_register, nonselective_measurement,
)

ZZ_MODELS = ("low_rank", "exact")
EXACT_MAX_UNION_QUBITS = 8          # the dense-computation limit; ``exact`` is only offered up to it (D79)
_ROUND_OFF = 1e-14                  # relative singular value treated as zero, also in ``exact``
_TIME_TOLERANCE = 1e-9              # us; absolute float coordinates of a window end differ by round-off
_NEGATIVE_TOLERANCE = 1e-6          # truncation may leave tiny negative eigenvalues; larger ones are an error
_FACTOR_CACHE_LIMIT = 4096


@dataclass(frozen=True)
class ResidualZZSettings:
    """Frozen choice of the cross-region residual ZZ and of its approximation model (D79, D85)."""

    couplings: tuple[ResidualZZCoupling, ...]
    model: str = "low_rank"
    max_rank: int = 32
    truncation: float = 1e-6        # largest Hilbert-Schmidt norm one compression may drop (low_rank)

    def __post_init__(self) -> None:
        couplings = tuple(self.couplings)
        if not couplings or any(type(item) is not ResidualZZCoupling for item in couplings):
            raise ValueError("residual ZZ needs at least one ResidualZZCoupling")
        pairs = [frozenset((item.left, item.right)) for item in couplings]
        if len(set(pairs)) != len(pairs):
            raise ValueError("a residual-ZZ pair is declared twice")
        object.__setattr__(self, "couplings", couplings)
        if self.model not in ZZ_MODELS:
            raise ValueError("unknown residual-ZZ model")
        if type(self.max_rank) is not int or self.max_rank < 1:
            raise ValueError("max_rank must be a positive integer")
        truncation = float(self.truncation)
        if not math.isfinite(truncation) or truncation < 0.0:
            raise ValueError("truncation must be finite and nonnegative")
        object.__setattr__(self, "truncation", truncation)


@dataclass(frozen=True)
class RegionSide:
    """One region of the pair, with the models its jobs run under."""

    region: tuple[int, ...]
    idle_model: IndependentIdleModel
    reset_models: ResetModelRegistry
    reset_model_id: str
    reset_us: float
    measurement_us: float
    handover_reset_count: int = 1           # the runtime's service reset when the region changes tenant
    handover_measures_input: bool = False
    handover_scope: str = "layout"          # "layout": the new tenant's qubits; "region": every qubit
    readout_dephasing_per_us: float | None = None   # None: strong-measurement (Zeno) limit


@dataclass(frozen=True)
class WorldJob:
    """A started job as the world needs it. ``kicks`` recomputes its drive-leak plan from the windows known now."""

    job_id: str
    side: int
    operation: CircuitExecutionOperation
    slots: tuple[int, ...]
    start_us: float
    finish_us: float
    kicks: Callable[[], KickPlan] = lambda: EMPTY_PLAN
    handover: bool = False          # the region changes tenant: service reset of the layout before the shots


@dataclass
class _Progress:
    actions: list
    layout: tuple = ()              # (instruction starts, measurement start, shot length)
    pointer: int = 0
    last_seq: tuple = (-math.inf,)
    last_time: float = -math.inf
    applied: Counter = field(default_factory=Counter)
    generator: random.Random | None = None
    counts: dict = field(default_factory=dict)
    records: list = field(default_factory=list)
    assignment: dict = field(default_factory=dict)
    duration: float = 0.0
    classical: list = field(default_factory=list)
    first_shot_us: float = 0.0
    handover: tuple | None = None   # (reduced state, duration) of the handover service reset

    def copy(self) -> "_Progress":
        generator = random.Random()
        generator.setstate(self.generator.getstate())
        return _Progress(self.actions, self.layout, self.pointer, self.last_seq, self.last_time, Counter(self.applied), generator,
                         dict(self.counts), list(self.records), dict(self.assignment), self.duration,
                         list(self.classical), self.first_shot_us, self.handover)

    @property
    def done(self) -> bool:
        return self.pointer >= len(self.actions)


@dataclass
class WorldStats:
    compressions: int = 0
    max_rank: int = 1
    dropped_norm: float = 0.0       # sum of the Hilbert-Schmidt norms dropped by truncation (error bound)
    clipped_eigenvalue: float = 0.0  # most negative reduced-state eigenvalue removed before a record
    late_kicks: int = 0             # leaks that became known after their place had been simulated


class CoupledRegionWorld:
    """Two regions advanced together on one joint state. Copy before advancing a candidate."""

    def __init__(self, sides: tuple[RegionSide, RegionSide], settings: ResidualZZSettings,
                 initial_states: tuple[np.ndarray, np.ndarray], *, repetition_delay_us: float) -> None:
        if len(sides) != 2 or set(sides[0].region) & set(sides[1].region):
            raise ValueError("coupled regions must be two disjoint regions")
        self.sides = tuple(sides)
        self.settings = settings
        self.repetition_delay_us = float(repetition_delay_us)
        sizes = tuple(len(side.region) for side in sides)
        if settings.model == "exact" and sum(sizes) > EXACT_MAX_UNION_QUBITS:
            raise ValueError("the exact residual-ZZ model is limited to two regions of at most 8 qubits together")
        location = {q: (s, side.region.index(q)) for s, side in enumerate(sides) for q in side.region}
        edges = []
        for coupling in settings.couplings:
            ends = (location.get(coupling.left), location.get(coupling.right))
            if None in ends or ends[0][0] == ends[1][0]:
                raise ValueError("a residual-ZZ pair must join the two regions")
            a, b = sorted(ends)
            edges.append((a[1], b[1], float(coupling.conditional_shift_rad_per_us)))
        self._components = _components(edges)
        self.exposed = (frozenset(q for comp in self._components for q in comp[0]),
                        frozenset(q for comp in self._components for q in comp[1]))
        self.factors = tuple(np.asarray(state, dtype=complex).reshape(1, *np.shape(state)).copy()
                             for state in initial_states)
        for factor, size in zip(self.factors, sizes):
            if factor.shape != (1, 1 << size, 1 << size):
                raise ValueError("initial state does not match its region")
        self.free = tuple([0.0] * size for size in sizes)
        self.zz_time = 0.0
        self.progress: dict[str, _Progress] = {}
        self.stats = WorldStats()
        self._cache: dict = {}

    # ----- lifecycle -------------------------------------------------------------------------------------

    def copy(self) -> "CoupledRegionWorld":
        other = object.__new__(CoupledRegionWorld)
        other.sides, other.settings, other.repetition_delay_us = self.sides, self.settings, self.repetition_delay_us
        other._components, other.exposed, other._cache = self._components, self.exposed, self._cache
        other.factors = tuple(np.array(factor) for factor in self.factors)
        other.free = tuple(list(values) for values in self.free)
        other.zz_time = self.zz_time
        other.progress = {job_id: item.copy() for job_id, item in self.progress.items()}
        other.stats = WorldStats(**vars(self.stats))
        return other

    def advance_to(self, time_us: float, jobs: Sequence[WorldJob]) -> None:
        """Simulate every action at or before ``time_us`` of the started jobs ``jobs``.

        ``jobs`` must hold every job that has started before ``time_us`` and is not yet complete; their kick
        plans must include every window known up to now (sealed ingress guarantees nothing earlier is missing).
        """
        limit = time_us + _TIME_TOLERANCE
        queue = []
        for rank, job in enumerate(sorted(jobs, key=lambda item: (item.start_us, item.job_id))):
            if job.start_us >= limit:
                continue
            progress = self.progress.get(job.job_id)
            if progress is None:
                progress = self.progress[job.job_id] = self._start(job)
            if progress.done:
                continue
            pending = self._pending(job, progress)
            if pending:
                heapq.heappush(queue, (pending[0][0], rank, 0, job, pending))
        known = {job.job_id for job in jobs}
        for job_id, progress in self.progress.items():
            if not progress.done and job_id not in known:
                raise ValueError("a started job that is not complete was left out of the advance")
        while queue:
            when, rank, index, job, pending = heapq.heappop(queue)
            if when > limit:
                break
            _, actual, seq, kind, payload = pending[index]
            progress = self.progress[job.job_id]
            self._do(job, progress, kind, payload, when, actual)
            progress.last_seq, progress.last_time = seq, when
            if kind != "kick":
                progress.pointer += 1
            else:
                progress.applied[payload] += 1
            if index + 1 < len(pending):
                heapq.heappush(queue, (pending[index + 1][0], rank, index + 1, job, pending))

    def settle(self, side: int, time_us: float) -> None:
        """Bring every qubit of ``side`` (and the coupling) up to ``time_us``."""
        self._flush(time_us)
        self._relax_to(side, range(len(self.free[side])), time_us)

    def reduced_state(self, side: int) -> np.ndarray:
        """The region's own state, ``Tr_other(rho)``; tiny negative eigenvalues from truncation are clipped."""
        state = self._reduced(side)
        state = 0.5 * (state + state.conj().T)
        if self.settings.model == "exact":
            return state
        values, vectors = np.linalg.eigh(state)
        lowest = float(values[0])
        if lowest >= 0.0:
            return state
        if lowest < -_NEGATIVE_TOLERANCE:
            raise ValueError("the low-rank residual-ZZ state lost positivity; raise max_rank or lower truncation")
        self.stats.clipped_eigenvalue = min(self.stats.clipped_eigenvalue, lowest)
        values = np.clip(values, 0.0, None)
        state = (vectors * values) @ vectors.conj().T
        return state / np.trace(state).real

    def records(self, job_id: str) -> tuple[list, dict]:
        """The job's ``(reduced state, duration)`` per shot and its counts; the job must be complete."""
        progress = self.progress[job_id]
        if not progress.done:
            raise ValueError("the job has not been simulated through its finish")
        return progress.records, progress.counts

    def handover_record(self, job_id: str) -> tuple | None:
        """``(reduced state, duration)`` after the job's handover service reset, or None without one."""
        progress = self.progress[job_id]
        if not progress.done:
            raise ValueError("the job has not been simulated through its finish")
        return progress.handover

    # ----- actions ---------------------------------------------------------------------------------------

    def _start(self, job: WorldJob) -> _Progress:
        side = self.sides[job.side]
        operation = job.operation
        starts, measure_start = instruction_starts(operation, reset_duration_us=side.reset_us)
        groups = len({i.time_group for i in operation.instructions if i.kind == "measure"})
        shot_length = measure_start + side.measurement_us * groups
        count = len(operation.instructions)
        first = job.start_us + (side.reset_us if job.handover else 0.0)
        # A reset (IBM: a measurement, then an X on outcome 1) occupies its qubits from its start and takes
        # effect at its end; the ZZ coupling keeps acting on the pre-reset populations meanwhile.
        actions = ([(job.start_us, (-1, 0, 1, 0), "handover", "begin"),
                    (first, (-1, 1, 0, 0), "handover", "end")] if job.handover else [])
        for shot in range(operation.shots):
            begin = first + shot * (shot_length + self.repetition_delay_us)
            actions.append((begin, (shot, -2, 0, 0), "shot", shot))
            for index, (instruction, start) in enumerate(zip(operation.instructions, starts)):
                if instruction.kind in {"initialize", "reset"}:
                    actions.append((begin + start, (shot, index, 1, 0), "reset", (shot, index, "begin")))
                    actions.append((begin + start + side.reset_us, (shot, index, 1, 1), "reset",
                                    (shot, index, "end")))
                elif instruction.kind != "measure":
                    actions.append((begin + start, (shot, index, 1, 0), "instruction", (shot, index)))
            actions.append((begin + measure_start, (shot, count, 2, 0), "measure", shot))
            actions.append((begin + shot_length, (shot, count + 1, 1, 0), "commit", shot))
        return _Progress(actions, (starts, measure_start, shot_length),
                         generator=random.Random(operation.sampling_seed),
                         counts={"0": 0, "1": 0} if operation.classical_bits == 1 else {}, first_shot_us=first)

    def _pending(self, job: WorldJob, progress: _Progress) -> list:
        """Remaining actions with the kicks known now, in program order, each with a non-decreasing time."""
        starts, measure_start, shot_length = progress.layout
        count = len(job.operation.instructions)
        plan = job.kicks()
        kicks = []
        for shot in range(job.operation.shots):
            begin = progress.first_shot_us + shot * (shot_length + self.repetition_delay_us)
            for order, kick in enumerate(plan.of_shot(shot)):
                if kick.before_handover and not job.handover:
                    raise ValueError("leaks were placed before a handover that does not happen")
                if kick.readout is not None:
                    # Folded into the shot's measurement (``_readout``); late once that is simulated.
                    end = (shot, count, 2, 0)
                    if end > progress.last_seq:
                        continue
                    kicks.append((begin + measure_start, end, (shot, kick)))
                elif kick.in_reset:
                    # Folded into its reset's end (``_feedback``). Once that end is simulated, a kick
                    # learned only now is late: it then acts as a plain rotation (counted below).
                    end = (-1, 1, 0, 0) if kick.before_handover else (shot, kick.position, 1, 1)
                    if end > progress.last_seq:
                        continue
                    at = job.start_us if kick.before_handover else begin + starts[kick.position]
                    kicks.append((at, end, (shot, kick)))
                elif kick.before_handover:
                    kicks.append((job.start_us, (-1, 0, 0, order), (shot, kick)))
                elif kick.wait_offset_us is not None:
                    kicks.append((begin - self.repetition_delay_us + kick.wait_offset_us, (shot, -3, 0, order),
                                  (shot, kick)))
                else:
                    at = starts[kick.position] if kick.position < count else None
                    kicks.append((begin + (measure_start if at is None else at), (shot, kick.position, 0, order),
                                  (shot, kick)))
        last = job.operation.shots - 1
        end = progress.first_shot_us + last * (shot_length + self.repetition_delay_us) + shot_length
        kicks.extend((end, (last, count + 1, 0, order), (last, kick)) for order, kick in enumerate(plan.trailing))
        seen = Counter(progress.applied)
        fresh, late = [], []
        for time, seq, key in kicks:
            if seen[key] > 0:
                seen[key] -= 1
                continue
            (late if seq <= progress.last_seq else fresh).append((time, seq, "kick", key))
        merged = sorted([(time, seq, kind, payload) for time, seq, kind, payload in progress.actions[progress.pointer:]]
                        + fresh, key=lambda item: item[1])
        self.stats.late_kicks += len(late)
        merged = [(max(progress.last_time, 0.0), seq, kind, payload) for _, seq, kind, payload in late] + merged
        pending, floor = [], progress.last_time
        for time, seq, kind, payload in merged:
            floor = max(floor, time)
            pending.append((floor, time, seq, kind, payload))
        return pending

    def _do(self, job: WorldJob, progress: _Progress, kind: str, payload, when: float, actual: float) -> None:
        side_index, side = job.side, self.sides[job.side]
        operation = job.operation
        starts, measure_start, shot_length = progress.layout
        if kind == "handover":
            # As the runtime's service reset: the scope's qubits are reset; the rest of the region keeps
            # what the previous tenant left (and keeps relaxing / coupling meanwhile).
            targets = (tuple(range(len(side.region))) if side.handover_scope == "region" else job.slots)
            if payload == "begin":
                self._occupy(side_index, targets, when, actual, side.reset_us)
            else:
                self._touch(side_index, targets, when)
                feedback = self._feedback(job, progress, 0, None)
                self._reset(side_index, targets, count=side.handover_reset_count,
                            measures_input=side.handover_measures_input, feedback=feedback)
            return
        if kind == "reset":
            shot, index, phase = payload
            instruction = operation.instructions[index]
            operands = tuple(job.slots[q] for q in instruction.qubits)
            if phase == "begin":
                self._occupy(side_index, operands, when, actual, side.reset_us)
                progress.duration += side.reset_us
            else:
                self._touch(side_index, operands, when)
                errors = self._reset(side_index, operands, feedback=self._feedback(job, progress, shot, index))
                progress.assignment.update(zip(instruction.qubits, errors))
            return
        if kind == "shot":
            if job.handover and progress.handover is None:
                # The service-reset record is the region when the reset ends, i.e. when the first shot begins.
                self.settle(side_index, when)
                progress.handover = (self.reduced_state(side_index), side.reset_us)
            progress.assignment = {}
            progress.duration = self.repetition_delay_us if payload else 0.0
            if operation.initialization == "inherit":
                errors = register_assignment_errors(side.reset_models.get(side.reset_model_id),
                                                    subsystem_count=len(side.region), targets=job.slots)
                progress.assignment.update(enumerate(errors))
            return
        if kind == "kick":
            _, kick = payload
            slot = side.region.index(kick.qubit)
            self._touch(side_index, (slot,), when)
            self._relax_to(side_index, (slot,), actual)
            self._unitary(side_index, kick_unitary(kick), (slot,))
            return
        if kind == "instruction":
            _, index = payload
            instruction = operation.instructions[index]
            operands = tuple(job.slots[q] for q in instruction.qubits)
            self._touch(side_index, operands, when)
            self._relax_to(side_index, operands, actual)
            if instruction.kind == "unitary":
                self._unitary(side_index, np.asarray(instruction.unitary, dtype=complex), operands)
                if instruction.depolarizing_probability:
                    self._depolarize(side_index, operands, instruction.depolarizing_probability)
                busy = instruction.intrinsic_us
                progress.duration += instruction.duration_us
            elif instruction.kind == "delay":
                busy = 0.0
                progress.duration += instruction.duration_us
            else:
                raise ValueError("unknown compiled circuit instruction")
            for slot in operands:
                self.free[side_index][slot] = max(self.free[side_index][slot], actual + busy)
            return
        if kind == "measure":
            measurements = tuple(i for i in operation.instructions if i.kind == "measure")
            measured_qubits = tuple(q for i in measurements for q in i.qubits)
            targets = tuple(c for i in measurements for c in i.classical_targets)
            measured_slots = tuple(job.slots[q] for q in measured_qubits)
            self._flush(when)
            self._relax_to(side_index, range(len(side.region)), actual)
            readout = self._readout(job, progress, payload)
            # A leak during a qubit's readout acts on the collapsed state (strong measurement, quantum Zeno);
            # the thresholded result is the state at the window's middle (``kick_application``).
            if any(kick.readout == "decision" for kick in readout):
                self._set(side_index, np.array([
                    apply_readout_decisions(term, readout, side.region.index, side.readout_dephasing_per_us)
                    for term in self.factors[side_index]]))
            bits = self._sample(side_index, measured_slots, progress.generator)
            for kick in readout:
                if kick.readout == "after":
                    self._unitary(side_index, kick_unitary(kick), (side.region.index(kick.qubit),))
            classical = [0] * operation.classical_bits
            for q, bit, target in zip(measured_qubits, bits, targets):
                p10, p01 = progress.assignment[q]
                draw = progress.generator.random()
                classical[target] = int(draw < p10) if bit == 0 else int(draw >= p01)
            label = "".join(str(bit) for bit in classical)
            progress.counts[label] = progress.counts.get(label, 0) + 1
            measurement = shot_length - measure_start
            for slot in measured_slots:
                self.free[side_index][slot] = max(self.free[side_index][slot], actual + measurement)
            progress.duration += measurement
            return
        if kind == "commit":
            self.settle(side_index, when)
            progress.records.append((self.reduced_state(side_index), progress.duration))
            return
        raise ValueError("unknown world action")

    # ----- coupling ---------------------------------------------------------------------------------------

    def _occupy(self, side: int, slots: Sequence[int], when: float, actual: float, busy_us: float) -> None:
        """Bring ``slots`` up to ``actual`` and mark them busy (no idle relaxation) for ``busy_us``."""
        self._touch(side, slots, when)
        self._relax_to(side, slots, actual)
        for slot in slots:
            self.free[side][slot] = max(self.free[side][slot], actual + busy_us)

    def _touch(self, side: int, slots: Sequence[int], time_us: float) -> None:
        if any(slot in self.exposed[side] for slot in slots):
            self._flush(time_us)

    def _flush(self, time_us: float) -> None:
        """Evolve the coupled qubits (ZZ and their idle relaxation, exactly) from ``zz_time`` to ``time_us``."""
        if time_us <= self.zz_time:
            return
        exposed = [(side, slot) for side in (0, 1) for slot in sorted(self.exposed[side])]
        cuts = sorted({self.free[side][slot] for side, slot in exposed
                       if self.zz_time < self.free[side][slot] < time_us} | {self.zz_time, time_us})
        for begin, end in zip(cuts, cuts[1:]):
            idle = {(side, slot) for side, slot in exposed if self.free[side][slot] <= begin}
            for component in self._components:
                self._apply_component(component, end - begin, idle)
            for side, slot in idle:
                self.free[side][slot] = end
        self.zz_time = time_us

    def _apply_component(self, component, elapsed: float, idle) -> None:
        slots_a, slots_b, edges = component
        mask = tuple((0, s) in idle for s in slots_a) + tuple((1, s) in idle for s in slots_b)
        key = (id(component), elapsed, mask)
        factors = self._cache.get(key)
        if factors is None:
            factors = _component_factors(component, elapsed, mask, self.sides)
            if len(self._cache) >= _FACTOR_CACHE_LIMIT:
                self._cache.clear()
            self._cache[key] = factors
        x_factors, y_factors = factors
        a_coef, a_basis = _block_coefficients(self.factors[0], slots_a)
        b_coef, b_basis = _block_coefficients(self.factors[1], slots_b)
        # term (m, k) = X_m(A_k) (x) Y_m(B_k), expressed in the two orthonormal block bases.
        a_new = np.einsum("mde,ker->mkdr", x_factors, a_coef).reshape(-1, a_coef.shape[1] * a_coef.shape[2])
        b_new = np.einsum("mde,ker->mkdr", y_factors, b_coef).reshape(-1, b_coef.shape[1] * b_coef.shape[2])
        a_vectors, b_vectors = self._compress(a_new.T, b_new.T)
        self.factors = (
            _from_blocks(a_vectors.T.reshape(-1, a_coef.shape[1], a_coef.shape[2]) @ a_basis.T,
                         slots_a, len(self.sides[0].region)),
            _from_blocks(b_vectors.T.reshape(-1, b_coef.shape[1], b_coef.shape[2]) @ b_basis.T,
                         slots_b, len(self.sides[1].region)),
        )

    def _compress(self, left: np.ndarray, right: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Best rank of ``sum_j left[:, j] right[:, j]^T`` within the model's limits."""
        q_left, r_left = np.linalg.qr(left)
        q_right, r_right = np.linalg.qr(right)
        u, singular, vh = np.linalg.svd(r_left @ r_right.T)
        if singular.size == 0 or singular[0] == 0.0:
            raise ValueError("the joint state vanished")
        keep = int(np.count_nonzero(singular > _ROUND_OFF * singular[0]))
        if self.settings.model == "low_rank":
            tail = np.sqrt(np.cumsum((singular ** 2)[::-1])[::-1])     # tail[r] = norm dropped when keeping r
            within = [r for r in range(1, keep + 1) if r == keep or tail[r] <= self.settings.truncation]
            keep = min(within[0], self.settings.max_rank)
            dropped = float(tail[keep]) if keep < singular.size else 0.0
            self.stats.dropped_norm += dropped
        self.stats.compressions += 1
        self.stats.max_rank = max(self.stats.max_rank, keep)
        return q_left @ (u[:, :keep] * singular[:keep]), q_right @ vh[:keep].T

    # ----- local operations -------------------------------------------------------------------------------

    def _relax_to(self, side: int, slots, time_us: float) -> None:
        model = self.sides[side].idle_model
        due = []
        for slot in slots:
            if slot in self.exposed[side]:
                continue                    # kept current by the coupling flush
            if time_us > self.free[side][slot]:
                due.append((slot, model.qubits[slot], time_us - self.free[side][slot]))
                self.free[side][slot] = time_us
        if due:
            stack = np.array(self.factors[side], copy=True)
            relax_axes_in_place(stack, due)
            self._set(side, stack)

    def _unitary(self, side: int, operator: np.ndarray, slots: tuple[int, ...]) -> None:
        self._set(side, _apply_stack(self.factors[side], operator, slots))

    def _depolarize(self, side: int, slots: tuple[int, ...], probability: float) -> None:
        stack = self.factors[side]
        mixed = stack
        for slot in slots:
            mixed = _replace_with_mixed(mixed, slot)
        self._set(side, (1.0 - probability) * stack + probability * mixed)

    def _readout(self, job: WorldJob, progress: _Progress, shot: int) -> list:
        """The kicks known now that arrive during this shot's readout of a measured qubit."""
        found = []
        seen = Counter(progress.applied)
        for kick in job.kicks().of_shot(shot):
            if kick.readout is None:
                continue
            if seen[(shot, kick)] > 0:
                seen[(shot, kick)] -= 1
                continue
            progress.applied[(shot, kick)] += 1
            found.append(kick)
        return found

    def _feedback(self, job: WorldJob, progress: _Progress, shot: int, position: int | None) -> list:
        """The kicks known now that arrive in the feedback of this reset (``position`` None: the handover)."""
        found = []
        seen = Counter(progress.applied)
        for kick in job.kicks().of_shot(shot):
            if not kick.in_reset or kick.before_handover != (position is None):
                continue
            if position is not None and kick.position != position:
                continue
            if seen[(shot, kick)] > 0:
                seen[(shot, kick)] -= 1
                continue
            progress.applied[(shot, kick)] += 1
            found.append(kick)
        return found

    def _reset(self, side: int, slots: tuple[int, ...], *, count: int = 1, measures_input: bool = False,
               feedback: Sequence[PlacedKick] = ()):
        """The region's reset model on every factor, by linearity over valid density matrices.

        ``feedback`` kicks arrive between the first attempt's measurement and its X (IBM's reset); they are
        folded into that attempt by ``apply_reset_with_feedback_kick``, which is linear, term by term.
        """
        if feedback:
            return self._reset_with_feedback(side, slots, count, measures_input, feedback)
        spec = self.sides[side]
        size = len(spec.region)
        errors = None
        results = []
        for term in self.factors[side]:
            out = np.zeros_like(term)
            for part, weight in ((0.5 * (term + term.conj().T), 1.0), (-0.5j * (term - term.conj().T), 1.0j)):
                values, vectors = np.linalg.eigh(part)
                for sign in (1.0, -1.0):
                    selected = np.clip(sign * values, 0.0, None)
                    total = float(selected.sum())
                    if total <= 0.0:
                        continue
                    density = (vectors * (selected / total)) @ vectors.conj().T
                    reset = spec.reset_models.run_register(spec.reset_model_id, ResetModelRequest(
                        input_state=freeze_register(density), reset_count=count, victim_measurement=measures_input,
                        subsystem_count=size, targets=slots))
                    errors = reset.assignment_errors
                    out += weight * sign * total * np.asarray(reset.post_reset_state, dtype=complex)
            results.append(out)
        if errors is None:
            raise ValueError("the joint state vanished")
        self._set(side, np.array(results))
        return errors

    def _reset_with_feedback(self, side: int, slots: tuple[int, ...], count: int, measures_input: bool,
                             feedback: Sequence[PlacedKick]):
        spec = self.sides[side]
        model = spec.reset_models.get(spec.reset_model_id)
        if not callable(getattr(model, "local_spec", None)):
            raise ValueError("leaks inside a reset need a reset model with one-qubit channels")
        by_slot: dict[int, np.ndarray] = {}
        plain = []
        for kick in feedback:
            slot = spec.region.index(kick.qubit)
            if slot not in slots or count == 0:
                plain.append((slot, kick_unitary(kick)))
            else:
                by_slot[slot] = kick_unitary(kick) @ by_slot.get(slot, np.eye(2, dtype=complex))
        stack = np.array(self.factors[side], dtype=complex)
        for slot, unitary in plain:
            stack = _apply_stack(stack, unitary, (slot,))
        results = []
        for term in stack:
            if measures_input:
                term = nonselective_measurement(term, slots)
            for attempt in range(count):
                for slot in slots:
                    params = model.local_spec(slot)
                    if params.local_dimension != 2:
                        raise ValueError("leaks inside a reset need two-level reset channels")
                    term = (apply_reset_with_feedback_kick(term, slot, params.success_probability,
                                                           params.target_populations, by_slot[slot])
                            if attempt == 0 and slot in by_slot else
                            apply_local_replacement(term, slot, params.success_probability, params.target_populations))
            results.append(term)
        self._set(side, np.array(results))
        return register_assignment_errors(model, subsystem_count=len(spec.region), targets=slots)

    def _reduced(self, side: int) -> np.ndarray:
        other = self.factors[1 - side]
        weights = np.trace(other, axis1=1, axis2=2)
        return np.tensordot(weights, self.factors[side], axes=1)

    def _sample(self, side: int, slots: tuple[int, ...], generator: random.Random) -> tuple[int, ...]:
        """Draw the outcome as ``sample_measurement`` does, then project this side's factors onto it."""
        state = self._reduced(side)
        n = len(self.sides[side].region)
        probabilities = np.diag(state).real
        if np.any(~np.isfinite(probabilities)) or np.min(probabilities) < -_NEGATIVE_TOLERANCE \
                or abs(float(probabilities.sum()) - 1.0) > 1e-8:
            raise ValueError("invalid measurement probability distribution")
        labels = np.arange(len(state))
        outcomes = np.zeros(len(state), dtype=int)
        for slot in slots:
            outcomes = (outcomes << 1) | ((labels >> (n - 1 - slot)) & 1)
        marginal = np.bincount(outcomes, weights=np.maximum(probabilities, 0.0), minlength=1 << len(slots))
        draw = generator.random() * float(marginal.sum())
        outcome = min(int(np.searchsorted(np.cumsum(marginal), draw, side="right")), len(marginal) - 1)
        probability = float(marginal[outcome])
        if probability <= 0:
            raise ValueError("sampled a zero-probability measurement branch")
        keep = outcomes == outcome
        mask = keep[:, None] & keep[None, :]
        self._set(side, np.where(mask[None], self.factors[side] / probability, 0.0j))
        return tuple(int(bit) for bit in format(outcome, f"0{len(slots)}b"))

    def _set(self, side: int, stack: np.ndarray) -> None:
        self.factors = (stack, self.factors[1]) if side == 0 else (self.factors[0], stack)


# ----- helpers ---------------------------------------------------------------------------------------------


def _components(edges):
    """Group coupled pairs that share a qubit: ``((A slots), (B slots), ((a, b, zeta), ...))`` each."""
    groups: list[tuple[set, set, list]] = []
    for a, b, zeta in edges:
        hits = [g for g in groups if a in g[0] or b in g[1]]
        merged = (set().union(*(g[0] for g in hits), {a}), set().union(*(g[1] for g in hits), {b}),
                  [e for g in hits for e in g[2]] + [(a, b, zeta)])
        groups = [g for g in groups if g not in hits] + [merged]
    result = []
    for slots_a, slots_b, group_edges in groups:
        if len(slots_a) + len(slots_b) > 4:
            raise ValueError("a residual-ZZ cluster may join at most four qubits")
        result.append((tuple(sorted(slots_a)), tuple(sorted(slots_b)), tuple(group_edges)))
    return tuple(result)


_SIGMA_MINUS = np.array([[0, 1], [0, 0]], dtype=complex)   # |0><1|
_SIGMA_Z = np.array([[1, 0], [0, -1]], dtype=complex)


def _component_factors(component, elapsed: float, mask, sides):
    """``exp(L t)`` on the cluster split as ``sum_m X_m (x) Y_m`` (A-side and B-side superoperators on blocks)."""
    slots_a, slots_b, edges = component
    qubits = [(0, s) for s in slots_a] + [(1, s) for s in slots_b]
    count = len(qubits)
    dimension = 1 << count
    position = {qubit: index for index, qubit in enumerate(qubits)}
    energies = np.zeros(dimension)
    for a, b, zeta in edges:
        i, j = position[(0, a)], position[(1, b)]
        for state in range(dimension):
            if (state >> (count - 1 - i)) & 1 and (state >> (count - 1 - j)) & 1:
                energies[state] += zeta
    collapse = []
    for index, ((side, slot), idle) in enumerate(zip(qubits, mask)):
        if not idle:
            continue
        spec = sides[side].idle_model.qubits[slot]
        rates = []
        if math.isfinite(spec.t1_us):
            p = spec.equilibrium_excited_population
            rates += [((1.0 - p) / spec.t1_us, _SIGMA_MINUS), (p / spec.t1_us, _SIGMA_MINUS.T)]
        if math.isfinite(spec.tphi_us):
            rates.append((0.5 / spec.tphi_us, _SIGMA_Z))
        for rate, operator in rates:
            if rate > 0.0:
                collapse.append(math.sqrt(rate) * _embed(operator, index, count))
    generator = np.asarray(gksl_liouvillian_superoperator(np.diag(energies).astype(complex), collapse))
    propagator = dense_matrix_exponential(generator * elapsed)
    # column-major vectorization: index = column * D + row. Split rows/columns into (A part, B part).
    da, db = 1 << len(slots_a), 1 << len(slots_b)
    tensor = propagator.reshape(da, db, da, db, da, db, da, db)  # out col(A,B), out row(A,B), in col, in row
    # -> out row A, out col A, in row A, in col A | out row B, out col B, in row B, in col B
    tensor = tensor.transpose(2, 0, 6, 4, 3, 1, 7, 5).reshape(da ** 4, db ** 4)
    u, singular, vh = np.linalg.svd(tensor)
    keep = int(np.count_nonzero(singular > _ROUND_OFF * singular[0]))
    root = np.sqrt(singular[:keep])
    x_factors = (u[:, :keep] * root).T.reshape(keep, da * da, da * da)
    y_factors = (vh[:keep].T * root).T.reshape(keep, db * db, db * db)
    return x_factors, y_factors


def _embed(operator: np.ndarray, index: int, count: int) -> np.ndarray:
    result = np.eye(1, dtype=complex)
    for position in range(count):
        result = np.kron(result, operator if position == index else np.eye(2, dtype=complex))
    return result


def _block_coefficients(stack: np.ndarray, slots: tuple[int, ...]):
    """Write each factor as blocks over ``slots`` (row and column bits) times an orthonormal rest basis.

    Returns ``coef`` with shape (terms, 4**len(slots), rank) and the basis (rest dimension, rank).
    """
    terms, dimension = stack.shape[0], stack.shape[1]
    n = dimension.bit_length() - 1
    blocks = _to_blocks(stack, slots, n)                      # (terms, d, rest)
    flat = blocks.reshape(-1, blocks.shape[2]).T              # (rest, terms * d)
    basis, triangle = np.linalg.qr(flat)
    coef = triangle.T.reshape(terms, blocks.shape[1], -1)     # (terms, d, rank)
    return coef, basis


def _to_blocks(stack: np.ndarray, slots: tuple[int, ...], n: int) -> np.ndarray:
    terms = stack.shape[0]
    tensor = stack.reshape((terms,) + (2,) * (2 * n))
    front = [1 + s for s in slots] + [1 + n + s for s in slots]
    rest = [axis for axis in range(1, 2 * n + 1) if axis not in front]
    ordered = tensor.transpose([0] + front + rest)
    return ordered.reshape(terms, 1 << (2 * len(slots)), -1)


def _from_blocks(blocks: np.ndarray, slots: tuple[int, ...], n: int) -> np.ndarray:
    terms = blocks.shape[0]
    front = [1 + s for s in slots] + [1 + n + s for s in slots]
    rest = [axis for axis in range(1, 2 * n + 1) if axis not in front]
    order = [0] + front + rest
    tensor = blocks.reshape((terms,) + (2,) * (2 * n)).transpose(np.argsort(order))
    return np.ascontiguousarray(tensor.reshape(terms, 1 << n, 1 << n))


def _apply_stack(stack: np.ndarray, operator: np.ndarray, slots: tuple[int, ...]) -> np.ndarray:
    """``U X U^dagger`` on every factor ``X`` of the stack (qubit 0 is the most significant)."""
    terms, dimension = stack.shape[0], stack.shape[1]
    n = dimension.bit_length() - 1
    tensor = stack.reshape((terms,) + (2,) * (2 * n))
    for axes, transform in (([1 + s for s in slots], operator), ([1 + n + s for s in slots], operator.conj())):
        order = axes + [axis for axis in range(2 * n + 1) if axis not in axes]
        ordered = tensor.transpose(order)
        updated = transform @ ordered.reshape(len(transform), -1)
        tensor = updated.reshape(ordered.shape).transpose(np.argsort(order))
    return np.ascontiguousarray(tensor.reshape(stack.shape))


def _replace_with_mixed(stack: np.ndarray, slot: int) -> np.ndarray:
    """``I/2 (x) Tr_slot(X)`` on every factor, as ``apply_local_replacement`` with success 1."""
    terms, dimension = stack.shape[0], stack.shape[1]
    n = dimension.bit_length() - 1
    tensor = stack.reshape((terms,) + (2,) * (2 * n))
    reduced = np.trace(tensor, axis1=1 + slot, axis2=1 + n + slot)
    replaced = np.zeros_like(tensor)
    for level in (0, 1):
        index = [slice(None)] * (2 * n + 1)
        index[1 + slot] = index[1 + n + slot] = level
        replaced[tuple(index)] = 0.5 * reduced
    return replaced.reshape(stack.shape)
