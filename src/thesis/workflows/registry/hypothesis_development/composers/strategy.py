"""Composer for the canonical JSON strategy-design envelope."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from thesis.agent.router import InvocationResult
from thesis.strategies.bundles import StrategyBundleError

from ..contracts import (
    ExitCriterion,
    StrategyContent,
    StrategyDesignDraft,
)
from ..errors import (
    FalsificationComposeError,
    WorkflowError,
)
from ._shared import _json_document, _model_text


class JsonStrategyDesigner:
    """Parse the canonical JSON strategy-design envelope."""

    def compose(self, invocation: InvocationResult) -> StrategyDesignDraft:
        document = _json_document(invocation, "strategy design")
        content = _compose_strategy(document, "strategy")
        criteria = _compose_exit_criteria(document.get("exitCriteria"))
        return StrategyDesignDraft(content=content, exit_criteria=criteria)


def _compose_strategy(value: object, label: str) -> StrategyContent:
    if not isinstance(value, Mapping):
        raise FalsificationComposeError(f"{label} must be a JSON object")
    files = value.get("files")
    tests = value.get("tests")
    strategy_id = value.get("strategyId")
    version = value.get("version")
    if not isinstance(files, Mapping) or not files:
        raise FalsificationComposeError(f"{label} requires source files")
    if not isinstance(tests, Mapping) or not tests:
        raise FalsificationComposeError(f"{label} requires tests")
    if not isinstance(strategy_id, str) or not strategy_id.strip():
        raise FalsificationComposeError(f"{label} requires strategyId")
    if not isinstance(version, str) or not version.strip():
        raise FalsificationComposeError(f"{label} requires version")
    dependencies = value.get("dependencies", [])
    limitations = value.get("limitations", [])
    for sequence, name in (
        (dependencies, "dependencies"),
        (limitations, "limitations"),
    ):
        if isinstance(sequence, str) or not isinstance(sequence, Sequence):
            raise FalsificationComposeError(f"{label} {name} must be a sequence")
        for item in sequence:
            if not isinstance(item, str) or not item.strip():
                raise FalsificationComposeError(
                    f"{label} {name} must contain non-empty strings"
                )
    harness_configuration = value.get("harnessConfiguration", {})
    if not isinstance(harness_configuration, Mapping):
        raise FalsificationComposeError(
            f"{label} harnessConfiguration must be a JSON object"
        )
    try:
        return StrategyContent(
            strategy_id=strategy_id,
            version=version,
            source=dict(files),
            tests=dict(tests),
            entrypoint=_model_text(
                value.get("entrypoint", value.get("className", "Strategy")),
                f"{label} entrypoint",
            ),
            harness_configuration=dict(harness_configuration),
            dependencies=tuple(dict.fromkeys(item.strip() for item in dependencies)),
            limitations=tuple(dict.fromkeys(item.strip() for item in limitations)),
        )
    except (WorkflowError, StrategyBundleError) as error:
        raise FalsificationComposeError(str(error)) from error


def _compose_exit_criteria(value: object) -> tuple[ExitCriterion, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise FalsificationComposeError("exitCriteria must be a sequence")
    criteria: list[ExitCriterion] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise FalsificationComposeError("each exit criterion must be an object")
        try:
            criteria.append(
                ExitCriterion(
                    observable=item.get("observable"),
                    condition=item.get("condition"),
                    threshold=item.get("threshold"),
                    description=item.get("description"),
                    falsifier_id=item.get("falsifierId"),
                )
            )
        except WorkflowError as error:
            raise FalsificationComposeError(str(error)) from error
    return tuple(criteria)
