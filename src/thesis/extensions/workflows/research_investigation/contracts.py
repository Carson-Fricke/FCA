"""Current launch and accepted handoff contracts for one research branch."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from thesis.workspaces.sketch_contract import SKETCH_REFERENCE_SCHEMA

WORKFLOW_ID = "research-investigation"
VERSION = "4.0.0"
REVIEW_PHASES = {
    "implementation": "implementation_critique",
    "experimental": "experimental_critique",
    "alignment": "alignment_critique",
}
CRITIC_PHASES = (*REVIEW_PHASES.values(), "focused_critique")


def text(limit: int = 4096) -> dict[str, object]:
    return {"type": "string", "minLength": 1, "maxLength": limit}


def record(properties: Mapping[str, object]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": dict(properties),
    }


EVIDENCE_SCHEMA = record({"jobId": text(128), "artifactName": text(256)})
LAUNCH_SCHEMA = record(
    {
        "direction": text(8192),
        "question": text(),
        "decision": text(),
        "constraints": text(),
        "sketchReference": SKETCH_REFERENCE_SCHEMA,
        "evidence": {"type": "array", "items": EVIDENCE_SCHEMA, "maxItems": 32},
        "revisionBudget": {"type": "integer", "minimum": 0, "maximum": 8},
        "executionBudget": {"type": "integer", "minimum": 1, "maximum": 512},
    }
)

PROPOSAL_SCHEMA = record(
    {
        "construction": text(),
        "design": text(12000),
        "decision": text(),
        "contrastingOutcomes": text(),
        "interpretationLimits": text(),
        "repairs": {
            "type": "array",
            "maxItems": 48,
            "items": record({"issueId": text(128), "response": text()}),
        },
    }
)
CRITIQUE_SCHEMA = record(
    {
        "assessment": text(),
        "strengths": text(),
        "verifiedRepairs": {
            "type": "array",
            "maxItems": 16,
            "uniqueItems": True,
            "items": record({"issueId": text(128), "evidence": text()}),
        },
        "issues": {
            "type": "array",
            "maxItems": 16,
            "items": record(
                {
                    "issueId": text(128),
                    "concern": text(),
                    "evidence": text(),
                    "consequence": text(),
                    "repair": text(),
                }
            ),
        },
    }
)
ADJUDICATION_SCHEMA = record(
    {
        "reason": text(),
        "resolutions": {
            "type": "array",
            "maxItems": 48,
            "items": record(
                {
                    "issueId": text(128),
                    "disposition": {"enum": ["repair", "accept", "unresolved"]},
                    "reason": text(),
                }
            ),
        },
        "remainingUncertainty": text(),
    }
)
ATTEMPT_SCHEMA = record(
    {
        "jobId": text(128),
        "purpose": text(),
        "dataTypes": {"type": "array", "minItems": 1, "items": text(128)},
        "window": text(),
        "result": text(),
        "changed": {"type": "boolean"},
    }
)
INVESTIGATION_SCHEMA = record(
    {
        "interpretation": text(12000),
        "limitations": text(),
        "attempts": {"type": "array", "items": ATTEMPT_SCHEMA, "maxItems": 512},
        "evidence": {"type": "array", "items": EVIDENCE_SCHEMA, "maxItems": 512},
    }
)
ROUTES = {
    "proposal": {
        "critique": tuple(f"{phase}.input" for phase in REVIEW_PHASES.values()),
        "focused_critique": ("focused_critique.input",),
    },
    **{
        phase: {"collect_critiques": (f"collect_critiques.{name}",)}
        for name, phase in REVIEW_PHASES.items()
    },
    "collect_critiques": {"adjudicate": ("adjudicate.input",)},
    "focused_critique": {"adjudicate": ("adjudicate.input",)},
    "adjudicate": {
        target: (f"{target}.input",)
        for target in ("proposal", "investigation", "completed")
    },
    "investigation": {
        "critique": tuple(f"{phase}.input" for phase in REVIEW_PHASES.values()),
        "focused_critique": ("focused_critique.input",),
    },
    "completed": {},
    "failed": {},
}
PAYLOADS = {
    "proposal": PROPOSAL_SCHEMA,
    **{phase: CRITIQUE_SCHEMA for phase in CRITIC_PHASES},
    "collect_critiques": record({name: CRITIQUE_SCHEMA for name in REVIEW_PHASES}),
    "adjudicate": ADJUDICATION_SCHEMA,
    "investigation": INVESTIGATION_SCHEMA,
}


def completion_schema(phase: str) -> dict[str, Any]:
    """Advertise the same contract the extension validates on acceptance."""
    field = (
        "adjudication"
        if phase == "adjudicate"
        else "critique"
        if phase in CRITIC_PHASES
        else "critiques"
        if phase == "collect_critiques"
        else phase
    )
    return record(
        {
            "target": {"enum": list(ROUTES[phase])},
            "intentEpoch": {"type": "integer", "minimum": 0},
            field: PAYLOADS[phase],
        }
    )
