"""Policy-free helpers shared by every challenge preset builder.

Deliberately small: nothing here decides what a challenge locks, hides or grades.
Those are each challenge's own choices.
"""

from __future__ import annotations

from typing import Any, Mapping


def thaw(value: Any) -> Any:
    """A mutable deep copy of a read-only configuration value."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(item) for item in value]
    return value


def unique_by_assumption_id(records):
    """Keep the first record of each assumption_id, in order."""
    seen, kept = set(), []
    for record in records:
        if record.assumption_id not in seen:
            seen.add(record.assumption_id)
            kept.append(record)
    return tuple(kept)
