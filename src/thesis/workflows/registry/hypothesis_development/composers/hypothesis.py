"""Compose helpers for the canonical JSON hypothesis envelope.

The compose *helpers* here are the live extraction surface: the deterministic
trials phase and the rebuild fold turn an accepted hypothesis contract's
payload into the typed draft through them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from thesis.research.hypothesis import HypothesisFalsifier
from thesis.research.models import ResearchModel, ResearchModelError
from thesis.workflows.research_models import study_window

from ..errors import (
    FalsificationComposeError,
)
from ..primitives import _DEFAULT_UNIVERSE
from ._shared import (
    _model_text,
    _optional_compose_text,
)

__all__ = [
    "_compose_falsifiers",
    "_compose_research_model",
    "_compose_universe",
    "_optional_compose_text",
]


def _compose_research_model(value: object) -> ResearchModel:
    if not isinstance(value, Mapping):
        raise FalsificationComposeError("hypothesis requires a research model object")
    if "indicators" in value:
        raise FalsificationComposeError(
            "model indicators are not accepted: authored Rust programs embed their own"
        )
    outputs = value.get("outputs")
    tests = value.get("tests")
    configuration = value.get("harnessConfiguration", {})
    if isinstance(outputs, str) or not isinstance(outputs, Sequence):
        raise FalsificationComposeError("model outputs must be a sequence")
    if not isinstance(tests, Mapping):
        raise FalsificationComposeError("model tests must be a mapping")
    if not isinstance(configuration, Mapping):
        raise FalsificationComposeError("model harnessConfiguration must be an object")
    try:
        # The fetch window is the model's to declare; validating it here keeps
        # the composer accepting exactly what the contract collector accepts.
        # Stored executions created before the bar-interval migration carry no
        # ``interval`` in their harnessConfiguration and will not re-compose:
        # ResearchModel requires it and no fallback is provided.
        study_window(configuration)
        return ResearchModel(
            model_id=_model_text(value.get("modelId"), "modelId"),
            version=_model_text(value.get("version"), "model version"),
            description=_model_text(value.get("description"), "model description"),
            source=_model_text(value.get("source"), "model source"),
            outputs=tuple(outputs),
            tests=dict(tests),
            harness_configuration=dict(configuration),
        )
    except (ResearchModelError, TypeError) as error:
        raise FalsificationComposeError(str(error)) from error


def _compose_universe(value: object, model: ResearchModel) -> tuple[str, ...]:
    """Return the declared universe, inferring it from the model when absent."""
    if value is not None:
        return _compose_symbols(value, "universe")
    symbols = model.harness_configuration.get("symbols")
    data = model.harness_configuration.get("data")
    if not isinstance(symbols, Sequence) or isinstance(symbols, str):
        symbols = data.get("symbols") if isinstance(data, Mapping) else None
    if isinstance(symbols, Sequence) and not isinstance(symbols, str):
        inferred = [item.strip() for item in symbols if isinstance(item, str)]
        inferred = [item for item in inferred if item]
        if inferred:
            return tuple(dict.fromkeys(inferred))
    return _DEFAULT_UNIVERSE


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
