"""Model test keys must be portable paths before the creation contract commits."""

from pathlib import Path

import pytest
from tests.thesis.extensions.workflows.hypothesis_development._support import (
    _phase_attempt,
)

from thesis.extensions.workflows.hypothesis_development.contracts.validation import (
    hypothesis_payload_errors,
)
from thesis_runtime.workflows.workflow import PhaseCompletionRejected

from ._support import (
    WORKSPACE,
    _discovery_envelope,
    _hypothesis_envelope,
    _kit,
    _phase_node_id,
    _request,
)


def test_reported_condition_key_is_rejected_by_the_payload_validator() -> None:
    payload = _hypothesis_envelope()
    model = payload["model"]
    assert isinstance(model, dict)
    model["tests"] = {"fit.data_ok < 1.0": "metric gate"}

    errors = hypothesis_payload_errors(payload)

    assert any(
        "hypothesis.model.tests" in error and "confined relative path" in error
        for error in errors
    )


def test_invalid_test_key_rejects_completion_before_trials(tmp_path: Path) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    discovery_node = kit.workflow.prepare_phase(request)
    assert discovery_node is not None
    kit.workflow.apply_phase_completion(
        request,
        discovery_node,
        {"target": "model_creation", "discovery": _discovery_envelope()},
        attempt_id=_phase_attempt(kit.workflow, request.workspace_id, discovery_node),
    )
    invalid = _hypothesis_envelope()
    model = invalid["model"]
    assert isinstance(model, dict)
    model["tests"] = {"fit.data_ok < 1.0": "metric gate"}

    with pytest.raises(PhaseCompletionRejected, match="confined relative path"):
        kit.workflow.apply_phase_completion(
            request,
            _phase_node_id(kit, request, "model_creation"),
            {"target": "falsification_trials", "hypothesis": invalid},
            attempt_id=_phase_attempt(
                kit.workflow,
                request.workspace_id,
                _phase_node_id(kit, request, "model_creation"),
            ),
        )

    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.phase == "model_creation"
