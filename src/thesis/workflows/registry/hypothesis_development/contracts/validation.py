"""Advertised contract schemas and semantic checks beyond structure.

The canonical agent JSON Schema validator owns structural validation. Domain
checks cover subscription consistency and the authored model's observables;
phase validators additionally compose typed contracts before acceptance.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from thesis.agent.runtime.schema import violations
from thesis.paths import InvalidRelativePath, confined_relative_path
from thesis.research.grammar import IDENTIFIER_PATTERN
from thesis.research.models import MARKET_SYMBOL_PATTERN
from thesis.workflows.research_models import PROGRAM_REFERENCE_SCHEMA

HYPOTHESIS_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claim": {
            "type": "string",
            "minLength": 1,
            "maxLength": 8192,
            "pattern": r"\S",
        },
        "disagreement": {
            "type": "string",
            "minLength": 1,
            "maxLength": 8192,
            "pattern": r"\S",
        },
        "riskFactor": {"type": "string", "maxLength": 4096},
        "horizon": {"type": "string", "maxLength": 4096},
        "universe": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "pattern": MARKET_SYMBOL_PATTERN},
        },
        "falsifiers": {
            "type": "array",
            "minItems": 1,
            "description": (
                "The complete operator-approved research falsifier suite: "
                "cover fit/data failure, departure from the no-structure null, "
                "wrong-direction or too-weak regularity, and a negative control. "
                "Data availability alone does not test the hypothesis. Never "
                "shrink the approved suite while repairing tool arguments."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "falsifierId": {"type": "string", "pattern": IDENTIFIER_PATTERN},
                    "falsifier_id": {"type": "string", "pattern": IDENTIFIER_PATTERN},
                    "observable": {"type": "string", "pattern": IDENTIFIER_PATTERN},
                    "condition": {"type": "string", "enum": ["lt", "lte", "gt", "gte"]},
                    "threshold": {"type": "number"},
                    "test": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 8192,
                        "pattern": r"\S",
                    },
                },
                "required": ["observable", "condition", "threshold", "test"],
            },
        },
        "model": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                **cast(dict[str, object], PROGRAM_REFERENCE_SCHEMA["properties"]),
                "kind": {"type": "string", "enum": ["study"]},
                "description": {"type": "string", "minLength": 1, "pattern": r"\S"},
                "outputs": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {"type": "string", "pattern": IDENTIFIER_PATTERN},
                },
                "tests": {
                    "type": "object",
                    "minProperties": 1,
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": [
                "workspaceId",
                "kind",
                "programId",
                "gitCommit",
                "version",
                "description",
                "outputs",
                "tests",
            ],
        },
        "sizing": {"type": "string"},
        "body": {"type": "string"},
        "hypothesisId": {"type": "string", "pattern": IDENTIFIER_PATTERN},
    },
    "required": ["claim", "disagreement", "falsifiers", "model"],
}

HYPOTHESIS_COMPLETION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target": {"type": "string", "enum": ["falsification_trials"]},
        "hypothesis": HYPOTHESIS_PAYLOAD_SCHEMA,
    },
    "required": ["hypothesis"],
}

DISCOVERY_CANDIDATE_FIELDS: tuple[str, ...] = (
    "opportunity",
    "signal",
    "informationTime",
    "direction",
    "holdingPeriod",
    "profitSource",
    "effect",
    "competingExplanation",
    "evidence",
)

DISCOVERY_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "candidate": {
            "type": "object",
            "properties": {
                name: {
                    "type": "string",
                    "minLength": 1,
                    "pattern": r"\S",
                    "maxLength": 8192 if name == "evidence" else 4096,
                }
                for name in DISCOVERY_CANDIDATE_FIELDS
            },
            "required": list(DISCOVERY_CANDIDATE_FIELDS),
        },
        "attempts": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "purpose": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 4096,
                        "pattern": r"\S",
                    },
                    "studyId": {"type": "string", "pattern": IDENTIFIER_PATTERN},
                    "dataTypes": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "pattern": IDENTIFIER_PATTERN},
                    },
                    "window": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 4096,
                        "pattern": r"\S",
                    },
                    "result": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 4096,
                        "pattern": r"\S",
                    },
                    "changed": {"type": "boolean"},
                    "runId": {
                        "type": ["string", "null"],
                        "pattern": IDENTIFIER_PATTERN,
                    },
                },
                "required": ["purpose", "studyId", "dataTypes", "window", "result"],
            },
        },
        "untouchedData": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4096,
            "pattern": r"\S",
        },
        "limitations": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4096,
            "pattern": r"\S",
        },
    },
    "required": ["candidate", "attempts", "untouchedData", "limitations"],
}

DISCOVERY_COMPLETION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target": {"type": "string", "enum": ["model_creation"]},
        "discovery": DISCOVERY_PAYLOAD_SCHEMA,
    },
    "required": ["discovery"],
}


def discovery_payload_errors(payload: Mapping[str, object]) -> list[str]:
    """Return every structural miss using the advertised discovery schema."""
    return [
        miss.render()
        for miss in violations(DISCOVERY_PAYLOAD_SCHEMA, payload, "discovery")
    ]


def hypothesis_payload_errors(payload: Mapping[str, object]) -> list[str]:
    """Collect structural misses and cross-field authored-model constraints."""
    errors = [
        miss.render()
        for miss in violations(HYPOTHESIS_PAYLOAD_SCHEMA, payload, "hypothesis")
    ]
    model = payload.get("model")
    falsifiers = payload.get("falsifiers")
    if isinstance(model, Mapping):
        tests = model.get("tests")
        if isinstance(tests, Mapping):
            for path in tests:
                try:
                    confined_relative_path(path, f"hypothesis.model.tests[{path!r}]")
                except InvalidRelativePath as error:
                    errors.append(str(error))
        outputs = model.get("outputs")
        if isinstance(outputs, Sequence) and not isinstance(outputs, str):
            declared = {name for name in outputs if isinstance(name, str)}
            if isinstance(falsifiers, Sequence) and not isinstance(falsifiers, str):
                missing = sorted(
                    {
                        item["observable"]
                        for item in falsifiers
                        if isinstance(item, Mapping)
                        and isinstance(item.get("observable"), str)
                        and item["observable"] not in declared
                    }
                )
                if missing:
                    errors.append(
                        "hypothesis.falsifiers: observable(s) absent from the newly "
                        f"authored model.outputs: {', '.join(missing)}"
                    )
    return errors


def format_contract_errors(errors: Sequence[str]) -> str:
    """Format every miss in one repair response."""
    listed = "\n".join(f"- {error}" for error in errors)
    return (
        f"completion contract rejected with {len(errors)} problem(s) - fix "
        f"every problem and call request_phase_completion again:\n{listed}"
    )


__all__ = [
    "DISCOVERY_CANDIDATE_FIELDS",
    "DISCOVERY_COMPLETION_SCHEMA",
    "DISCOVERY_PAYLOAD_SCHEMA",
    "HYPOTHESIS_COMPLETION_SCHEMA",
    "HYPOTHESIS_PAYLOAD_SCHEMA",
    "discovery_payload_errors",
    "format_contract_errors",
    "hypothesis_payload_errors",
]
