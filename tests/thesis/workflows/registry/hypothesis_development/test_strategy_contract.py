"""Phase capabilities and structural strategy receipt completion contracts."""

from dataclasses import replace

import pytest

from thesis.agent.runtime.schema import validate_arguments
from thesis.workflows.core.workflow import _phase_allowed_tools
from thesis.workflows.registry.hypothesis_development.spec import (
    build_hypothesis_development_workflow,
)


def _receipt() -> dict[str, object]:
    return {
        "artifactPath": "strategies/momentum/1.0.0",
        "bundleSha256": "a" * 64,
        "strategyId": "momentum",
        "version": "1.0.0",
    }


def test_only_design_has_authoring_grant_and_generic_default_is_empty() -> None:
    workflow = build_hypothesis_development_workflow()
    doctrine = workflow._doctrine
    assert doctrine is not None
    assert doctrine.phase_tools == {"strategy_design": ("author_strategy",)}
    for phase in workflow.phase_names:
        allowed = _phase_allowed_tools(doctrine, phase, "strategy")
        assert ("author_strategy" in allowed) == (phase == "strategy_design")
    assert "author_strategy" not in _phase_allowed_tools(
        replace(doctrine, phase_tools={}), "strategy_design", "strategy"
    )
    custom = replace(doctrine, phase_tools={"custom": ("local_tool", "local_tool")})
    assert _phase_allowed_tools(custom, "custom", "strategy").count("local_tool") == 1


def test_design_receipt_schema_and_validator_agree() -> None:
    phase = build_hypothesis_development_workflow().spec.phase_specs["strategy_design"]
    completion = {"strategy": _receipt(), "target": "strategy_backtesting"}
    assert phase.validate is not None and phase.validate(completion) is None
    assert not validate_arguments(phase.completion_schema, completion)


@pytest.mark.parametrize(
    "receipt",
    [
        {},
        {"anything": "nonempty"},
        {"strategyId": "momentum"},
        {**_receipt(), "bundleSha256": "not-a-digest"},
        {**_receipt(), "version": 1},
        {**_receipt(), "strategyId": " "},
        {**_receipt(), "artifactPath": "../outside/manifest.json"},
        {**_receipt(), "artifactPath": "strategies/../1.0.0"},
        {**_receipt(), "validation": {"accepted": False}},
    ],
)
def test_design_rejects_arbitrary_or_malformed_receipts(
    receipt: dict[str, object],
) -> None:
    phase = build_hypothesis_development_workflow().spec.phase_specs["strategy_design"]
    assert phase.validate is not None
    assert phase.validate({"strategy": receipt}) is not None
