"""Shared validation and serialization helpers for harness domain records."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Final

DecimalInput = Decimal | int | str

_ZERO = Decimal("0")
SCHEMA_VERSION: Final = "1"


def exact_decimal(value: object, field_name: str = "value") -> Decimal:
    """Convert an exact decimal input while rejecting floats and non-finite values."""
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{field_name} must be an exact Decimal, not float")
    if not isinstance(value, (Decimal, int, str)):
        raise TypeError(f"{field_name} must be an exact Decimal")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be an exact Decimal") from error
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return result


def decimal(value: object, field_name: str, *, positive: bool = False) -> Decimal:
    result = exact_decimal(value, field_name)
    if positive and result <= 0:
        raise ValueError(f"{field_name} must be positive")
    return result


def nonnegative(value: object, field_name: str) -> Decimal:
    result = decimal(value, field_name)
    if result < 0:
        raise ValueError(f"{field_name} must not be negative")
    return result


def text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def timestamp(value: datetime, field_name: str = "timestamp") -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def sequence(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("sequence must be a non-negative integer")
    return value


def optional_decimal(value: object | None, field_name: str) -> Decimal | None:
    return None if value is None else exact_decimal(value, field_name)


def optional_positive(value: object | None, field_name: str) -> Decimal | None:
    return None if value is None else decimal(value, field_name, positive=True)


def decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


__all__ = [
    "DecimalInput",
    "SCHEMA_VERSION",
    "decimal",
    "decimal_text",
    "exact_decimal",
    "nonnegative",
    "optional_decimal",
    "optional_positive",
    "sequence",
    "text",
    "timestamp",
]
