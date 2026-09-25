"""Foundation-level validation shared by domain entities.

This module intentionally does NOT implement the full Action Registry or
Policy Engine (Stage 2). It only stops obviously-invalid values — an
out-of-range confidence score, an unregistered action type — from entering
the domain layer in the first place.
"""

from __future__ import annotations

from crm_ai_agent.domain.enums import KnownActionType, KnownTicketType


class DomainValidationError(ValueError):
    pass


def validate_confidence(value: float, *, field_name: str = "confidence") -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise DomainValidationError(f"{field_name} must be a number, got {type(value).__name__}")
    if not (0.0 <= float(value) <= 1.0):
        raise DomainValidationError(f"{field_name} must be within [0.0, 1.0], got {value!r}")
    return float(value)


def validate_action_type(value: str) -> str:
    try:
        KnownActionType(value)
    except ValueError as exc:
        allowed = ", ".join(t.value for t in KnownActionType)
        raise DomainValidationError(
            f"Unknown action_type {value!r}; must be one of: {allowed}"
        ) from exc
    return value


def validate_ticket_type(value: str) -> str:
    try:
        KnownTicketType(value)
    except ValueError as exc:
        allowed = ", ".join(t.value for t in KnownTicketType)
        raise DomainValidationError(
            f"Unknown ticket_type {value!r}; must be one of: {allowed}"
        ) from exc
    return value


def validate_non_empty(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{field_name} must be a non-empty string")
    return value


def validate_positive_int(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainValidationError(f"{field_name} must be an int, got {type(value).__name__}")
    if value <= 0:
        raise DomainValidationError(f"{field_name} must be > 0, got {value!r}")
    return value


def validate_unique_non_empty_strings(values: tuple[str, ...], *, field_name: str) -> tuple[str, ...]:
    values = tuple(values)
    if not values:
        raise DomainValidationError(f"{field_name} must contain at least one role")
    for v in values:
        validate_non_empty(v, field_name=f"{field_name} entry")
    if len(set(values)) != len(values):
        raise DomainValidationError(f"{field_name} must not contain duplicate roles, got {values!r}")
    return values
