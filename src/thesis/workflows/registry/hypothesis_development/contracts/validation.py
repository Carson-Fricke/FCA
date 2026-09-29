"""Deterministic, complete shape validation for completion contracts.

The creation phase's completion contract is the one handoff whose payload the
deterministic successor phase composes into typed objects.  These helpers give
that contract one authoritative shape: a machine-checkable JSON Schema the
tool catalog advertises so the provider constrains the call at decode time,
and an error collector that reports every missing or wrongly-typed field in
one deterministic pass so the repair turn is precise.

The advertised schema and the collector are both mirrored from the composer
the trials phase runs (:func:`..phases.trials._draft_from_contract` plus the
compose helpers it delegates to), and both read the identifier, name, and
version grammars from :mod:`thesis.research.grammar`, so the three can never
drift apart.  The schema and the collector are only ever stricter than the
composer, never weaker; the spec's creation validator additionally re-runs the
composer as a drift guard, so a contract is accepted here if and only if the
deterministic successor can consume it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from thesis.research.grammar import (
    IDENTIFIER_PATTERN,
    NAME_PATTERN,
    VERSION_PATTERN,
)
from thesis.research.hypothesis import HypothesisFalsifier
from thesis.research.models import BAR_INTERVALS

#: The identifier grammar shared with the strictly validated front matter.
_IDENTIFIER = re.compile(IDENTIFIER_PATTERN)
_NAME = re.compile(NAME_PATTERN)
_VERSION = re.compile(VERSION_PATTERN)

_CONDITIONS = ("lt", "lte", "gt", "gte")
_SCOPES = ("global", "workspace")

#: Text caps mirrored from the hypothesis front matter's ``NonEmptyText``.
_MAX_HYPOTHESIS_TEXT = 8_192

#: The machine-checkable shape of the ``hypothesis`` payload, mirrored exactly
#: from the prompt-side schema string and the composer's requirements.  It is
#: never weaker than the composer: every declarative constraint the composer
#: enforces is expressed here, and where the composer is more permissive (for
#: example its tolerance of alternate harness-window key spellings) this shape
#: stays with the stricter primary spelling instead.
HYPOTHESIS_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claim": {
            "type": "string",
            "minLength": 1,
            "maxLength": _MAX_HYPOTHESIS_TEXT,
        },
        "disagreement": {
            "type": "string",
            "minLength": 1,
            "maxLength": _MAX_HYPOTHESIS_TEXT,
        },
        "riskFactor": {"type": "string"},
        "horizon": {"type": "string"},
        "universe": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "string",
                "minLength": 1,
                "pattern": IDENTIFIER_PATTERN,
            },
        },
        "falsifiers": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "falsifierId": {
                        "type": "string",
                        "pattern": IDENTIFIER_PATTERN,
                    },
                    "falsifier_id": {
                        "type": "string",
                        "pattern": IDENTIFIER_PATTERN,
                    },
                    "observable": {
                        "type": "string",
                        "minLength": 1,
                        "pattern": IDENTIFIER_PATTERN,
                    },
                    "condition": {"type": "string", "enum": list(_CONDITIONS)},
                    "threshold": {"type": "number"},
                    "test": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _MAX_HYPOTHESIS_TEXT,
                    },
                },
                "required": ["observable", "condition", "threshold", "test"],
                # The strictly validated falsifier model forbids extra keys;
                # both identifier spellings the composer normalizes are legal.
                "additionalProperties": False,
            },
        },
        "model": {
            "type": "object",
            "properties": {
                "modelId": {"type": "string", "pattern": IDENTIFIER_PATTERN},
                "version": {"type": "string", "pattern": VERSION_PATTERN},
                "description": {"type": "string", "minLength": 1},
                "source": {"type": "string", "minLength": 1},
                # The composer accepts an array of identifier strings only:
                # observables are the model's own statistic names, never
                # objects carrying a separate type field.
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
                "harnessConfiguration": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "string", "minLength": 1},
                        "end": {"type": "string", "minLength": 1},
                        # The bar interval the study replays at; the harness
                        # validates the same enum in the next wave.
                        "interval": {
                            "type": "string",
                            "minLength": 1,
                            "enum": list(BAR_INTERVALS),
                        },
                    },
                    "required": ["start", "end", "interval"],
                },
                "indicators": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "alias": {
                                "type": "string",
                                "minLength": 1,
                                "pattern": IDENTIFIER_PATTERN,
                            },
                            "scope": {"type": "string", "enum": list(_SCOPES)},
                            "name": {
                                "type": "string",
                                "minLength": 1,
                                "pattern": NAME_PATTERN,
                            },
                            "symbol": {
                                "type": "string",
                                "minLength": 1,
                                "pattern": IDENTIFIER_PATTERN,
                            },
                            "parameters": {"type": "object"},
                            "contentSha256": {"type": ["string", "null"]},
                            "definition": {"type": ["object", "null"]},
                        },
                        "required": ["alias", "scope", "name", "symbol"],
                    },
                },
            },
            "required": [
                "modelId",
                "version",
                "description",
                "source",
                "outputs",
                "tests",
                "harnessConfiguration",
            ],
        },
        "sizing": {"type": "string"},
        "body": {"type": "string"},
        "hypothesisId": {"type": "string", "pattern": IDENTIFIER_PATTERN},
    },
    "required": ["claim", "disagreement", "falsifiers", "model"],
}

#: The advertised completion contract for the hypothesis-creation phase. The
#: target enum names only the real successor: the engine still accepts a
#: terminal failure target, but the advertised shape never invites the agent
#: to end the whole run itself.
HYPOTHESIS_COMPLETION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target": {"type": "string", "enum": ["falsification_trials"]},
        "hypothesis": HYPOTHESIS_PAYLOAD_SCHEMA,
    },
    "required": ["hypothesis"],
}


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _text_error(
    path: str, value: object, *, max_length: int | None = None
) -> str | None:
    if value is None:
        return f"{path}: required non-empty text is missing"
    if not isinstance(value, str):
        return f"{path}: must be text, got {type(value).__name__}"
    if not value.strip():
        return f"{path}: must be non-empty text"
    if max_length is not None and len(value.strip()) > max_length:
        return f"{path}: exceeds maximum length of {max_length} characters"
    return None


def _sequence_error(path: str, value: object, label: str) -> str | None:
    if value is None:
        return f"{path}: required {label} is missing"
    if isinstance(value, str) or not isinstance(value, Sequence):
        return f"{path}: must be a {label}, got {type(value).__name__}"
    return None


def _output_errors(outputs: Sequence[object]) -> list[str]:
    """Collect every output-identifier problem, mirroring the composer."""
    if not outputs:
        return ["hypothesis.model.outputs: must contain at least one output"]
    errors: list[str] = []
    normalized: list[str] = []
    for index, item in enumerate(outputs):
        path = f"hypothesis.model.outputs[{index}]"
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{path}: must be a non-empty identifier")
            continue
        text = item.strip()
        if _IDENTIFIER.fullmatch(text) is None:
            errors.append(f"{path}: must match the identifier grammar {item!r}")
            continue
        normalized.append(text)
    if len(set(normalized)) != len(normalized):
        errors.append("hypothesis.model.outputs: entries must be unique")
    return errors


def _tests_errors(tests: Mapping[object, object]) -> list[str]:
    """Collect every test-mapping problem, mirroring the composer."""
    errors: list[str] = []
    if not tests:
        errors.append("hypothesis.model.tests: must contain at least one test")
    for path, content in tests.items():
        if not isinstance(path, str) or not path.strip():
            errors.append("hypothesis.model.tests: test paths must be non-empty text")
        if not isinstance(content, str):
            errors.append(
                f"hypothesis.model.tests[{path!r}]: must be text, "
                f"got {type(content).__name__}"
            )
    return errors


def _model_errors(model: object) -> list[str]:
    if not isinstance(model, Mapping):
        return [
            "hypothesis.model: required object is missing"
            if model is None
            else f"hypothesis.model: must be an object, got {type(model).__name__}"
        ]
    errors: list[str] = []
    model_id = model.get("modelId")
    error = _text_error("hypothesis.model.modelId", model_id)
    if error:
        errors.append(error)
    elif _IDENTIFIER.fullmatch(str(model_id).strip()) is None:
        errors.append(
            f"hypothesis.model.modelId: must match the identifier grammar {model_id!r}"
        )
    version = model.get("version")
    error = _text_error("hypothesis.model.version", version)
    if error:
        errors.append(error)
    elif _VERSION.fullmatch(str(version).strip()) is None:
        errors.append(
            f"hypothesis.model.version: must match the semantic-version grammar "
            f"{version!r}"
        )
    for key in ("description", "source"):
        error = _text_error(f"hypothesis.model.{key}", model.get(key))
        if error:
            errors.append(error)
    outputs = model.get("outputs")
    error = _sequence_error("hypothesis.model.outputs", outputs, "sequence")
    if error:
        errors.append(error)
    else:
        assert isinstance(outputs, Sequence)
        errors.extend(_output_errors(outputs))
    tests = model.get("tests")
    if tests is None:
        errors.append("hypothesis.model.tests: required mapping is missing")
    elif not isinstance(tests, Mapping):
        errors.append(
            f"hypothesis.model.tests: must be a mapping, got {type(tests).__name__}"
        )
    else:
        errors.extend(_tests_errors(tests))
    configuration = model.get("harnessConfiguration")
    if configuration is not None and not isinstance(configuration, Mapping):
        errors.append(
            f"hypothesis.model.harnessConfiguration: must be an object, "
            f"got {type(configuration).__name__}"
        )
    else:
        declared = configuration or {}
        for key in ("start", "end"):
            error = _text_error(
                f"hypothesis.model.harnessConfiguration.{key}", declared.get(key)
            )
            if error:
                errors.append(error)
        interval = declared.get("interval")
        error = _text_error("hypothesis.model.harnessConfiguration.interval", interval)
        if error:
            errors.append(error)
        elif str(interval).strip() not in BAR_INTERVALS:
            errors.append(
                "hypothesis.model.harnessConfiguration.interval: must be one "
                f"of {', '.join(BAR_INTERVALS)}, got {interval!r}"
            )
    indicators = model.get("indicators")
    if indicators is not None:
        indicator_error = _sequence_error(
            "hypothesis.model.indicators", indicators, "sequence"
        )
        if indicator_error:
            errors.append(indicator_error)
        else:
            assert isinstance(indicators, Sequence)
            errors.extend(_indicator_errors(indicators))
    return errors


def _indicator_errors(indicators: Sequence[object]) -> list[str]:
    errors: list[str] = []
    aliases: list[str] = []
    for index, item in enumerate(indicators):
        path = f"hypothesis.model.indicators[{index}]"
        if not isinstance(item, Mapping):
            errors.append(f"{path}: must be an object, got {type(item).__name__}")
            continue
        for key in ("alias", "name", "symbol"):
            error = _text_error(f"{path}.{key}", item.get(key))
            if error:
                errors.append(error)
        scope = item.get("scope")
        if scope is None:
            errors.append(f"{path}.scope: required non-empty text is missing")
        elif not isinstance(scope, str) or scope not in _SCOPES:
            errors.append(
                f"{path}.scope: must be one of {', '.join(_SCOPES)}, got {scope!r}"
            )
        parameters = item.get("parameters")
        if parameters is not None and not isinstance(parameters, Mapping):
            errors.append(
                f"{path}.parameters: must be an object, got {type(parameters).__name__}"
            )
        alias = item.get("alias")
        if isinstance(alias, str) and alias.strip():
            text = alias.strip()
            aliases.append(text)
            if _IDENTIFIER.fullmatch(text) is None:
                errors.append(
                    f"{path}.alias: must match the identifier grammar {alias!r}"
                )
        symbol = item.get("symbol")
        if (
            isinstance(symbol, str)
            and symbol.strip()
            and _IDENTIFIER.fullmatch(symbol.strip()) is None
        ):
            errors.append(
                f"{path}.symbol: must match the identifier grammar {symbol!r}"
            )
        name = item.get("name")
        if (
            isinstance(name, str)
            and name.strip()
            and _NAME.fullmatch(name.strip()) is None
        ):
            errors.append(
                f"{path}.name: must match the indicator-name grammar {name!r}"
            )
    if len(set(aliases)) != len(aliases):
        errors.append("hypothesis.model.indicators: aliases must be unique")
    return errors


def _universe_errors(universe: object) -> list[str]:
    if universe is None:
        return []
    error = _sequence_error("hypothesis.universe", universe, "sequence")
    if error:
        return [error]
    errors: list[str] = []
    assert isinstance(universe, Sequence)
    if not universe:
        errors.append("hypothesis.universe: must contain at least one symbol")
    for index, item in enumerate(universe):
        path = f"hypothesis.universe[{index}]"
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{path}: must be a non-empty symbol")
        elif not _IDENTIFIER.match(item.strip()):
            errors.append(f"{path}: must match the identifier grammar {item!r}")
    return errors


def _falsifier_errors(falsifiers: object) -> list[str]:
    if falsifiers is None:
        return ["hypothesis.falsifiers: required sequence is missing"]
    error = _sequence_error("hypothesis.falsifiers", falsifiers, "sequence")
    if error:
        return [error]
    assert isinstance(falsifiers, Sequence)
    if not falsifiers:
        # The deterministic successor derives one trial per falsifier and the
        # trials contract requires a non-empty trial list, so a hypothesis
        # without a falsifier can never be consumed: it is a contract problem
        # to repair here, before any transition commits.
        return ["hypothesis.falsifiers: must contain at least one falsifier"]
    errors: list[str] = []
    for index, item in enumerate(falsifiers):
        path = f"hypothesis.falsifiers[{index}]"
        if not isinstance(item, Mapping):
            errors.append(f"{path}: must be an object, got {type(item).__name__}")
            continue
        # The composer normalizes the camelCase id key before validation, so
        # the collector must accept exactly the same spelling.
        normalized = dict(item)
        if "falsifierId" in normalized and "falsifier_id" not in normalized:
            normalized["falsifier_id"] = normalized.pop("falsifierId")
        if not normalized.get("falsifier_id"):
            normalized["falsifier_id"] = f"f{index + 1}"
        try:
            HypothesisFalsifier.model_validate(normalized)
        except ValidationError as validation:
            for entry in validation.errors():
                field = ".".join(str(part) for part in entry["loc"])
                errors.append(f"{path}.{field}: {entry['msg']}")
    return errors


def hypothesis_payload_errors(payload: Mapping[str, object]) -> list[str]:
    """Return every deterministic shape violation in one hypothesis payload.

    The list is complete — every missing or wrongly-typed field is reported
    once, in declaration order — and deterministic: the same payload always
    yields the same lines, so a repair turn can be asserted exactly.
    """
    errors: list[str] = []
    for key in ("claim", "disagreement"):
        error = _text_error(
            f"hypothesis.{key}", payload.get(key), max_length=_MAX_HYPOTHESIS_TEXT
        )
        if error:
            errors.append(error)
    errors.extend(_model_errors(payload.get("model")))
    errors.extend(_universe_errors(payload.get("universe")))
    errors.extend(_falsifier_errors(payload.get("falsifiers")))
    return errors


def format_contract_errors(errors: Sequence[str]) -> str:
    """Format one collected error list as the rejection repair reason."""
    listed = "\n".join(f"- {error}" for error in errors)
    return (
        f"completion contract rejected with {len(errors)} problem(s) — fix "
        f"every problem and call request_phase_completion again:\n{listed}"
    )


__all__ = [
    "HYPOTHESIS_COMPLETION_SCHEMA",
    "HYPOTHESIS_PAYLOAD_SCHEMA",
    "format_contract_errors",
    "hypothesis_payload_errors",
]
