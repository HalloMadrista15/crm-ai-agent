"""Deep-immutability helpers for domain value objects.

``frozen=True`` on a dataclass only stops reassignment of its own fields; a
``dict`` or ``list`` stored in one of those fields is still mutable in place.
Entities that feed the plan/approval hash (Stage 2) must be immutable all the
way down, otherwise a caller could mutate a plan's arguments after it was
hashed and approved. ``deep_freeze`` converts nested dict/list structures into
``MappingProxyType``/``tuple`` recursively so no part of the object graph can
be mutated after construction.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any


def deep_freeze(value: Any) -> Any:
    if isinstance(value, MappingProxyType):
        return value
    if isinstance(value, dict):
        return MappingProxyType({k: deep_freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(deep_freeze(v) for v in value)
    if isinstance(value, set):
        return frozenset(deep_freeze(v) for v in value)
    return value


def canonicalize(value: Any) -> Any:
    """Produce a JSON-serializable, deterministically ordered projection.

    Used as the input to canonical hashing (Stage 2): dict keys are sorted so
    the same logical content always serializes identically regardless of
    insertion order or whether it currently lives in a dict or a
    MappingProxyType.
    """

    if isinstance(value, MappingProxyType) or isinstance(value, dict):
        return {k: canonicalize(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [canonicalize(v) for v in value]
    if isinstance(value, frozenset):
        return sorted(canonicalize(v) for v in value)
    return value
