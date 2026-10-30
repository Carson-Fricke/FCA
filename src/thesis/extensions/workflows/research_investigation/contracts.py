"""Current launch and accepted handoff contracts for one research branch."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from thesis.workspaces.sketch_contract import SKETCH_REFERENCE_SCHEMA

WORKFLOW_ID = "research-investigation"
VERSION = "6.0.0"
REVIEW_PHASES = {
    "implementation": "implementation_critique",
    "experimental": "experimental_critique",
    "alignment": "alignment_critique",
}
CRITIC_PHASES = (*REVIEW_PHASES.values(), "focused_critique")
CONSTRUCTION_PHASES = ("proposal", "investigation", "adjudicate")
SETTLED_ISSUE_STATUSES = frozenset(("verified", "accept", "escalate"))


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
REPAIRS_SCHEMA = {
    "type": "array",
    "maxItems": 48,
    "items": record({"issueId": text(128), "response": text()}),
}
REVIEW_SCHEMA = record(
    {
        "target": {"enum": ["critique", "focused_critique"]},
        "scope": {
            **text(),
            "description": "Specialty, changed claims and sufficient evidence for the next review; exclude settled findings and discovery-owned decisions.",
        },
        "issueIds": {
            "type": "array",
            "maxItems": 48,
            "uniqueItems": True,
            "items": text(128),
        },
    }
)
PROPOSAL_SCHEMA = record(
    {
        "attempts": {"type": "array", "items": ATTEMPT_SCHEMA, "maxItems": 512},
        "evidence": {"type": "array", "items": EVIDENCE_SCHEMA, "maxItems": 512},
        "construction": text(),
        "design": text(12000),
        "decision": text(),
        "contrastingOutcomes": text(),
        "interpretationLimits": text(),
        "repairs": REPAIRS_SCHEMA,
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
                    "reopenReason": {
                        "type": ["string", "null"],
                        "minLength": 1,
                        "maxLength": 4096,
                        "description": "New contradictory evidence when reopening a settled issue; otherwise null. Reuse the original issueId.",
                    },
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
                    "disposition": {
                        "enum": ["repair", "accept", "unresolved", "escalate"],
                        "description": "escalate returns a discovery-owned decision without making it a local repair prerequisite.",
                    },
                    "reason": text(),
                }
            ),
        },
        "remainingUncertainty": text(),
        "repairs": REPAIRS_SCHEMA,
        "attempts": {"type": "array", "items": ATTEMPT_SCHEMA, "maxItems": 512},
        "evidence": {"type": "array", "items": EVIDENCE_SCHEMA, "maxItems": 512},
        "review": {
            "anyOf": [REVIEW_SCHEMA, {"type": "null"}],
            "description": "Explicit next review, including after delegated repair. Null only when completing the branch.",
        },
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
        "critique": tuple(f"{phase}.input" for phase in REVIEW_PHASES.values()),
        "focused_critique": ("focused_critique.input",),
        **{
            target: (f"{target}.input",)
            for target in ("proposal", "investigation", "completed")
        },
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
