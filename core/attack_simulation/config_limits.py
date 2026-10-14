"""Provisional host-safety bounds for scenario authoring, not exercise budgets."""

from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any


MAX_CONFIG_DEPTH = 32
MAX_CONFIG_NODES = 16_384
MAX_CONFIG_BYTES = 1_048_576
MAX_METADATA_ITEMS = 256
MAX_TEXT_BYTES = 8_192


def check_json_bounds(value: Any, name: str) -> None:
    """Bound work before recursive freezing and manifest serialization.

    The byte count is a cheap preflight estimate; the serialized manifest is
    checked separately against MAX_CONFIG_BYTES before it is stored.
    """

    stack = [(value, 0)]
    nodes = 0
    byte_count = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > MAX_CONFIG_NODES:
            raise ValueError(f"{name} exceeds the node limit")
        if depth > MAX_CONFIG_DEPTH:
            raise ValueError(f"{name} exceeds the nesting limit")
        if isinstance(item, Mapping):
            if len(item) > MAX_CONFIG_NODES - nodes - len(stack):
                raise ValueError(f"{name} exceeds the node limit")
            for key, child in item.items():
                key_text = str(key)
                if len(key_text) > MAX_CONFIG_BYTES - byte_count:
                    raise ValueError(f"{name} exceeds the byte limit")
                byte_count += len(key_text.encode("utf-8"))
                if byte_count > MAX_CONFIG_BYTES:
                    raise ValueError(f"{name} exceeds the byte limit")
                stack.append((child, depth + 1))
        elif isinstance(item, (list, tuple)):
            if len(item) > MAX_CONFIG_NODES - nodes - len(stack):
                raise ValueError(f"{name} exceeds the node limit")
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            if len(item) > MAX_CONFIG_BYTES - byte_count:
                raise ValueError(f"{name} exceeds the byte limit")
            byte_count += len(item.encode("utf-8"))
        elif item is None or isinstance(item, bool):
            pass
        elif isinstance(item, int):
            if item.bit_length() > 10_000:
                raise ValueError(f"{name} contains an oversized integer")
            byte_count += len(str(item))
        elif isinstance(item, float) and math.isfinite(item):
            byte_count += len(repr(item))
        else:
            raise TypeError(f"{name} must contain JSON-compatible finite values")
        if byte_count > MAX_CONFIG_BYTES:
            raise ValueError(f"{name} exceeds the byte limit")
