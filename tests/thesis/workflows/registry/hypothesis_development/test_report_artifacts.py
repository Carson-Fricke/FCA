"""Backend-produced report fixtures consumed by the UI transport tests.

Regenerate with ``uv run --extra dev python -m
tests.thesis.workflows.registry.hypothesis_development.test_report_artifacts``.
"""

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from thesis.workflows.registry.hypothesis_development.artifacts import (
    forward_report_artifact,
    variation_report_artifact,
)
from thesis.workflows.registry.hypothesis_development.contracts import (
    ForwardEvaluation,
    VariationEvaluation,
)
from thesis.workflows.registry.hypothesis_development.phases import (
    HypothesisDevelopmentPhase,
)
from thesis.workflows.registry.hypothesis_development.state import (
    HypothesisDevelopmentState,
)

from ._support import WORKSPACE, _forward_envelope, _variation_envelope

FIXTURE = Path("src/ui/src/tests/fixtures/research-reports.json")


def report_fixture() -> dict[str, object]:
    """Exercise accepted contracts, including harness decimal strings."""
    payload = cast(dict[str, Any], _variation_envelope())
    payload.update(method="one_at_a_time", interactions=[])
    payload["center"]["metrics"] = {"total_return": "0.12", "profit_factor": "1.6"}
    payload["points"][0]["metrics"] = {
        "total_return": "0.10",
        "profit_factor": None,
        "profit_factor_reason": "no_losing_trades",
    }
    state = HypothesisDevelopmentState(
        workspace_id=WORKSPACE,
        execution_id="report-fixture",
        phase=HypothesisDevelopmentPhase.VARIATION,
        intent_epoch=0,
        variation=VariationEvaluation.from_dict(payload),
    )
    forward = cast(dict[str, Any], _forward_envelope())
    forward["variation"].update(method="one_at_a_time", interactions=[])
    forward["variation"]["center"]["metrics"].update(
        total_return="0.05", profit_factor="1.2"
    )
    joint = {**payload, "method": "joint", "interactions": [["fast", "slow"]]}
    joint["points"] = [
        *payload["points"],
        {
            "parameters": {"fast": 6, "slow": 21},
            "jobId": "strategy-momentum-joint-1",
            "state": "succeeded",
            "metrics": {"total_return": "0.08", "profit_factor": "1.1"},
            "error": None,
        },
    ]
    typed_development = deepcopy(payload)
    typed_development["points"].append(
        {
            "parameters": {"fast": 5.0, "slow": 20},
            "jobId": "strategy-momentum-float-development",
            "state": "succeeded",
            "metrics": {"total_return": "0.22", "profit_factor": "2.2"},
            "error": None,
        }
    )
    typed_forward = deepcopy(forward)
    typed_forward["variation"]["points"].append(
        {
            "parameters": {"fast": 5.0, "slow": 20},
            "jobId": "strategy-momentum-float-forward",
            "state": "succeeded",
            "metrics": {"total_return": "0.09", "profit_factor": "1.3"},
            "error": None,
        }
    )
    return {
        "development": variation_report_artifact(state),
        "joint": variation_report_artifact(
            replace(state, variation=VariationEvaluation.from_dict(joint))
        ),
        "forward": forward_report_artifact(
            replace(state, forward_validation=ForwardEvaluation.from_dict(forward))
        ),
        "typedIdentity": forward_report_artifact(
            replace(
                state,
                variation=VariationEvaluation.from_dict(typed_development),
                forward_validation=ForwardEvaluation.from_dict(typed_forward),
            )
        ),
    }


def test_report_transport_matches_backend_fixture() -> None:
    generated = report_fixture()
    assert generated == json.loads(FIXTURE.read_text(encoding="utf-8"))
    forward = cast(dict[str, Any], generated["forward"])
    assert forward["matchedDevelopment"] == generated["development"]
    assert forward["evidence"]["observed"]["jobId"] == forward["center"]["jobId"]
    assert cast(dict[str, Any], generated["development"])["points"][2]["error"]
    typed = cast(dict[str, Any], generated["typedIdentity"])
    integer = typed["center"]
    decimal = typed["points"][-1]
    assert integer["parameterIdentity"] != decimal["parameterIdentity"]
    assert integer["levelIdentities"]["fast"] != decimal["levelIdentities"]["fast"]
    assert (
        decimal["parameterIdentity"]
        == typed["matchedDevelopment"]["points"][-1]["parameterIdentity"]
    )


if __name__ == "__main__":
    FIXTURE.write_text(json.dumps(report_fixture(), indent=2) + "\n", encoding="utf-8")
