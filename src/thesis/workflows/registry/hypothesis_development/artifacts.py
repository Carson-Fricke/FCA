"""Durable artifact projections for the workflow state."""

from __future__ import annotations

import math
from dataclasses import replace

from thesis.research.hypothesis import (
    HypothesisDocument,
    HypothesisFrontMatter,
    HypothesisLinks,
    HypothesisStatus,
)

from .contracts import (
    HypothesisDevelopmentRequest,
    HypothesisDraft,
)
from .services import DevelopmentEngine, services_of
from .state import HypothesisDevelopmentState


def build_hypothesis(
    workflow: DevelopmentEngine,
    request: HypothesisDevelopmentRequest,
    draft: HypothesisDraft,
) -> HypothesisDocument:
    hypothesis_id = draft.hypothesis_id or f"h-{request.execution_id}"
    front_matter = HypothesisFrontMatter(
        hypothesis_id=hypothesis_id,
        status=HypothesisStatus.ACTIVE,
        claim=draft.claim,
        disagreement=draft.disagreement,
        risk_factor=draft.risk_factor,
        horizon=draft.horizon,
        universe=draft.universe,
        falsifiers=draft.falsifiers,
        sizing=draft.sizing,
        links=HypothesisLinks(),
    )
    return HypothesisDocument(front_matter=front_matter, body=draft.body)


def project_hypothesis(
    workflow: DevelopmentEngine,
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
    *,
    status: str | None = None,
) -> HypothesisDevelopmentState:
    document = state.hypothesis
    if document is None:
        return state
    projected_status = document.status if status is None else HypothesisStatus(status)
    links = HypothesisLinks(
        runs=tuple(trial.job_id for trial in state.trials),
        evidence=tuple(trial.digest for trial in state.trials),
    )
    projected = document.projected(status=projected_status, links=links)
    services_of(workflow).store.save(projected, workspace_id=request.workspace_id)
    return replace(state, hypothesis=projected)


def discovery_report_artifact(
    state: HypothesisDevelopmentState,
) -> dict[str, object] | None:
    """Build the discovery phase's structured candidate artifact."""
    draft = state.discovery
    if draft is None:
        return None
    candidate = draft.candidate
    return {
        "type": "discovery",
        "content": f"Discovery candidate: {candidate.opportunity}",
        "candidate": candidate.as_dict(),
        "attempts": [attempt.as_dict() for attempt in draft.attempts],
        "untouchedData": draft.untouched_data,
        "limitations": draft.limitations,
    }


def falsification_report_artifact(
    workflow: DevelopmentEngine,
    state: HypothesisDevelopmentState,
) -> dict[str, object]:
    """Build the rich structured falsification trials report artifact."""
    tests_by_id = {t.test_id: t for t in (state.plan.tests if state.plan else ())}
    passed_count = sum(
        1 for t in state.trials if not t.falsified and t.observed is not None
    )
    falsified_count = sum(1 for t in state.trials if t.falsified)
    total_count = len(state.trials)
    survived = total_count > 0 and falsified_count == 0 and passed_count == total_count

    trials_data: list[dict[str, object]] = []
    for t in state.trials:
        planned = tests_by_id.get(t.test_id)
        trials_data.append(
            {
                "testId": t.test_id,
                "falsifierId": t.falsifier_id,
                "jobId": t.job_id,
                "runState": t.run_state,
                "detail": t.detail,
                "observed": t.observed,
                "falsified": t.falsified,
                "observable": (planned.observable if planned else t.falsifier_id),
                "condition": planned.condition if planned else "",
                "threshold": planned.threshold if planned else 0.0,
                "description": planned.description if planned else "",
            }
        )

    controls_data = [
        {
            "controlId": c.control_id,
            "description": c.description,
            "expectation": c.expectation,
        }
        for c in (state.plan.controls if state.plan else ())
    ]

    summary_text = (
        f"Ran {total_count} falsification trial(s): {passed_count} survived, {falsified_count} refuted."
        if total_count > 0
        else "No falsification trials were executed."
    )

    metrics_data: dict[str, object] = {}
    for name, val in (state.fitted_parameters or {}).items():
        if isinstance(val, (int, float)) and math.isfinite(float(val)):
            metrics_data[name] = float(val)
        else:
            metrics_data[name] = str(val)

    return {
        "type": "falsification_report",
        "content": summary_text,
        "passedCount": passed_count,
        "falsifiedCount": falsified_count,
        "totalCount": total_count,
        "survived": survived,
        "hypothesis": {
            "claim": (state.hypothesis.front_matter.claim if state.hypothesis else ""),
            "disagreement": (
                state.hypothesis.front_matter.disagreement if state.hypothesis else ""
            ),
            "universe": (
                list(state.hypothesis.front_matter.universe) if state.hypothesis else []
            ),
            "horizon": (
                state.hypothesis.front_matter.horizon if state.hypothesis else ""
            ),
        },
        "trials": trials_data,
        "controls": controls_data,
        "metrics": metrics_data,
    }
