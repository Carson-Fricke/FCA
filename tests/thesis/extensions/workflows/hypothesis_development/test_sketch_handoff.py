"""Accepted sketches remain the premise across trials, verdict and construction."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest

from thesis.extensions.workflows.hypothesis_development.contracts.validation import (
    HYPOTHESIS_PAYLOAD_SCHEMA,
    hypothesis_payload_errors,
)
from thesis.extensions.workflows.hypothesis_development.endpoint import (
    HypothesisExtension,
)
from thesis.extensions.workflows.hypothesis_development.exposure import (
    record_submission_exposure,
)
from thesis.extensions.workflows.hypothesis_development.session_services import (
    HypothesisSessionServices,
)
from thesis.research.evidence import document_digest
from thesis.workspaces.sketch_contract import SKETCH_SCHEMA, validate_sketch
from thesis_runtime.subagents.store import PersistenceSubAgentStore
from thesis_runtime.tools.schema import validate_arguments

from ._support import (
    WORKSPACE,
    FakePrograms,
    _discovery_envelope,
    _hypothesis_envelope,
    _kit,
    _node_record,
    _request,
    _research_envelope,
    _sketch_document,
    _sketch_reference,
)


def test_sketch_contract_supports_large_graphs_and_rejects_false_connections() -> None:
    """Strict graph decoding is pure untrusted-input validation (category 3)."""
    sketch = _sketch_document()
    sketch["representations"] = [
        {
            "nodeId": f"node-{index}",
            "description": "A provisional representation",
            "inputs": [] if index == 0 else [f"node-{index - 1}"],
            "timeframes": ["1min", "15min"],
            "informationTime": "After bar completion",
        }
        for index in range(32)
    ]
    assert not validate_arguments(SKETCH_SCHEMA, sketch)
    assert validate_sketch(sketch) == sketch
    bad = deepcopy(sketch)
    cast(list[dict[str, Any]], bad["representations"])[0]["inputs"] = ["unknown-node"]
    with pytest.raises(ValueError, match="unknown dependencies"):
        validate_sketch(bad)
    cast(list[dict[str, Any]], bad["representations"])[0]["inputs"] = ["node-31"]
    with pytest.raises(ValueError, match="cycle"):
        validate_sketch(bad)


def test_falsification_contract_requires_exact_sketch_and_declared_test_links() -> None:
    payload = _hypothesis_envelope()
    assert not validate_arguments(HYPOTHESIS_PAYLOAD_SCHEMA, payload)
    del payload["sketchReference"]
    assert hypothesis_payload_errors(payload)
    payload = _hypothesis_envelope()
    payload["assumptionTests"] = {"continuation": ["invented-test"]}
    assert any(
        "unknown falsifier" in item for item in hypothesis_payload_errors(payload)
    )


@pytest.mark.parametrize(
    "mutation",
    ["wrong_revision", "unknown_assumption", "unknown_test", "no_test", "valid"],
)
def test_verdict_binds_interpretation_to_accepted_sketch_and_executed_tests(
    tmp_path: Path,
    mutation: str,
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    state = kit.extension.state(
        request, kit.execution_store.get((request).execution_id).transitions
    )
    trial = {
        "testId": "T01",
        "falsifierId": "f1",
        "jobId": "job-1",
        "digest": "a" * 64,
        "runState": "succeeded",
        "observed": 1.0,
        "falsified": False,
    }
    for completion in (
        {"target": "model_creation", "discovery": _discovery_envelope()},
        {"target": "falsification_trials", "hypothesis": _hypothesis_envelope()},
        {
            "target": "verdict",
            "sketchReference": _sketch_reference(),
            "trials": [trial],
            "research": _research_envelope(),
        },
    ):
        state = kit.workflow._apply_contract(
            request,
            state,
            state.phase if isinstance(state.phase, str) else state.phase.value,
            completion,
        )
    node = kit.workflow.prepare_phase(request)
    assert node is not None
    record = _node_record(kit, node)
    verdict = {
        "outcome": "supported",
        "reason": "Construction is worth attempting",
        "sketchReference": _sketch_reference(),
        "assumptionAssessments": [
            {
                "assumptionId": "continuation",
                "status": "supported",
                "reason": "Recorded trial survived",
                "testIds": ["T01"],
            }
        ],
    }
    assessment = verdict["assumptionAssessments"][0]
    if mutation == "wrong_revision":
        verdict["sketchReference"]["version"] = "f" * 64
    elif mutation == "unknown_assumption":
        assessment["assumptionId"] = "invented-assumption"
    elif mutation == "unknown_test":
        assessment["testIds"] = ["invented-test"]
    elif mutation == "no_test":
        assessment["testIds"] = []
    service = HypothesisSessionServices(
        chat=kit.chat,
        store=PersistenceSubAgentStore(kit.chat.persistence),
        harness_tools=None,
        programs=FakePrograms(),
        extension=cast(HypothesisExtension, (kit.workflow).extension),
        workflow=lambda: kit.workflow,
        activity=lambda context, tool, arguments, execute: execute(),
    )
    failure = service.validate_completion(
        WORKSPACE,
        record,
        {
            "intentEpoch": 0,
            "target": "strategy_design",
            "verdict": verdict,
        },
    )
    assert (failure is None) == (mutation == "valid")


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "provisional",
        "foreign_job",
        "never_started",
        "missing_evidence",
        "failed_job",
    ],
)
def test_discovery_accepts_only_its_executed_jobs_as_sketch_evidence(
    tmp_path: Path,
    case: str,
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node = kit.workflow.prepare_phase(request)
    assert node is not None
    record = _node_record(kit, node)
    sketch = _sketch_document()
    assumption = cast(list[dict[str, Any]], sketch["assumptions"])[0]
    assumption.update(status="supported", evidence=[{"jobId": "job-momentum"}])
    if case == "provisional":
        assumption.update(status="untested", evidence=[])
    elif case == "foreign_job":
        assumption["evidence"] = [{"jobId": "another-workflow-job"}]
    elif case == "missing_evidence":
        assumption["evidence"] = []

    class Programs(FakePrograms):
        def verify_strategy_sketch(self, workspace_id, reference):
            super().verify_strategy_sketch(workspace_id, reference)
            return deepcopy(sketch)

    programs = Programs()
    accepted = {"job_id": "job-momentum"}
    attribution = {
        "phase": "discovery",
        "programId": "momentum-study",
        "kind": "study",
        "version": "c" * 64,
        "gitCommit": "d" * 40,
        "submittedInputSha256": document_digest(accepted),
    }
    kit.chat.submit_tool_job(
        WORKSPACE,
        request.execution_id,
        tool_name="run_study",
        actor_id="operator",
        job_id="job-momentum",
        attribution=attribution,
    )
    record_submission_exposure(
        kit.extension.activities(request),
        request,
        phase="discovery",
        job_id="job-momentum",
        tool_name="run_study",
        window_start="2024-01-01T00:00:00Z",
        window_end="2024-03-01T00:00:00Z",
        parameter_sha256="a" * 64,
        design_sha256=None,
        phase_attempt_id=record.phase_attempt_id,
    )
    if case != "never_started":
        kit.chat.start_tool_job(WORKSPACE, "job-momentum", actor_id="operator")
    programs.results["job-momentum"] = {
        "job_id": "job-momentum",
        "state": "failed" if case == "failed_job" else "succeeded",
        "accepted_input": accepted,
        "metrics": {},
    }
    service = HypothesisSessionServices(
        chat=kit.chat,
        store=PersistenceSubAgentStore(kit.chat.persistence),
        harness_tools=None,
        programs=programs,
        extension=cast(HypothesisExtension, (kit.workflow).extension),
        workflow=lambda: kit.workflow,
        activity=lambda context, tool, arguments, execute: execute(),
    )
    failure = service.validate_completion(
        WORKSPACE,
        record,
        {
            "intentEpoch": 0,
            "target": "model_creation",
            "discovery": _discovery_envelope(),
        },
    )
    assert (failure is None) == (case in ("valid", "provisional"))
