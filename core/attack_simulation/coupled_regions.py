"""Joint simulation of two concurrent regions coupled by residual ZZ (D85) or by exchange (D91).

A coupling between qubits of two tenants' regions entangles the regions, so the regions can no longer be
advanced one job at a time from separate states (D81). Coupled regions are instead advanced together, in
time order, on one joint state.

The coupling is declared either as measured ZZ shifts (``ResidualZZSettings``) or as device values
(``CoupledTransmonSettings``: frequency, anharmonicity and exchange ``J``), from which the static shifts of
every coupled cluster follow (``TransmonCluster``). Both give each cluster one energy per basis state.

The joint state is kept as a sum of products ``rho = sum_k A_k (x) B_k`` (operator Schmidt form). Local
operations act on each factor; only the coupling raises the number of terms, and a compression after each
coupling step keeps it bounded. ``exact`` keeps every term above round-off; ``low_rank`` also drops the
smallest terms (D79), and the dropped Hilbert-Schmidt norm is accumulated as the error bound.

Physics in this module:

* ZZ is ``H = sum zeta n_i n_j`` over the declared cross-region pairs (the conditional-shift convention of
  ``ResidualZZCoupling``): qubit ``j`` runs ``zeta`` faster in angle while qubit ``i`` is in |1>. From device
  values the cluster's Hamiltonian is the dressed frame's diagonal on the qubit labels: the same pair shifts,
  plus the shifts that only appear with three or more qubits excited.
* The coupled qubits (the "exposed" ones) evolve under ZZ together with their idle relaxation, with the
  exact superoperator of each interval (no splitting error). A qubit inside an instruction is busy: it does
  not relax, but ZZ keeps acting on it. Instructions themselves are instantaneous at their start time.
* Every other qubit relaxes lazily, just before it is next touched, as in the single-region runtime (D85
  idle relaxation); local channels on other qubits commute with ZZ, so this is exact.
* A qubit may have more levels than two (D91; the levels are its idle model's). Gates and leaks act on its
  qubit levels, a reset lets a leaked level through, and with device values the cluster's dressed energies
  cover every level the world holds. A measured ZZ and declared readout crosstalk describe two levels only,
  so they are refused on a qubit with more.

* Reading out a qubit disturbs its declared neighbours for as long as the readout lasts (extra dephasing and
  a frequency shift, ``ReadoutCrosstalk``), in either region and also during a reset's measurement. These
  commute with the coupling and with idle relaxation, so each neighbour takes what has accrued just before it
  is next touched. Where the reader's assigned result depends on a neighbour's state, drawing the result is
  a weak measurement of that neighbour: the joint state is updated with what the result tells.

The per-shot semantics mirror ``SharedQpuRuntime._run_before_measurement`` and ``_execute_circuit``: with no
coupling, the reduced states and counts are those of the region-by-region path.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import cmath
import heapq
import math
import random

import numpy as np

from core.attack_simulation.circuit_program import CircuitExecutionOperation
from core.attack_simulation.pulse_shape import SQUARE_SHAPE, PulseShape
from core.attack_simulation.kick_application import (
    EMPTY_PLAN, KickPlan, PlacedKick, apply_readout_decisions, kick_unitary,
)
from core.attack_simulation.register_readout import (
    assign_bit, assigned_one_probability, register_assignment_errors,
)
from core.attack_simulation.shot_activity import instruction_starts
from core.cptp_liouvillian import dense_matrix_exponential, gksl_liouvillian_superoperator
from core.experimental_isolation_physics.coherent_crosstalk import DriveCrosstalkTransfer, ResidualZZCoupling
from core.experimental_isolation_physics.pulse_calibration import (
    CalibratedPulse, calibrate_pulse, relaxation_error, residual_survival,
)
from core.experimental_isolation_physics.idle_evolution import (
    IndependentIdleModel, ladder_relaxation_operators, relax_axes_in_place,
)
from core.experimental_isolation_physics.readout_crosstalk import ReadoutCrosstalk
from core.experimental_isolation_physics.reset_model import ResetModelRegistry, ResetModelRequest
from core.experimental_isolation_physics.transmon_cluster import ClusterDrive, TransmonCluster, TransmonSpec, _turn
from core.pulse_transmon_network import TransmonExchangeCoupling
from core.register_operations import (
    apply_local_depolarizing_replacement, apply_local_replacement, apply_reset_with_feedback_kick,
    embed_qubit_operator, freeze_register, level_digits, level_outcomes, nonselective_measurement,
)

ZZ_MODELS = ("low_rank", "exact")
TRANSMON_SOLVERS = ("effective", "reference")   # how a cluster declared by device values is advanced (D91)
MAX_CLUSTER_QUBITS = 4
EXACT_MAX_UNION_QUBITS = 8          # the dense-computation limit; ``exact`` is only offered up to it (D79)
EXACT_MAX_UNION_DIMENSION = 1 << EXACT_MAX_UNION_QUBITS   # the same limit for subsystems with more levels
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
class CoupledTransmonSettings:
    """Frozen device values of the coupled qubits and of their exchange couplings (D91).

    ``transmons`` gives every coupled physical qubit its measured frequency and anharmonicity; ``exchange``
    is ``(physical qubit, physical qubit, J in rad/us)`` per coupled pair. A pair may lie inside one region,
    but every cluster of coupled qubits must reach both regions. ``solver="effective"`` advances a cluster
    with the static shifts of its dressed frame, computed with the declared levels and used on the qubit
    levels; the rest is the approximation model of ``ResidualZZSettings``.

    ``solver="reference"`` advances the cluster at pulse level while one of its qubits plays a pulse
    (``TransmonCluster.propagator``): the drive reaches the neighbours through the exchange (cross-resonance)
    and through the declared classical leaks between the cluster's lines, it can leave the qubit levels, and
    relaxation acts during it. A calibrated gate plays the pulse calibrated on the model
    (``calibrate_pulse``); a pulse the tenant plays directly is played as stated. The calibrated gate error is
    then added only for the part relaxation does not explain (``residual_survival``). The world must hold every
    cluster qubit with the levels its ``TransmonSpec`` declares.
    """

    transmons: tuple[tuple[int, TransmonSpec], ...]
    exchange: tuple[tuple[int, int, float], ...]
    solver: str = "effective"
    model: str = "low_rank"
    max_rank: int = 32
    truncation: float = 1e-6

    def __post_init__(self) -> None:
        transmons = tuple((qubit, spec) for qubit, spec in self.transmons)
        qubits = [qubit for qubit, _ in transmons]
        if (not transmons or any(type(qubit) is not int or qubit < 0 or type(spec) is not TransmonSpec
                                 for qubit, spec in transmons) or len(set(qubits)) != len(qubits)):
            raise ValueError("transmons must give each coupled physical qubit one TransmonSpec")
        object.__setattr__(self, "transmons", transmons)
        exchange = tuple((left, right, float(strength)) for left, right, strength in self.exchange)
        pairs = [frozenset((left, right)) for left, right, _ in exchange]
        if (not exchange or len(set(pairs)) != len(pairs)
                or any(len(pair) != 2 or not pair <= set(qubits) for pair in pairs)
                or any(not math.isfinite(strength) or strength < 0.0 for _, _, strength in exchange)):
            raise ValueError("exchange couplings join two declared transmons, once each, with J >= 0")
        object.__setattr__(self, "exchange", exchange)
        if self.solver not in TRANSMON_SOLVERS:
            raise ValueError("unknown coupled-transmon solver")
        if self.model not in ZZ_MODELS:
            raise ValueError("unknown approximation model")
        if type(self.max_rank) is not int or self.max_rank < 1:
            raise ValueError("max_rank must be a positive integer")
        truncation = float(self.truncation)
        if not math.isfinite(truncation) or truncation < 0.0:
            raise ValueError("truncation must be finite and nonnegative")
        object.__setattr__(self, "truncation", truncation)


@dataclass(frozen=True)
class UncoupledSettings:
    """Two regions with no coupling Hamiltonian, joined only by what a readout learns about a neighbour.

    The joint state is then a mixture of products and needs few terms; the limits are those of the
    approximation model of ``ResidualZZSettings``.
    """

    model: str = "low_rank"
    max_rank: int = 32
    truncation: float = 1e-6

    def __post_init__(self) -> None:
        if self.model not in ZZ_MODELS:
            raise ValueError("unknown approximation model")
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
    pulses: tuple = ()              # the pulses of cluster qubits, absolute times (reference solver)

    def copy(self) -> "_Progress":
        generator = random.Random()
        generator.setstate(self.generator.getstate())
        return _Progress(self.actions, self.layout, self.pointer, self.last_seq, self.last_time, Counter(self.applied), generator,
                         dict(self.counts), list(self.records), dict(self.assignment), self.duration,
                         list(self.classical), self.first_shot_us, self.handover, self.pulses)

    @property
    def done(self) -> bool:
        return self.pointer >= len(self.actions)


@dataclass(frozen=True)
class _ClusterPulse:
    """One pulse on a cluster qubit's line, in world time. ``phase_rad`` is the hardware carrier phase at start."""

    side: int
    slot: int
    start_us: float
    duration_us: float              # as declared (``end_us - start_us`` may differ from it by round-off)
    angle_rad: float
    phase_rad: float
    shape: PulseShape
    frequency_offset_rad_per_us: float
    calibrated: bool

    @property
    def end_us(self) -> float:
        return self.start_us + self.duration_us


@dataclass
class WorldStats:
    compressions: int = 0
    max_rank: int = 1
    dropped_norm: float = 0.0       # sum of the Hilbert-Schmidt norms dropped by truncation (error bound)
    clipped_eigenvalue: float = 0.0  # most negative reduced-state eigenvalue removed before a record
    late_kicks: int = 0             # leaks that became known after their place had been simulated


class CoupledRegionWorld:
    """Two regions advanced together on one joint state. Copy before advancing a candidate."""

    def __init__(self, sides: tuple[RegionSide, RegionSide],
                 settings: ResidualZZSettings | CoupledTransmonSettings | UncoupledSettings,
                 initial_states: tuple[np.ndarray, np.ndarray], *, repetition_delay_us: float,
                 readout_crosstalk: Sequence[ReadoutCrosstalk] = (),
                 transfers: Sequence[DriveCrosstalkTransfer] = (), pulse_shape: PulseShape = SQUARE_SHAPE) -> None:
        if len(sides) != 2 or set(sides[0].region) & set(sides[1].region):
            raise ValueError("coupled regions must be two disjoint regions")
        self.sides = tuple(sides)
        self.settings = settings
        self.repetition_delay_us = float(repetition_delay_us)
        self._reference: tuple = ()              # per component: (TransmonCluster, its (side, slot) qubits)
        sizes = tuple(len(side.region) for side in sides)
        self.dims = tuple(side.idle_model.dims for side in sides)
        if any(len(dims) != size for dims, size in zip(self.dims, sizes)):
            raise ValueError("a region's idle model must cover its qubits")
        if settings.model == "exact" and math.prod(self.dims[0]) * math.prod(self.dims[1]) > EXACT_MAX_UNION_DIMENSION:
            raise ValueError("the exact residual-ZZ model is limited to two regions of at most 8 qubits together")
        location = {q: (s, side.region.index(q)) for s, side in enumerate(sides) for q in side.region}
        if type(settings) is ResidualZZSettings:
            edges = []
            for coupling in settings.couplings:
                ends = (location.get(coupling.left), location.get(coupling.right))
                if None in ends or ends[0][0] == ends[1][0]:
                    raise ValueError("a residual-ZZ pair must join the two regions")
                if any(self.dims[side][slot] != 2 for side, slot in ends):
                    raise ValueError("a measured ZZ says nothing about a leaked level; declare device values instead")
                edges.append((*sorted(ends), float(coupling.conditional_shift_rad_per_us)))
            self._components = tuple((slots_a, slots_b, _pair_shift_energies(slots_a, slots_b, group))
                                     for slots_a, slots_b, group in _clusters(edges))
        elif type(settings) is CoupledTransmonSettings:
            specs = dict(settings.transmons)
            if any(qubit not in location for qubit in specs):
                raise ValueError("a coupled transmon lies outside the two regions")
            edges = [(*sorted((location[left], location[right])), strength)
                     for left, right, strength in settings.exchange]
            physical = {place: qubit for qubit, place in location.items()}
            groups = _clusters(edges)
            self._components = tuple(
                (slots_a, slots_b, _device_energies(slots_a, slots_b, group, lambda place: specs[physical[place]],
                                                    self.dims))
                for slots_a, slots_b, group in groups)
            if settings.solver == "reference":
                self._reference = tuple(self._reference_cluster(slots_a, slots_b, group, specs, physical, transfers)
                                        for slots_a, slots_b, group in groups)
        elif type(settings) is UncoupledSettings:
            self._components = ()
        else:
            raise TypeError("coupled regions need ResidualZZSettings, CoupledTransmonSettings or UncoupledSettings")
        readers: dict = {}
        for item in readout_crosstalk:
            if type(item) is not ReadoutCrosstalk:
                raise TypeError("readout_crosstalk must contain ReadoutCrosstalk values")
            if item.reader not in location or item.neighbour not in location:
                raise ValueError("readout crosstalk must join qubits of the two regions")
            if any(self.dims[side][slot] != 2 for side, slot in (location[item.reader], location[item.neighbour])):
                raise ValueError("measured readout crosstalk describes two-level qubits")
            entries = readers.setdefault(location[item.reader], [])
            if any(place == location[item.neighbour] for place, _ in entries):
                raise ValueError("a readout-crosstalk pair is declared twice")
            entries.append((location[item.neighbour], item))
        self._readers = {place: tuple(entries) for place, entries in readers.items()}
        # Readouts still acting on a neighbour: [side, slot, applied up to, end, Gamma + i shift].
        self.exposures: list[list] = []
        self.exposed = (frozenset(q for comp in self._components for q in comp[0]),
                        frozenset(q for comp in self._components for q in comp[1]))
        if type(pulse_shape) is not PulseShape:
            raise TypeError("a PulseShape is required")
        self.pulse_shape = pulse_shape
        self._cluster_of = {qubit: index for index, (_, qubits) in enumerate(self._reference) for qubit in qubits}
        self.frames: dict[tuple[int, int], float] = {}       # virtual-Z frame of each cluster qubit in its shot
        self._calibrations: dict = {}
        self.factors = tuple(np.asarray(state, dtype=complex).reshape(1, *np.shape(state)).copy()
                             for state in initial_states)
        for factor, dims in zip(self.factors, self.dims):
            if factor.shape != (1, math.prod(dims), math.prod(dims)):
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
        other.dims = self.dims
        other._reference, other._cluster_of, other._calibrations = self._reference, self._cluster_of, self._calibrations
        other.pulse_shape, other.frames = self.pulse_shape, dict(self.frames)
        other._components, other.exposed, other._cache = self._components, self.exposed, self._cache
        other._readers, other.exposures = self._readers, [list(entry) for entry in self.exposures]
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
                elif self._pulse_level(job, instruction):
                    actions.append((begin + start, (shot, index, 1, 0), "gate", (shot, index)))
                    cursor = start
                    for order, step in enumerate(instruction.natives):
                        if step.is_virtual:
                            actions.append((begin + cursor, (shot, index, 1, 1 + order), "frame",
                                            (job.slots[instruction.qubits[0]], step.angle_rad)))
                        else:
                            cursor += step.duration_us
                elif instruction.kind != "measure":
                    actions.append((begin + start, (shot, index, 1, 0), "instruction", (shot, index)))
            actions.append((begin + measure_start, (shot, count, 2, 0), "measure", shot))
            actions.append((begin + shot_length, (shot, count + 1, 1, 0), "commit", shot))
        pulses = self._job_pulses(job, starts, first, shot_length) if self._reference else ()
        return _Progress(actions, (starts, measure_start, shot_length),
                         generator=random.Random(operation.sampling_seed),
                         counts={"0": 0, "1": 0} if operation.classical_bits == 1 else {}, first_shot_us=first,
                         pulses=pulses)

    # ----- pulse level (reference solver) ----------------------------------------------------------------

    def _reference_cluster(self, slots_a, slots_b, group, specs, physical, transfers):
        qubits = [(0, s) for s in slots_a] + [(1, s) for s in slots_b]
        position = {qubit: index for index, qubit in enumerate(qubits)}
        cluster_specs = tuple(specs[physical[qubit]] for qubit in qubits)
        for (side, slot), spec in zip(qubits, cluster_specs):
            if self.dims[side][slot] != spec.levels:
                raise ValueError("the reference solver needs the world to hold every level a coupled transmon has")
            idle = self.sides[side].idle_model.qubits[slot]
            if (spec.levels > 2 and idle.equilibrium_excited_population > 0.0 and math.isfinite(idle.t1_us)
                    and (idle.frequency_ghz != spec.frequency_ghz or idle.anharmonicity_mhz != spec.anharmonicity_mhz)):
                raise ValueError("a coupled transmon's idle model must state the same frequency and anharmonicity")
        by_physical = {physical[qubit]: index for qubit, index in position.items()}
        line_transfers = tuple((by_physical[item.source], by_physical[item.target], item.coefficient)
                               for item in transfers
                               if item.source in by_physical and item.target in by_physical and item.coefficient != 0.0)
        cluster = TransmonCluster(
            cluster_specs,
            tuple(TransmonExchangeCoupling(position[left], position[right], strength) for left, right, strength in group),
            line_transfers)
        return cluster, tuple(qubits)

    def _pulse_level(self, job: WorldJob, instruction) -> bool:
        """A single-qubit gate of a cluster qubit, which the reference solver plays as pulses."""
        if not self._reference or instruction.kind != "unitary" or len(instruction.qubits) != 1:
            return False
        if (job.side, job.slots[instruction.qubits[0]]) not in self._cluster_of:
            return False
        if not instruction.natives:
            raise ValueError("the reference solver plays a cluster qubit's gates as pulses: they need native steps")
        return True

    def _job_pulses(self, job: WorldJob, starts, first: float, shot_length: float) -> tuple:
        """Every pulse the job plays on cluster qubits, for every shot, at world times."""
        relative = []
        frames: dict[int, float] = {}
        for instruction, start in zip(job.operation.instructions, starts):
            if instruction.kind != "unitary" or len(instruction.qubits) != 1 or not instruction.natives:
                continue
            qubit = instruction.qubits[0]
            slot = job.slots[qubit]
            cursor = start
            for step in instruction.natives:
                if step.is_virtual:
                    frames[qubit] = frames.get(qubit, 0.0) + step.angle_rad
                    continue
                if (job.side, slot) in self._cluster_of:
                    relative.append((slot, cursor, step.duration_us, step.angle_rad, -frames.get(qubit, 0.0),
                                     self.pulse_shape if step.shape is None else step.shape,
                                     step.frequency_offset_rad_per_us, step.shape is None))
                cursor += step.duration_us
        pulses = []
        for shot in range(job.operation.shots):
            begin = first + shot * (shot_length + self.repetition_delay_us)
            pulses.extend(_ClusterPulse(job.side, slot, begin + low, length, angle, phase, shape, offset, calibrated)
                          for slot, low, length, angle, phase, shape, offset, calibrated in relative)
        return tuple(pulses)

    def _calibration(self, component: int, line: int, angle: float, duration: float,
                     shape: PulseShape) -> CalibratedPulse:
        key = (component, line, angle, duration, shape)
        if key not in self._calibrations:
            self._calibrations[key] = calibrate_pulse(self._reference[component][0], line, angle, duration, shape)
        return self._calibrations[key]

    def _drives(self, component: int, pulses) -> tuple[ClusterDrive, ...]:
        cluster, qubits = self._reference[component]
        drives = []
        for pulse in pulses:
            line = qubits.index((pulse.side, pulse.slot))
            duration = pulse.duration_us
            angle, drag = pulse.angle_rad, 0.0
            if pulse.calibrated:
                calibration = self._calibration(component, line, pulse.angle_rad, duration, pulse.shape)
                angle, drag = calibration.amplitude_scale * pulse.angle_rad, calibration.drag_us
            # The carrier's phase is counted from the pulse's start (``direct_pulse``); the cluster's from time 0.
            offset = pulse.frequency_offset_rad_per_us
            drives.append(ClusterDrive(line, pulse.start_us, duration, angle, pulse.shape,
                                       phase_rad=pulse.phase_rad + offset * pulse.start_us,
                                       frequency_offset_rad_per_us=offset, drag_us=drag))
        return tuple(drives)

    def _apply_driven(self, component: int, begin: float, end: float, idle, pulses) -> None:
        """The cluster's map over a piece in which some of its qubits play pulses, in the simulator's frame."""
        cluster, qubits = self._reference[component]
        relaxation = tuple(self.sides[side].idle_model.qubits[slot] if (side, slot) in idle else None
                           for side, slot in qubits)
        propagator = cluster.propagator(begin, end, self._drives(component, pulses), relaxation)
        # The world's state is turned by each qubit's virtual-Z frame: rho_sim = F rho_hw F^+, F = exp(i theta n).
        angles = np.array(cluster.labels, dtype=float) @ np.array([self.frames.get(qubit, 0.0) for qubit in qubits])
        turn = _turn(angles)
        propagator = turn[:, None] * propagator * np.conj(turn)[None, :]
        slots_a, slots_b, _ = self._components[component]
        dims = tuple(cluster.levels)
        self._apply_factors(_split_superoperator(propagator, math.prod(dims[:len(slots_a)]),
                                                 math.prod(dims[len(slots_a):])), slots_a, slots_b)

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
                self._announce_readout(side_index, targets, actual, min(side.measurement_us, side.reset_us))
            else:
                self._touch(side_index, targets, when)
                self._expose(side_index, targets, actual)
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
                # A reset is a measurement and then the feedback: its measurement reads the qubit out.
                self._announce_readout(side_index, operands, actual, min(side.measurement_us, side.reset_us))
                progress.duration += side.reset_us
            else:
                self._touch(side_index, operands, when)
                self._expose(side_index, operands, actual)
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
            for slot in job.slots:                  # the hardware frame starts each shot at zero
                self.frames.pop((side_index, slot), None)
            if operation.initialization == "inherit":
                errors = register_assignment_errors(side.reset_models.get(side.reset_model_id),
                                                    subsystem_count=len(side.region), targets=job.slots)
                progress.assignment.update(enumerate(errors))
            return
        if kind == "gate":
            # A cluster qubit's gate, played as pulses by the flush; here only the error the pulses do not
            # explain, and the bookkeeping. The qubit is not busy: relaxation acts during its pulses.
            _, index = payload
            instruction = operation.instructions[index]
            slot = job.slots[instruction.qubits[0]]
            self._touch(side_index, (slot,), when)
            self._relax_to(side_index, (slot,), actual)
            if instruction.depolarizing_probability:
                played = sum(step.duration_us for step in instruction.natives if not step.is_virtual)
                residual = residual_survival(instruction.depolarizing_probability,
                                             relaxation_error(side.idle_model.qubits[slot], played))
                if residual > 0.0:
                    self._depolarize(side_index, (slot,), residual)
            progress.duration += instruction.duration_us
            return
        if kind == "frame":
            slot, angle = payload
            self._touch(side_index, (slot,), when)
            self._relax_to(side_index, (slot,), actual)
            levels = self.dims[side_index][slot]
            self._set(side_index, _apply_stack(self.factors[side_index],
                                               np.diag(np.exp(1j * angle * np.arange(levels))), (slot,),
                                               self.dims[side_index]))
            self.frames[(side_index, slot)] = self.frames.get((side_index, slot), 0.0) + angle
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
                    apply_readout_decisions(term, readout, side.region.index, side.readout_dephasing_per_us,
                                            self.dims[side_index])
                    for term in self.factors[side_index]]))
            levels = self._sample(side_index, measured_slots, progress.generator)
            for kick in readout:
                if kick.readout == "after":
                    self._unitary(side_index, kick_unitary(kick), (side.region.index(kick.qubit),))
            classical = [0] * operation.classical_bits
            for q, level, target, slot in zip(measured_qubits, levels, targets, measured_slots):
                channel = progress.assignment[q]
                draw = progress.generator.random()
                if any(item.reads for _, item in self._readers.get((side_index, slot), ())):
                    classical[target] = self._observe(side_index, slot, level, channel, draw)
                else:
                    classical[target] = assign_bit(channel, level, draw)
            # Each block of final measurements reads its qubits for one measurement time, one block after another.
            groups = list(dict.fromkeys(i.time_group for i in measurements))
            for instruction in measurements:
                self._announce_readout(side_index, tuple(job.slots[q] for q in instruction.qubits),
                                       actual + groups.index(instruction.time_group) * side.measurement_us,
                                       side.measurement_us)
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
        pulses = ([pulse for progress in self.progress.values() for pulse in progress.pulses
                   if pulse.end_us > self.zz_time and pulse.start_us < time_us] if self._reference else [])
        cuts = sorted({self.free[side][slot] for side, slot in exposed
                       if self.zz_time < self.free[side][slot] < time_us} | {self.zz_time, time_us}
                      | {edge for pulse in pulses for edge in (pulse.start_us, pulse.end_us)
                         if self.zz_time < edge < time_us})
        for begin, end in zip(cuts, cuts[1:]):
            idle = {(side, slot) for side, slot in exposed if self.free[side][slot] <= begin}
            middle = 0.5 * (begin + end)
            for index, component in enumerate(self._components):
                active = [pulse for pulse in pulses if pulse.start_us <= middle < pulse.end_us
                          and self._cluster_of.get((pulse.side, pulse.slot)) == index]
                if active:
                    self._apply_driven(index, begin, end, idle, active)
                else:
                    self._apply_component(component, end - begin, idle)
            for side, slot in idle:
                self.free[side][slot] = end
        self.zz_time = time_us

    def _apply_component(self, component, elapsed: float, idle) -> None:
        slots_a, slots_b, _ = component
        mask = tuple((0, s) in idle for s in slots_a) + tuple((1, s) in idle for s in slots_b)
        key = (id(component), elapsed, mask)
        factors = self._cache.get(key)
        if factors is None:
            factors = _component_factors(component, elapsed, mask, self.sides)
            if len(self._cache) >= _FACTOR_CACHE_LIMIT:
                self._cache.clear()
            self._cache[key] = factors
        self._apply_factors(factors, slots_a, slots_b)

    def _apply_factors(self, factors, slots_a, slots_b) -> None:
        """Apply a cluster map given as ``sum_m X_m (x) Y_m`` (A-side and B-side superoperators on blocks)."""
        x_factors, y_factors = factors
        a_coef, a_basis = _block_coefficients(self.factors[0], slots_a, self.dims[0])
        b_coef, b_basis = _block_coefficients(self.factors[1], slots_b, self.dims[1])
        # term (m, k) = X_m(A_k) (x) Y_m(B_k), expressed in the two orthonormal block bases.
        a_new = np.einsum("mde,ker->mkdr", x_factors, a_coef).reshape(-1, a_coef.shape[1] * a_coef.shape[2])
        b_new = np.einsum("mde,ker->mkdr", y_factors, b_coef).reshape(-1, b_coef.shape[1] * b_coef.shape[2])
        a_vectors, b_vectors = self._compress(a_new.T, b_new.T)
        self.factors = (
            _from_blocks(a_vectors.T.reshape(-1, a_coef.shape[1], a_coef.shape[2]) @ a_basis.T,
                         slots_a, self.dims[0]),
            _from_blocks(b_vectors.T.reshape(-1, b_coef.shape[1], b_coef.shape[2]) @ b_basis.T,
                         slots_b, self.dims[1]),
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

    def _announce_readout(self, side: int, slots: Sequence[int], begin_us: float, duration_us: float) -> None:
        """Reading ``slots`` out over the window disturbs their declared neighbours for as long."""
        if duration_us <= 0.0:
            return
        for slot in slots:
            for (other, target), item in self._readers.get((side, slot), ()):
                if item.disturbs:
                    self.exposures.append([other, target, begin_us, begin_us + duration_us,
                                           complex(item.dephasing_per_us, item.frequency_shift_rad_per_us)])

    def _expose(self, side: int, slots, time_us: float) -> None:
        """Give ``slots`` what the readouts announced so far have done to them up to ``time_us``."""
        if not self.exposures:
            return
        wanted = set(slots)
        stack = None
        for entry in self.exposures:
            reach = min(time_us, entry[3])
            if entry[0] != side or entry[1] not in wanted or reach <= entry[2]:
                continue
            stack = _scale_coherence(self.factors[side] if stack is None else stack, entry[1],
                                     cmath.exp(-entry[4] * (reach - entry[2])), self.dims[side])
            entry[2] = reach
        if stack is not None:
            self._set(side, stack)
            self.exposures = [entry for entry in self.exposures if entry[2] < entry[3]]

    def _observe(self, side: int, slot: int, bit: int, channel: tuple[float, ...], draw: float) -> int:
        """Assign the reader's result when it depends on neighbours, and update them with what it tells.

        ``P(assigned 1)`` is the reader's own value for its true ``bit`` plus the declared shift of every
        neighbour in |1>. The assigned bit is drawn from the joint populations; the joint state then takes the
        square root of that likelihood on the neighbours' basis states (a measurement that only reads
        populations), which is what conditioning on the result means.
        """
        shifts = [np.zeros(math.prod(dims)) for dims in self.dims]
        for (other, target), item in self._readers[(side, slot)]:
            if item.reads:
                shifts[other] += item.excited_assignment_shift[bit] * level_digits(self.dims[other])[:, target]
        excited = assigned_one_probability(channel, bit) + shifts[0][:, None] + shifts[1][None, :]
        if excited.min() < -1e-12 or excited.max() > 1.0 + 1e-12:
            raise ValueError("the declared assignment shifts take an assignment probability outside [0, 1]")
        excited = np.clip(excited, 0.0, 1.0)
        populations = np.einsum("kaa,kbb->ab", self.factors[0], self.factors[1]).real
        probability = float((excited * populations).sum() / populations.sum())
        assigned = int(draw < probability) if bit == 0 else int(draw >= 1.0 - probability)
        weight = probability if assigned else 1.0 - probability
        if weight <= 0.0:
            raise ValueError("sampled a zero-probability assignment")
        self._weigh(np.sqrt((excited if assigned else 1.0 - excited) / weight))
        return assigned

    def _weigh(self, root: np.ndarray) -> None:
        """``rho -> K rho K`` with ``K`` diagonal: ``root[a, b]`` on basis state ``a`` of region 0 and ``b`` of 1."""
        u, singular, vh = np.linalg.svd(root)
        keep = max(1, int(np.count_nonzero(singular > _ROUND_OFF * singular[0])))
        left, right = u[:, :keep] * np.sqrt(singular[:keep]), vh[:keep].T * np.sqrt(singular[:keep])
        a, b = self.factors
        if keep == 1:                       # K is a product: no new terms
            self.factors = (a * np.outer(left[:, 0], left[:, 0])[None], b * np.outer(right[:, 0], right[:, 0])[None])
            return
        # K = sum_m diag(left_m) (x) diag(right_m), so term (m, n, k) is left_m A_k left_n (x) right_m B_k right_n.
        a_new = np.einsum("im,kij,jn->mnkij", left, a, left).reshape(-1, a.shape[1] * a.shape[2])
        b_new = np.einsum("im,kij,jn->mnkij", right, b, right).reshape(-1, b.shape[1] * b.shape[2])
        a_vectors, b_vectors = self._compress(a_new.T, b_new.T)
        self.factors = (np.ascontiguousarray(a_vectors.T.reshape(-1, a.shape[1], a.shape[2])),
                        np.ascontiguousarray(b_vectors.T.reshape(-1, b.shape[1], b.shape[2])))

    def _relax_to(self, side: int, slots, time_us: float) -> None:
        slots = tuple(slots)
        self._expose(side, slots, time_us)
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
            relax_axes_in_place(stack, due, self.dims[side])
            self._set(side, stack)

    def _unitary(self, side: int, operator: np.ndarray, slots: tuple[int, ...]) -> None:
        """An operator on the qubit levels of ``slots`` (a leaked level is left alone), on every factor."""
        self._set(side, _apply_stack(self.factors[side], operator, slots, self.dims[side]))

    def _depolarize(self, side: int, slots: tuple[int, ...], probability: float) -> None:
        dims = self.dims[side]
        self._set(side, np.array([apply_local_depolarizing_replacement(term, slots, probability, dims)
                                  for term in self.factors[side]]))

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
                        subsystem_count=size, targets=slots, dims=self.dims[side]))
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
        dims = self.dims[side]
        stack = np.array(self.factors[side], dtype=complex)
        for slot, unitary in plain:
            stack = _apply_stack(stack, unitary, (slot,), dims)
        identity = np.eye(2, dtype=complex)
        results = []
        for term in stack:
            if measures_input:
                term = nonselective_measurement(term, slots, dims)
            for attempt in range(count):
                for slot in slots:
                    params = model.local_spec(slot)
                    if params.local_dimension != 2:
                        raise ValueError("leaks inside a reset need two-level reset channels")
                    if (attempt == 0 and slot in by_slot) or dims[slot] > 2:
                        # On more levels the measurement-based form is the reset itself (a leaked level passes).
                        term = apply_reset_with_feedback_kick(
                            term, slot, params.success_probability, params.target_populations,
                            by_slot.get(slot, identity) if attempt == 0 else identity, dims)
                    else:
                        term = apply_local_replacement(term, slot, params.success_probability,
                                                       params.target_populations, dims)
            results.append(term)
        self._set(side, np.array(results))
        return register_assignment_errors(model, subsystem_count=len(spec.region), targets=slots)

    def _reduced(self, side: int) -> np.ndarray:
        other = self.factors[1 - side]
        weights = np.trace(other, axis1=1, axis2=2)
        return np.tensordot(weights, self.factors[side], axes=1)

    def _sample(self, side: int, slots: tuple[int, ...], generator: random.Random) -> tuple[int, ...]:
        """Draw the levels as ``sample_measurement`` does, then project this side's factors onto them."""
        state = self._reduced(side)
        dims = self.dims[side]
        probabilities = np.diag(state).real
        if np.any(~np.isfinite(probabilities)) or np.min(probabilities) < -_NEGATIVE_TOLERANCE \
                or abs(float(probabilities.sum()) - 1.0) > 1e-8:
            raise ValueError("invalid measurement probability distribution")
        outcomes, count = level_outcomes(dims, slots)
        marginal = np.bincount(outcomes, weights=np.maximum(probabilities, 0.0), minlength=count)
        draw = generator.random() * float(marginal.sum())
        outcome = min(int(np.searchsorted(np.cumsum(marginal), draw, side="right")), len(marginal) - 1)
        probability = float(marginal[outcome])
        if probability <= 0:
            raise ValueError("sampled a zero-probability measurement branch")
        keep = outcomes == outcome
        mask = keep[:, None] & keep[None, :]
        self._set(side, np.where(mask[None], self.factors[side] / probability, 0.0j))
        levels = []
        for slot in reversed(slots):
            outcome, level = divmod(outcome, dims[slot])
            levels.append(level)
        return tuple(reversed(levels))

    def _set(self, side: int, stack: np.ndarray) -> None:
        self.factors = (stack, self.factors[1]) if side == 0 else (self.factors[0], stack)


# ----- helpers ---------------------------------------------------------------------------------------------


def _clusters(edges):
    """Group coupled pairs that share a qubit: ``((A slots), (B slots), (((side, slot), (side, slot), strength), ...))``.

    Every cluster must reach both regions; a cluster inside one region would be that tenant's own coupling,
    which the gate calibration already contains.
    """
    groups: list[tuple[set, list]] = []
    for left, right, strength in edges:
        hits = [g for g in groups if left in g[0] or right in g[0]]
        merged = (set().union(*(g[0] for g in hits), {left, right}),
                  [e for g in hits for e in g[1]] + [(left, right, strength)])
        groups = [g for g in groups if g not in hits] + [merged]
    result = []
    for nodes, group_edges in groups:
        slots_a = tuple(sorted(slot for side, slot in nodes if side == 0))
        slots_b = tuple(sorted(slot for side, slot in nodes if side == 1))
        if not slots_a or not slots_b:
            raise ValueError("a coupled cluster must join the two regions")
        if len(nodes) > MAX_CLUSTER_QUBITS:
            raise ValueError(f"a coupled cluster may join at most {MAX_CLUSTER_QUBITS} qubits")
        result.append((slots_a, slots_b, tuple(group_edges)))
    return tuple(result)


def _pair_shift_energies(slots_a, slots_b, edges) -> np.ndarray:
    """Energy of each basis state of the cluster under ``sum zeta n_i n_j`` (A slots most significant)."""
    qubits = [(0, s) for s in slots_a] + [(1, s) for s in slots_b]
    count = len(qubits)
    position = {qubit: index for index, qubit in enumerate(qubits)}
    energies = np.zeros(1 << count)
    for left, right, zeta in edges:
        i, j = position[left], position[right]
        for state in range(1 << count):
            if (state >> (count - 1 - i)) & 1 and (state >> (count - 1 - j)) & 1:
                energies[state] += zeta
    return energies


def _device_energies(slots_a, slots_b, edges, spec_of, dims) -> np.ndarray:
    """The dressed frame's energy of each label the world holds for the cluster, from its device values (D91).

    The cluster is solved with its declared levels; the world keeps the labels up to its own levels per qubit
    (two: the qubit labels; three: |2> too, with its anharmonicity and the shifts it brings).
    """
    qubits = [(0, s) for s in slots_a] + [(1, s) for s in slots_b]
    position = {qubit: index for index, qubit in enumerate(qubits)}
    held = tuple(dims[side][slot] for side, slot in qubits)
    specs = tuple(spec_of(qubit) for qubit in qubits)
    if any(spec.levels < levels for spec, levels in zip(specs, held)):
        raise ValueError("a coupled transmon must be solved with at least the levels its qubit holds")
    cluster = TransmonCluster(
        specs,
        tuple(TransmonExchangeCoupling(position[left], position[right], strength) for left, right, strength in edges))
    return np.array([cluster.frame_energies[cluster.label_index(tuple(int(level) for level in label))]
                     for label in level_digits(held)])


def _component_factors(component, elapsed: float, mask, sides):
    """``exp(L t)`` on the cluster split as ``sum_m X_m (x) Y_m`` (A-side and B-side superoperators on blocks)."""
    slots_a, slots_b, energies = component
    qubits = [(0, s) for s in slots_a] + [(1, s) for s in slots_b]
    specs = [sides[side].idle_model.qubits[slot] for side, slot in qubits]
    cluster_dims = tuple(spec.levels for spec in specs)
    collapse = [_embed(operator, index, cluster_dims)
                for index, (spec, idle) in enumerate(zip(specs, mask)) if idle
                for operator in ladder_relaxation_operators(spec)]
    generator = np.asarray(gksl_liouvillian_superoperator(np.diag(energies).astype(complex), collapse))
    propagator = dense_matrix_exponential(generator * elapsed)
    return _split_superoperator(propagator, math.prod(cluster_dims[:len(slots_a)]),
                                math.prod(cluster_dims[len(slots_a):]))


def _split_superoperator(propagator: np.ndarray, da: int, db: int):
    """A cluster superoperator (A labels most significant) as ``sum_m X_m (x) Y_m`` on the two regions' blocks."""
    # column-major vectorization: index = column * D + row. Split rows/columns into (A part, B part).
    tensor = propagator.reshape(da, db, da, db, da, db, da, db)  # out col(A,B), out row(A,B), in col, in row
    # -> out row A, out col A, in row A, in col A | out row B, out col B, in row B, in col B
    tensor = tensor.transpose(2, 0, 6, 4, 3, 1, 7, 5).reshape(da ** 4, db ** 4)
    u, singular, vh = np.linalg.svd(tensor)
    keep = int(np.count_nonzero(singular > _ROUND_OFF * singular[0]))
    root = np.sqrt(singular[:keep])
    x_factors = (u[:, :keep] * root).T.reshape(keep, da * da, da * da)
    y_factors = (vh[:keep].T * root).T.reshape(keep, db * db, db * db)
    return x_factors, y_factors


def _embed(operator: np.ndarray, index: int, dims: tuple[int, ...]) -> np.ndarray:
    result = np.eye(1, dtype=complex)
    for position, level in enumerate(dims):
        result = np.kron(result, operator if position == index else np.eye(level, dtype=complex))
    return result


def _block_coefficients(stack: np.ndarray, slots: tuple[int, ...], dims: tuple[int, ...]):
    """Write each factor as blocks over ``slots`` (row and column levels) times an orthonormal rest basis.

    Returns ``coef`` with shape (terms, (levels of slots)**2, rank) and the basis (rest dimension, rank).
    """
    terms = stack.shape[0]
    blocks = _to_blocks(stack, slots, dims)                   # (terms, d, rest)
    flat = blocks.reshape(-1, blocks.shape[2]).T              # (rest, terms * d)
    basis, triangle = np.linalg.qr(flat)
    coef = triangle.T.reshape(terms, blocks.shape[1], -1)     # (terms, d, rank)
    return coef, basis


def _to_blocks(stack: np.ndarray, slots: tuple[int, ...], dims: tuple[int, ...]) -> np.ndarray:
    terms, n = stack.shape[0], len(dims)
    tensor = stack.reshape((terms,) + dims + dims)
    front = [1 + s for s in slots] + [1 + n + s for s in slots]
    rest = [axis for axis in range(1, 2 * n + 1) if axis not in front]
    ordered = tensor.transpose([0] + front + rest)
    return ordered.reshape(terms, math.prod(dims[s] for s in slots) ** 2, -1)


def _from_blocks(blocks: np.ndarray, slots: tuple[int, ...], dims: tuple[int, ...]) -> np.ndarray:
    terms, n = blocks.shape[0], len(dims)
    front = [1 + s for s in slots] + [1 + n + s for s in slots]
    rest = [axis for axis in range(1, 2 * n + 1) if axis not in front]
    order = [0] + front + rest
    shape = tuple(([terms] + list(dims) + list(dims))[axis] for axis in order)
    tensor = blocks.reshape(shape).transpose(np.argsort(order))
    return np.ascontiguousarray(tensor.reshape(terms, math.prod(dims), math.prod(dims)))


def _apply_stack(stack: np.ndarray, operator: np.ndarray, slots: tuple[int, ...],
                 dims: tuple[int, ...]) -> np.ndarray:
    """``U X U^dagger`` on every factor ``X`` of the stack (qubit 0 is the most significant).

    An operator given on the qubit levels of ``slots`` acts there and leaves leaked levels alone.
    """
    operand_dims = tuple(dims[s] for s in slots)
    operator = np.asarray(operator, dtype=complex)
    if len(operator) != math.prod(operand_dims):
        operator = embed_qubit_operator(operator, operand_dims)
    terms, n = stack.shape[0], len(dims)
    tensor = stack.reshape((terms,) + dims + dims)
    for axes, transform in (([1 + s for s in slots], operator), ([1 + n + s for s in slots], operator.conj())):
        order = axes + [axis for axis in range(2 * n + 1) if axis not in axes]
        ordered = tensor.transpose(order)
        updated = transform @ ordered.reshape(len(transform), -1)
        tensor = updated.reshape(ordered.shape).transpose(np.argsort(order))
    return np.ascontiguousarray(tensor.reshape(stack.shape))


def _scale_coherence(stack: np.ndarray, slot: int, factor: complex, dims: tuple[int, ...]) -> np.ndarray:
    """Multiply ``<1| X |0>`` of a two-level ``slot`` by ``factor`` and ``<0| X |1>`` by its conjugate."""
    bit = level_digits(dims)[:, slot]
    difference = bit[:, None] - bit[None, :]
    weights = np.where(difference == 1, factor, np.where(difference == -1, np.conj(factor), 1.0))
    return stack * weights[None]
