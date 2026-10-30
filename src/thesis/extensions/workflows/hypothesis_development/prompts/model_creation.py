"""Executable falsification planning and conversational operator acceptance."""

from __future__ import annotations

from collections.abc import Mapping

from .sdk import SDK_AUTHORING_GUIDANCE

_LATEX_INSTRUCTION = "Render math as LaTeX: inline \\(...\\), display \\[...\\]; reserve bare $ for currency."


def _model_creation_instructions() -> str:
    """Prepare one source-aligned falsification study over the accepted sketch."""
    return (
        """You own model creation: prepare executable falsification trials for the
accepted strategy sketch. Read its exact revision and pinned representations.
Translate the assumptions connecting mechanism, conditioning and trading rules
into observable challenges. Preserve the substantive candidate; a generic bar
test cannot replace a question about composed features or episodes.

Specify variables, estimator, sampling, windows, expected direction, uncertainty
and appropriate controls, with reasons. Layer A tests the historical regularity
against a suitable no-structure baseline, wrong direction and inadequate support.
Layer B describes trailing monitors for when its historical assumptions cease
to hold. Respect the frozen development window and keep forward data sealed.
If the requested window cannot support the inference, explain the limitation
and ask rather than expanding it silently.

Present the model, data and falsifiers/controls as a complete proposal in ordinary
reply text. In the same response call request_workflow_input for acceptance.
Incorporate corrections and ask again; finalize after the operator accepts.
Ask from this agent's own node, without relaying to the parent.

Author/update one executable study with owned indicator dependencies and validate
it. This phase prepares the model; downstream deterministic trials execute it.
Publish the exact named observables and at least one fit.<parameter> metric.
Publish meaningful inadequate-data indicators rather than panicking. Use targeted
knowledge and SDK reads for the method and implementation.

Complete with hypothesis containing claim, disagreement, riskFactor, horizon,
universe, body, unchanged sketchReference, assumptionTests and falsifiers. Map
each investigated assumptionId to actual falsifier IDs; retain untested assumptions.
Each falsifier specifies falsifierId, observable, condition, threshold, test and
role (claim, assumption or control). Include meaningful claim tests, interpretability
checks and a negative control. Calibrate thresholds to feasible independent support.

The model is the successful validated study reference (workspaceId, kind=study,
programId, gitCommit, version), with description, outputs and non-empty tests
mapping portable relative file names to test text. Its committed configuration
defines exact subscriptions and dates; subscribed symbols must belong to universe.
Explain assumptions and limitations in body. Complete through the phase tool.
\n"""
        + SDK_AUTHORING_GUIDANCE
        + "\n"
        + _LATEX_INSTRUCTION
    )


def _model_creation_revision_section(
    verdict: Mapping[str, object] | None,
    report: Mapping[str, object] | None,
) -> str:
    """Send only the changed decision on a revisit of the same child."""
    lines = [
        "Revise the model using the accepted verdict and recorded trial findings.",
        "Retain useful evidence and the selected candidate unless the accepted scope changes.",
        "Present the revision and ask through request_workflow_input; complete after acceptance.",
        "Verdict reason: " + str(_verdict_payload(verdict).get("reason") or ""),
        *_trial_findings(report),
    ]
    return "\n".join(lines)


def _verdict_payload(verdict: Mapping[str, object] | None) -> Mapping[str, object]:
    """Return the verdict object from a loop-back completion payload."""
    if not verdict:
        return {}
    nested = verdict.get("verdict")
    return nested if isinstance(nested, Mapping) else verdict


def _trial_findings(report: Mapping[str, object] | None) -> list[str]:
    """Render the recorded trials as compact, one-line findings.

    The trial descriptions are deliberately omitted: the agent authored its
    own falsifiers, so their full definitions are already in its transcript and
    only the recorded outcome is new information for the revision.
    """
    if not report:
        return []
    lines: list[str] = []
    summary = report.get("content")
    if isinstance(summary, str) and summary.strip():
        lines.append(summary.strip())
    trials = report.get("trials")
    if isinstance(trials, list):
        for trial in trials:
            if not isinstance(trial, Mapping):
                continue
            status = "FIRED (refutes)" if trial.get("falsified") else "survived"
            lines.append(
                f"- {trial.get('testId')} {trial.get('falsifierId')}: "
                f"{trial.get('observable')} observed {trial.get('observed')} "
                f"vs {trial.get('condition')} {trial.get('threshold')} "
                f"— {status}"
            )
    metrics = report.get("metrics")
    if isinstance(metrics, Mapping) and metrics:
        published = ", ".join(
            f"{name}={value}" for name, value in sorted(metrics.items())
        )
        lines.append(f"Fitted metrics: {published}")
    return lines


__all__ = [
    "_LATEX_INSTRUCTION",
    "_model_creation_instructions",
    "_model_creation_revision_section",
]
