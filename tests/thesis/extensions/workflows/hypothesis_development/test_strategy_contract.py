"""Phase capabilities and structural strategy receipt completion contracts."""

import pytest

from thesis.extensions.workflows.hypothesis_development.spec import (
    phase_declarations,
)
from thesis_runtime.tools.schema import validate_arguments


def _receipt() -> dict[str, object]:
    return {
        "workspaceId": "workspace-demo",
        "kind": "strategy",
        "programId": "momentum",
        "gitCommit": "a" * 40,
        "version": "b" * 64,
    }


def test_design_receipt_schema_and_validator_agree() -> None:
    phase = next(
        item.spec
        for item in phase_declarations()
        if item.spec.phase == "strategy_design"
    )
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
        {**_receipt(), "gitCommit": "a" * 41},
        {**_receipt(), "kind": "study"},
        {**_receipt(), "strategyId": " "},
        {**_receipt(), "artifactPath": "../outside/manifest.json"},
        {**_receipt(), "artifactPath": "strategies/../1.0.0"},
        {**_receipt(), "validation": {"accepted": False}},
    ],
)
def test_design_rejects_arbitrary_or_malformed_receipts(
    receipt: dict[str, object],
) -> None:
    phase = next(
        item.spec
        for item in phase_declarations()
        if item.spec.phase == "strategy_design"
    )
    assert phase.validate is not None
    assert phase.validate({"strategy": receipt}) is not None
