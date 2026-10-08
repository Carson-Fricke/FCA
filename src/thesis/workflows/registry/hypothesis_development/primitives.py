"""Shared constants, validation, and small helpers.

Extracted from the original single-module hypothesis-development
implementation; behavior is unchanged.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

from thesis.research.grammar import IDENTIFIER as _IDENTIFIER

from .errors import WorkflowError

#: Canonical falsification verdict outcomes shared by the thesis pipelines.
VERDICT_SUPPORTED = "supported"


VERDICT_REFUTED = "refuted"


VERDICT_INCONCLUSIVE = "inconclusive"


_WORKFLOW_ID = "hypothesis-development"


_WORKFLOW_VERSION = "2.3.0"


_WORKFLOW_TITLE = "Hypothesis development"


_WORKFLOW_ACTOR = "thesis-workflow"


_VERDICTS = frozenset({VERDICT_SUPPORTED, VERDICT_REFUTED, VERDICT_INCONCLUSIVE})


_WORKSPACE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")


_DIGEST = re.compile(r"^[0-9a-f]{64}$")


#: Minimum fields a good-faith model reply must carry; everything else is
#: defaulted so tolerant extraction never turns a partial answer into a failure.
_DEFAULT_HORIZON = "unspecified"


_DEFAULT_RISK_FACTOR = "unspecified"


_DEFAULT_UNIVERSE: tuple[str, ...] = ("UNSPECIFIED",)


#: Concise JSON shapes echoed back to the model during a compose repair.
_HYPOTHESIS_SCHEMA = (
    '{"claim": string, "disagreement": string, "riskFactor": string, '
    '"horizon": string, "universe": [string], "falsifiers": [{"falsifierId": '
    'string, "observable": string, "condition": "lt"|"lte"|"gt"|"gte", '
    '"threshold": number, "test": string}], "sizing": string, "body": string, '
    '"model": {"workspaceId": string, "kind": "study", "programId": string, '
    '"gitCommit": string, "version": string, "description": string, '
    '"outputs": [string], "tests": {path: string}}}'
)


_FALSIFICATION_PLAN_SCHEMA = (
    '{"tests": [{"testId": string, "falsifierId": string, "description": '
    'string}], "controls": [{"controlId": string, "description": string, '
    '"expectation": string}]}'
)


_RCA_SCHEMA = (
    '{"falsifierId": string, '
    '"failureClass": "harness"|"data"|"implementation"|"strategy"|"market_regime", '
    '"rationale": string, "confidence": number, "followUps": [string]}'
)


_STRATEGY_DESIGN_SCHEMA = (
    '{"workspaceId": string, "kind": "strategy", "programId": string, '
    '"gitCommit": string, "version": string}'
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise WorkflowError(f"{label} must be a safe identifier")
    return value


def _workspace_id(value: str) -> str:
    if not isinstance(value, str) or _WORKSPACE_ID.fullmatch(value) is None:
        raise WorkflowError("workspace_id must be a safe workspace identifier")
    return value


def _text(value: object, label: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError(f"{label} must be non-empty")
    sanitized = value.replace("\r\n", "\n").replace("\r", "\n")
    if len(sanitized) > max_length:
        raise WorkflowError(
            f"{label} exceeds maximum length of {max_length} characters"
        )
    if any(ord(character) < 32 and character not in "\n\t" for character in sanitized):
        raise WorkflowError(f"{label} contains unsupported control characters")
    return sanitized


def _format_observed(val: float | None) -> str:
    if val is None or not math.isfinite(val):
        return "—"
    if abs(val) < 0.0001 and val != 0:
        return f"{val:.2e}"
    if abs(val) < 1:
        return f"{val:.4f}"
    return f"{val:.2f}"


def _metric(metrics: object | None, observable: str) -> float | None:
    if not isinstance(metrics, Mapping):
        return None
    value = metrics.get(observable)
    if value is None:
        custom = metrics.get("custom")
        if isinstance(custom, Mapping):
            value = custom.get(observable)
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _digest(payload: Mapping[str, object]) -> str:
    document = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(document).hexdigest()
