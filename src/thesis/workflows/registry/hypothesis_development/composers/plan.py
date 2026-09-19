"""Composer for the canonical JSON falsification-plan envelope."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from thesis.agent.router import InvocationResult

from ..contracts import (
    FalsificationPlanDraft,
    PlaceboControl,
    PlannedTest,
)
from ..errors import (
    FalsificationComposeError,
    WorkflowError,
)
from ._shared import _json_document


class JsonFalsificationPlanner:
    """Parse the canonical JSON falsification-plan envelope."""

    def compose(self, invocation: InvocationResult) -> FalsificationPlanDraft:
        document = _json_document(invocation, "falsification plan")
        tests = _compose_tests(document.get("tests"))
        controls = _compose_controls(document.get("controls"))
        return FalsificationPlanDraft(tests=tests, controls=controls)


def _compose_tests(value: object) -> tuple[PlannedTest, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise FalsificationComposeError("plan tests must be a sequence")
    tests: list[PlannedTest] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise FalsificationComposeError("each plan test must be a JSON object")
        try:
            tests.append(
                PlannedTest(
                    test_id=item.get("testId"),
                    falsifier_id=item.get("falsifierId"),
                    description=item.get("description"),
                )
            )
        except WorkflowError as error:
            raise FalsificationComposeError(str(error)) from error
    return tuple(tests)


def _compose_controls(value: object) -> tuple[PlaceboControl, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise FalsificationComposeError("plan controls must be a sequence")
    controls: list[PlaceboControl] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise FalsificationComposeError("each control must be a JSON object")
        try:
            controls.append(
                PlaceboControl(
                    control_id=item.get("controlId"),
                    description=item.get("description"),
                    expectation=item.get("expectation"),
                )
            )
        except WorkflowError as error:
            raise FalsificationComposeError(str(error)) from error
    return tuple(controls)
