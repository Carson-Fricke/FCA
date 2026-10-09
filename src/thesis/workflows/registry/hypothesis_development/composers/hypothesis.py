"""Compose helpers for the canonical JSON hypothesis envelope.

The compose *helpers* here are the live extraction surface: the deterministic
trials phase and the rebuild fold turn an accepted hypothesis contract's
payload into the typed draft through them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from thesis.agent.runtime.schema import render_violations, violations
from thesis.research.hypothesis import HypothesisFalsifier
from thesis.workflows.research_models import StudyProgramReference

from ..contracts.validation import HYPOTHESIS_PAYLOAD_SCHEMA
from ..errors import (
    FalsificationComposeError,
)
from ..primitives import _DEFAULT_UNIVERSE
from ._shared import (
    _optional_compose_text,
)

__all__ = [
    "_compose_falsifiers",
    "_compose_research_model",
    "_compose_universe",
    "_optional_compose_text",
]


def _compose_research_model(value: object) -> StudyProgramReference:
    if not isinstance(value, Mapping):
        raise FalsificationComposeError("hypothesis requires a research model object")
    misses = violations(
        HYPOTHESIS_PAYLOAD_SCHEMA["properties"]["model"], value, "hypothesis.model"
    )
    if misses:
        raise FalsificationComposeError(render_violations(misses))
    return StudyProgramReference.from_document(value)


def _compose_universe(value: object) -> tuple[str, ...]:
    """Return the declared universe for the committed study reference."""
    return (
        _compose_symbols(value, "universe") if value is not None else _DEFAULT_UNIVERSE
    )


def _compose_symbols(value: object, label: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise FalsificationComposeError(f"{label} must be a sequence of symbols")
    symbols = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise FalsificationComposeError(f"{label} must contain non-empty symbols")
        symbols.append(item.strip())
    if not symbols:
        raise FalsificationComposeError(f"{label} must contain at least one symbol")
    return tuple(dict.fromkeys(symbols))


def _compose_falsifiers(value: object) -> tuple[HypothesisFalsifier, ...]:
    if value is None:
        # The trials phase derives one trial per falsifier and its contract
        # requires a non-empty trial list: a hypothesis without falsifiers can
        # never be consumed, so it is rejected before any transition commits.
        raise FalsificationComposeError("hypothesis requires at least one falsifier")
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise FalsificationComposeError("falsifiers must be a sequence")
    if not value:
        raise FalsificationComposeError("hypothesis requires at least one falsifier")
    falsifiers: list[HypothesisFalsifier] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise FalsificationComposeError("each falsifier must be a JSON object")
        normalized = dict(item)
        if "falsifierId" in normalized and "falsifier_id" not in normalized:
            normalized["falsifier_id"] = normalized.pop("falsifierId")
        if not normalized.get("falsifier_id"):
            normalized["falsifier_id"] = f"f{index + 1}"
        try:
            falsifiers.append(HypothesisFalsifier.model_validate(normalized))
        except ValueError as error:
            raise FalsificationComposeError(f"invalid falsifier: {error}") from error
    return tuple(falsifiers)
