"""Internal coherent extensions for the coupled-transmon Hamiltonian.

Residual ZZ is represented by the cross-Kerr form ``zeta * n_i * n_j``.
Inside the computational subspace this equals a ZZ term plus absorbable local
Z and global terms, while retaining a natural qutrit extension.  Drive
crosstalk is a sparse complex transfer between scheduled control lines.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from core.gates import Matrix
from core.pulse_qutrit_contract import (
    ANNIHILATION_QUTRIT,
    CREATION_QUTRIT,
    NUMBER_QUTRIT,
)
from core.pulse_transmon_network import (
    CoupledTransmonNetworkHamiltonian,
    CoupledTransmonNetworkSegmentHamiltonian,
    ScheduledTransmonDrive,
)


@dataclass(frozen=True)
class ResidualZZCoupling:
    left: int
    right: int
    conditional_shift_rad_per_us: float

    def __post_init__(self) -> None:
        if self.left < 0 or self.right < 0 or self.left == self.right:
            raise ValueError("residual-ZZ endpoints must be distinct")
        if not math.isfinite(self.conditional_shift_rad_per_us):
            raise ValueError("residual-ZZ shift must be finite")


@dataclass(frozen=True)
class DriveCrosstalkTransfer:
    """Complex control-line leakage from ``source`` to ``target``.

    If the intended source envelope is ``x + i y`` and the transfer is
    ``real + i imag``, the unintended target envelope is their product.
    """

    source: int
    target: int
    transfer_real: float
    transfer_imag: float = 0.0

    def __post_init__(self) -> None:
        if self.source < 0 or self.target < 0 or self.source == self.target:
            raise ValueError("drive-crosstalk endpoints must be distinct")
        if not math.isfinite(self.transfer_real) or not math.isfinite(
            self.transfer_imag
        ):
            raise ValueError("drive-crosstalk transfer must be finite")
        if abs(complex(self.transfer_real, self.transfer_imag)) > 1.0 + 1e-12:
            raise ValueError("drive-crosstalk transfer magnitude must not exceed one")

    @property
    def coefficient(self) -> complex:
        return complex(self.transfer_real, self.transfer_imag)


class _EvaluableNetworkHamiltonian(Protocol):
    drives: tuple[ScheduledTransmonDrive, ...]

    def evaluate_array(self, local_time_us: float) -> np.ndarray: ...


@dataclass(frozen=True)
class ExtendedTransmonNetworkHamiltonian:
    """Compose internal coherent mechanisms around the frozen network model."""

    base: CoupledTransmonNetworkHamiltonian
    residual_zz: tuple[ResidualZZCoupling, ...] = ()
    drive_crosstalk: tuple[DriveCrosstalkTransfer, ...] = ()
    _static_extension: np.ndarray | None = field(init=False, repr=False)
    _drive_x_arrays: tuple[np.ndarray, ...] = field(init=False, repr=False)
    _drive_y_arrays: tuple[np.ndarray, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        _validate_edges(
            self.base.transmon_count,
            self.residual_zz,
            self.drive_crosstalk,
        )
        static, drive_x, drive_y = _extension_arrays(
            self.base.transmon_count,
            self.base.local_dimension,
            self.residual_zz,
            include_drive_operators=bool(self.drive_crosstalk),
        )
        object.__setattr__(self, "_static_extension", static)
        object.__setattr__(self, "_drive_x_arrays", drive_x)
        object.__setattr__(self, "_drive_y_arrays", drive_y)

    def for_segment(
        self,
        start_time_us: float,
        end_time_us: float,
    ) -> "ExtendedTransmonNetworkSegmentHamiltonian":
        return ExtendedTransmonNetworkSegmentHamiltonian(
            base=self.base.for_segment(start_time_us, end_time_us),
            static_extension=self._static_extension,
            drive_x_arrays=self._drive_x_arrays,
            drive_y_arrays=self._drive_y_arrays,
            drive_crosstalk=self.drive_crosstalk,
        )

    def evaluate_array(self, local_time_us: float) -> np.ndarray:
        return _evaluate(
            self.base,
            local_time_us,
            self._static_extension,
            self._drive_x_arrays,
            self._drive_y_arrays,
            self.drive_crosstalk,
        )

    def evaluate(self, local_time_us: float) -> Matrix:
        return _to_matrix(self.evaluate_array(local_time_us))


@dataclass(frozen=True)
class ExtendedTransmonNetworkSegmentHamiltonian:
    """Segment view preserving the base model's pulse-edge convention."""

    base: CoupledTransmonNetworkSegmentHamiltonian
    static_extension: np.ndarray | None
    drive_x_arrays: tuple[np.ndarray, ...]
    drive_y_arrays: tuple[np.ndarray, ...]
    drive_crosstalk: tuple[DriveCrosstalkTransfer, ...]

    def for_segment(
        self,
        start_time_us: float,
        end_time_us: float,
    ) -> "ExtendedTransmonNetworkSegmentHamiltonian":
        del start_time_us, end_time_us
        return self

    def evaluate_array(self, local_time_us: float) -> np.ndarray:
        return _evaluate(
            self.base,
            local_time_us,
            self.static_extension,
            self.drive_x_arrays,
            self.drive_y_arrays,
            self.drive_crosstalk,
        )

    def evaluate(self, local_time_us: float) -> Matrix:
        return _to_matrix(self.evaluate_array(local_time_us))


def _validate_edges(
    transmon_count: int,
    residual_zz: tuple[ResidualZZCoupling, ...],
    drive_crosstalk: tuple[DriveCrosstalkTransfer, ...],
) -> None:
    zz_pairs: set[tuple[int, int]] = set()
    for edge in residual_zz:
        if edge.left >= transmon_count or edge.right >= transmon_count:
            raise ValueError("residual-ZZ endpoint is outside the register")
        pair = tuple(sorted((edge.left, edge.right)))
        if pair in zz_pairs:
            raise ValueError("residual-ZZ pairs must be unique")
        zz_pairs.add(pair)

    transfers: set[tuple[int, int]] = set()
    for edge in drive_crosstalk:
        if edge.source >= transmon_count or edge.target >= transmon_count:
            raise ValueError("drive-crosstalk endpoint is outside the register")
        pair = (edge.source, edge.target)
        if pair in transfers:
            raise ValueError("drive-crosstalk transfers must be unique")
        transfers.add(pair)


def _extension_arrays(
    transmon_count: int,
    local_dimension: int,
    residual_zz: tuple[ResidualZZCoupling, ...],
    *,
    include_drive_operators: bool,
) -> tuple[np.ndarray | None, tuple[np.ndarray, ...], tuple[np.ndarray, ...]]:
    number = np.asarray(NUMBER_QUTRIT, dtype=np.complex128)[
        :local_dimension, :local_dimension
    ]
    drive_x: tuple[np.ndarray, ...] = ()
    drive_y: tuple[np.ndarray, ...] = ()
    if include_drive_operators:
        annihilation = np.asarray(ANNIHILATION_QUTRIT, dtype=np.complex128)[
            :local_dimension, :local_dimension
        ]
        creation = np.asarray(CREATION_QUTRIT, dtype=np.complex128)[
            :local_dimension, :local_dimension
        ]
        local_x = 0.5 * (annihilation + creation)
        local_y = (annihilation - creation) / (2.0j)
        drive_x = tuple(
            _embed(local_x, index, transmon_count, local_dimension)
            for index in range(transmon_count)
        )
        drive_y = tuple(
            _embed(local_y, index, transmon_count, local_dimension)
            for index in range(transmon_count)
        )

    if not residual_zz:
        return None, drive_x, drive_y
    embedded_number = tuple(
        _embed(number, index, transmon_count, local_dimension)
        for index in range(transmon_count)
    )
    dimension = local_dimension ** transmon_count
    static = np.zeros((dimension, dimension), dtype=np.complex128)
    for edge in residual_zz:
        static += edge.conditional_shift_rad_per_us * (
            embedded_number[edge.left] @ embedded_number[edge.right]
        )
    static.setflags(write=False)
    return static, drive_x, drive_y


def _evaluate(
    base: _EvaluableNetworkHamiltonian,
    local_time_us: float,
    static_extension: np.ndarray | None,
    drive_x_arrays: tuple[np.ndarray, ...],
    drive_y_arrays: tuple[np.ndarray, ...],
    drive_crosstalk: tuple[DriveCrosstalkTransfer, ...],
) -> np.ndarray:
    baseline = base.evaluate_array(local_time_us)
    if static_extension is None and not drive_crosstalk:
        return baseline

    hamiltonian = baseline.copy()
    if static_extension is not None:
        hamiltonian += static_extension

    if drive_crosstalk:
        source_quadratures = [0.0j for _ in drive_x_arrays]
        for drive in base.drives:
            x_value, y_value = drive.quadratures(local_time_us)
            source_quadratures[drive.target] += complex(x_value, y_value)
        leaked = [0.0j for _ in drive_x_arrays]
        for transfer in drive_crosstalk:
            leaked[transfer.target] += (
                transfer.coefficient * source_quadratures[transfer.source]
            )
        for target, amplitude in enumerate(leaked):
            if amplitude.real != 0.0:
                hamiltonian += amplitude.real * drive_x_arrays[target]
            if amplitude.imag != 0.0:
                hamiltonian += amplitude.imag * drive_y_arrays[target]
    return hamiltonian


def _embed(
    operator: np.ndarray,
    target: int,
    transmon_count: int,
    local_dimension: int,
) -> np.ndarray:
    identity = np.eye(local_dimension, dtype=np.complex128)
    result = operator if target == 0 else identity
    for index in range(1, transmon_count):
        result = np.kron(result, operator if index == target else identity)
    return result


def _to_matrix(array: np.ndarray) -> Matrix:
    return tuple(tuple(complex(value) for value in row) for row in array)
