"""Evaluator registry: one place that maps a manifest's evaluator_id to its builder.

The product factory looks an evaluator up here and never names a challenge.
A new challenge adds one entry to ``_builders``; evaluators that grade the live
world read it only through ``context.victim_counts``, supplied by the factory.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping


@dataclass(frozen=True)
class EvaluatorContext:
    """What the factory can offer an evaluator, whichever challenge it grades."""

    manifest: object
    limits: object
    # Reads the victim's completed run counts from the live world, for the
    # supervisor only. Evaluators that regrade a fresh environment ignore it.
    victim_counts: Callable[[], tuple[Mapping[str, int], ...]]


def tenant_run_counts(runtime, tenant_id: str) -> tuple[Mapping[str, int], ...]:
    """A tenant's finished runs, in submission order (supervisor-side read of the live scheduler)."""
    jobs = runtime.scheduler.completed_job_results_for_tenant(tenant_id)
    return tuple(dict(job.result["counts"]) for job in jobs)


def _builders() -> Mapping[str, Callable[[EvaluatorContext], object]]:
    # Imported here because the challenge modules import the factory's types.
    from api import attack_challenge, attack_crosstalk_challenge

    return {
        attack_challenge.EVALUATOR_ID:
            lambda c: attack_challenge.HiddenCaseEvaluator(c.manifest, c.limits),
        attack_challenge.MESSAGE_EVALUATOR_ID:
            lambda c: attack_challenge.MessageExactEvaluator(c.manifest),
        attack_crosstalk_challenge.EVALUATOR_ID:
            lambda c: attack_crosstalk_challenge.CrosstalkGroverEvaluator(
                c.manifest, c.limits, c.victim_counts),
    }


def build_evaluator(context: EvaluatorContext):
    """The evaluator the manifest names; an unknown ID is refused, never defaulted."""
    builder = _builders().get(context.manifest.evaluator_id)
    if builder is None:
        raise ValueError("unsupported challenge evaluator")
    return builder(context)
