"""Composer for the canonical JSON RCA-diagnosis envelope."""

from __future__ import annotations

from collections.abc import Sequence

from thesis.agent.router import InvocationResult

from ..contracts import (
    RcaDiagnosis,
)
from ..errors import (
    FalsificationComposeError,
    WorkflowError,
)
from ._shared import _compose_text, _json_document


class JsonRcaComposer:
    """Parse the canonical JSON RCA-diagnosis envelope."""

    def compose(self, invocation: InvocationResult) -> RcaDiagnosis:
        document = _json_document(invocation, "rca")
        confidence = document.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, int | float):
            raise FalsificationComposeError("rca confidence must be numeric")
        follow_ups = document.get("followUps", [])
        if isinstance(follow_ups, str) or not isinstance(follow_ups, Sequence):
            raise FalsificationComposeError("rca followUps must be a sequence")
        for item in follow_ups:
            if not isinstance(item, str) or not item.strip():
                raise FalsificationComposeError(
                    "rca followUps must contain non-empty strings"
                )
        raw_class = document.get("failureClass")
        if isinstance(raw_class, str):
            lowered = raw_class.lower()
            if "harness" in lowered:
                failure_class = "harness"
            elif "data" in lowered:
                failure_class = "data"
            elif (
                "implementation" in lowered
                or "syntax" in lowered
                or "import" in lowered
            ):
                failure_class = "implementation"
            elif "market" in lowered or "regime" in lowered:
                failure_class = "market_regime"
            elif "strategy" in lowered:
                failure_class = "strategy"
            else:
                failure_class = _compose_text(document, "failureClass", "failureClass")[
                    :64
                ]
        else:
            failure_class = _compose_text(document, "failureClass", "failureClass")[:64]
        try:
            return RcaDiagnosis(
                falsifier_id=_compose_text(document, "falsifierId", "falsifierId"),
                failure_class=failure_class,
                rationale=_compose_text(document, "rationale", "rationale"),
                confidence=float(confidence),
                follow_ups=tuple(item.strip() for item in follow_ups),
            )
        except WorkflowError as error:
            raise FalsificationComposeError(str(error)) from error
