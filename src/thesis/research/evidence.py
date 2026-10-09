"""Pure parameter identities and numeric views of exact harness evidence."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation


def parameter_identity(parameters: Mapping[str, object]) -> str:
    """Canonical, type-strict JSON shared by submissions and assessments."""
    errors = parameter_errors(parameters, "parameters")
    if errors:
        raise ValueError("; ".join(errors))
    return json.dumps(
        dict(parameters),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def parameter_digest(parameters: Mapping[str, object]) -> str:
    """Hash the canonical vector, retaining integer/float distinctions."""
    return hashlib.sha256(parameter_identity(parameters).encode()).hexdigest()


def document_digest(document: Mapping[str, object]) -> str:
    """Hash a nested accepted-input document, separate from scalar parameters."""
    canonical = json.dumps(
        dict(document),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def numeric_metric(value: object) -> float | None:
    """Finite numeric view; exact decimal strings remain in the source evidence."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        decimal = Decimal(str(value))
        number = float(decimal)
    except (InvalidOperation, ValueError, OverflowError):
        return None
    return number if decimal.is_finite() and math.isfinite(number) else None


def parameter_errors(parameters: Mapping[str, object], label: str) -> list[str]:
    """Validate the scalar parameter vectors used by all research experiments."""
    errors: list[str] = []
    for name, value in parameters.items():
        if not isinstance(name, str) or not name:
            errors.append(f"{label}: parameter names must be non-empty strings")
        elif isinstance(value, bool | str):
            continue
        elif isinstance(value, int | float):
            if isinstance(value, float) and not math.isfinite(value):
                errors.append(f"{label}.{name}: parameter values must be finite")
        else:
            errors.append(
                f"{label}.{name}: parameter values must be scalar (string, number, or boolean)"
            )
    return errors


def metric_errors(metrics: Mapping[str, object], label: str) -> list[str]:
    """Reject invalid numeric evidence while retaining undefined-value reasons.

    Harness decimals remain exact strings. Text that cannot parse as a decimal
    is an explanatory field, not a synthesized financial value.
    """
    errors: list[str] = []
    for name, value in metrics.items():
        if not isinstance(name, str):
            errors.append(f"{label}: metric names must be strings")
            continue
        if isinstance(value, str):
            try:
                Decimal(value)
            except InvalidOperation:
                continue
        elif not isinstance(value, bool | int | float):
            continue
        if numeric_metric(value) is None:
            errors.append(f"{label}.{name}: metric values must be finite numbers")
    return errors
