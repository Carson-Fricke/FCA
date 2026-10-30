"""Program identities, failure contracts and configuration validation.

These value rules have no workspace, SDK transport, Git or submission dependency.
Lifecycle orchestration and SDK references both consume this contract.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Literal, Protocol

from thesis.research.models import (
    MARKET_SUBSCRIPTIONS_SCHEMA,
    ResearchModelError,
    validate_market_configuration,
    validate_market_subscriptions,
)
from thesis_runtime.contracts.failures import ToolFailureError

ProgramKind = Literal["study", "strategy", "indicator"]


class ProgramArtifactError(ToolFailureError):
    """Invalid identity, stale edit/validation, or an unavailable program capability."""

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(
            {
                "kind": "program",
                "message": message,
                "retry_safe": True,
                "retryGuidance": "Correct the rejected program input or capability using this diagnostic before retrying.",
                **details,
            }
        )


class ProgramPublicationError(ProgramArtifactError):
    """A local commit exists but provider publication needs explicit reconciliation."""

    def __init__(self, receipt: Mapping[str, object]) -> None:
        super().__init__(
            "Program is committed locally; publication or checkout materialization failed. Inspect the pinned receipt. Use the matching publish tool only to retry repository publication; it never repairs checkout/index state. Ask the operator to reconcile checkout failures; do not re-author or replay execution."
        )
        self.receipt = dict(receipt)


class ProgramHarnessResult(Protocol):
    """Common result surface of the MCP client and HarnessToolProvider."""

    @property
    def is_error(self) -> bool: ...

    @property
    def structured(self) -> Mapping[str, object]: ...

    @property
    def text(self) -> str: ...


def canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    except (TypeError, ValueError) as error:
        raise ProgramArtifactError("program values must be JSON-shaped") from error


_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
PROGRAM_CONFIGURATION_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["start", "end", "subscriptions"],
    "properties": {
        "start": {"type": "string", "pattern": _DATE_PATTERN},
        "end": {"type": "string", "pattern": _DATE_PATTERN},
        "subscriptions": MARKET_SUBSCRIPTIONS_SCHEMA,
    },
}


def configuration_window(
    value: Mapping[str, object],
) -> tuple[datetime, datetime]:
    dates: list[datetime] = []
    for key in ("start", "end"):
        bound = value.get(key)
        if not isinstance(bound, str) or re.fullmatch(_DATE_PATTERN, bound) is None:
            raise ProgramArtifactError(
                f"configuration.{key} requires an RFC 3339 date with UTC offset"
            )
        try:
            dates.append(datetime.fromisoformat(bound.replace("Z", "+00:00")))
        except ValueError as error:
            raise ProgramArtifactError(
                f"configuration.{key} is not a valid date"
            ) from error
    if dates[0] >= dates[1]:
        raise ProgramArtifactError("configuration.start must be before end")
    return dates[0], dates[1]


def check_configuration(value: object) -> None:
    if not isinstance(value, Mapping):
        raise ProgramArtifactError("configuration must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ProgramArtifactError("configuration must have string keys")
    canonical_bytes(value)
    configuration_window(value)
    try:
        validate_market_subscriptions(value.get("subscriptions"))
        validate_market_configuration(value)
    except ResearchModelError as error:
        raise ProgramArtifactError(str(error)) from error
