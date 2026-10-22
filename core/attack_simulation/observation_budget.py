"""Mutable per-session accounting for immutable observation-budget policy."""

from __future__ import annotations

from dataclasses import dataclass, replace
from collections.abc import Mapping
from types import MappingProxyType
from threading import Lock

from core.attack_simulation.scenario_policy import ObservationBudget


class ObservationQueryKind:
    """Current scenario query labels; the ledger accepts registered labels as strings."""

    TRAINING = "training"
    VICTIM = "victim"


class ObservationBudgetExceeded(ValueError):
    """Raised before execution when a query would exceed session policy."""


@dataclass(frozen=True)
class ObservationBudgetUsage:
    training_queries: int = 0
    victim_observations: int = 0
    retries: int = 0
    consumed_shots: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "training_queries": self.training_queries,
            "victim_observations": self.victim_observations,
            "retries": self.retries,
            "consumed_shots": self.consumed_shots,
        }


class ObservationBudgetLedger:
    """Atomically reserves query attempts against one scenario-session budget.

    An initial attempt consumes one query slot of its kind. A retry consumes a
    retry slot instead of another logical-query slot. Both consume shot
    budget, and reservations remain consumed after execution failures so a
    caller cannot bypass limits by repeatedly inducing partial failures.
    """

    def __init__(self, budget: ObservationBudget,
                 query_kind_counters: Mapping[str, str] | None = None) -> None:
        if not isinstance(budget, ObservationBudget):
            raise TypeError("paired-query accounting requires an explicit observation budget")
        self.budget = budget
        bindings = query_kind_counters or {
            ObservationQueryKind.TRAINING: "training_queries",
            ObservationQueryKind.VICTIM: "victim_observations",
        }
        allowed_counters = {"training_queries", "victim_observations"}
        if (not bindings or any(not isinstance(kind, str) or not kind.strip()
                                or counter not in allowed_counters
                                for kind, counter in bindings.items())):
            raise ValueError("query kinds must map to a declared observation counter")
        self.query_kind_counters = MappingProxyType(dict(bindings))
        self._usage = ObservationBudgetUsage()
        self._lock = Lock()

    def read(self) -> ObservationBudgetUsage:
        with self._lock:
            return self._usage

    def reserve(
        self,
        *,
        kind: str,
        shots: int,
        retry: bool = False,
    ) -> ObservationBudgetUsage:
        try:
            counter = self.query_kind_counters[kind]
        except (KeyError, TypeError):
            raise ValueError("observation query kind is not registered") from None
        requested_shots = int(shots)
        with self._lock:
            current = self._usage
            if retry and not self._has_original_query(current, counter):
                raise ObservationBudgetExceeded(
                    "a retry requires an existing query of the same kind"
                )
            next_usage = replace(
                current,
                **{
                    counter: getattr(current, counter) + int(not retry),
                    "retries": current.retries + int(retry),
                    "consumed_shots": current.consumed_shots + requested_shots,
                },
            )
            try:
                self.budget.require_within(
                    shots=requested_shots,
                    training_queries=next_usage.training_queries,
                    victim_observations=next_usage.victim_observations,
                    retries=next_usage.retries,
                )
            except ValueError as error:
                raise ObservationBudgetExceeded(str(error)) from error
            self._usage = next_usage
            return next_usage

    @staticmethod
    def _has_original_query(
        usage: ObservationBudgetUsage,
        counter: str,
    ) -> bool:
        return getattr(usage, counter) > 0
