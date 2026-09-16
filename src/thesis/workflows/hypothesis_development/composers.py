"""JSON composers that turn model output into contracts.

Extracted from the original single-module hypothesis-development
implementation; behavior is unchanged.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence

from thesis.agent.router import InvocationResult
from thesis.research.hypothesis import HypothesisFalsifier
from thesis.research.indicators import (
    IndicatorArtifactError,
    IndicatorScope,
    WorkspaceIndicator,
)
from thesis.research.models import IndicatorBinding, ResearchModel, ResearchModelError
from thesis.strategies.bundles import StrategyBundleError

from .contracts import (
    ExitCriterion,
    FalsificationPlanDraft,
    HypothesisDraft,
    PlaceboControl,
    PlannedTest,
    RcaDiagnosis,
    StrategyContent,
    StrategyDesignDraft,
)
from .errors import CreationTurn, FalsificationComposeError, WorkflowError
from .primitives import _DEFAULT_HORIZON, _DEFAULT_RISK_FACTOR, _DEFAULT_UNIVERSE, _text


def _balanced_objects(text: str) -> Iterator[str]:
    """Yield every balanced top-level ``{...}`` slice, in source order.

    The scan respects JSON string quoting and escapes so braces inside string
    literals never affect the nesting depth.
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    yield text[start : index + 1]
                    break
        start = text.find("{", start + 1)


_FENCED_JSON = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _extract_json_document(output: str) -> object | None:
    """Best-effort extraction of a JSON document from tolerant model output.

    Tried in order: the raw output as-is; every fenced code block (``json`` or
    bare); then each balanced top-level object.  Returns ``None`` when no
    candidate parses as JSON.
    """
    candidates: list[str] = [output]
    candidates.extend(match.group(1) for match in _FENCED_JSON.finditer(output))
    candidates.extend(_balanced_objects(output))
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _json_document(invocation: InvocationResult, label: str) -> dict[str, object]:
    output = invocation.response.output
    if not isinstance(output, str) or not output.strip():
        raise FalsificationComposeError(f"model provider returned no {label} content")
    document = _extract_json_document(output)
    if document is None:
        raise FalsificationComposeError(
            f"model output is not valid JSON {label} content"
        )
    if not isinstance(document, dict):
        raise FalsificationComposeError(f"{label} content must be a JSON object")
    return document


def _compose_text(document: Mapping[str, object], key: str, label: str) -> str:
    try:
        return _text(document.get(key), label, max_length=8_192)
    except WorkflowError as error:
        raise FalsificationComposeError(str(error)) from error


def _optional_compose_text(
    document: Mapping[str, object],
    key: str,
    default: str,
    *,
    max_length: int,
) -> str:
    """Return a trimmed optional field or ``default`` when it is absent/blank."""
    value = document.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if isinstance(value, str) and len(value) > max_length:
        value = value[:max_length].strip()
    try:
        return _text(value, key, max_length=max_length)
    except WorkflowError as error:
        raise FalsificationComposeError(str(error)) from error


class JsonCreationTurnComposer:
    """Parse the model's conversational message and advancement judgment."""

    def compose(self, invocation: InvocationResult) -> CreationTurn:
        document = _json_document(invocation, "creation turn")
        message = _compose_text(document, "message", "message")
        advance = document.get("advance")
        if not isinstance(advance, bool):
            raise FalsificationComposeError("creation turn advance must be boolean")
        return CreationTurn(message=message, advance=advance)


class JsonDevelopmentHypothesisComposer:
    """Parse the canonical JSON hypothesis envelope from model output.

    Composition is deliberately lenient: only ``claim`` and ``disagreement``
    plus the falsifier core fields and a research ``model`` are required.
    Prose metadata remains tolerant, but Layer A cannot silently substitute a
    placeholder strategy for a missing statistical model.
    """

    def compose(self, invocation: InvocationResult) -> HypothesisDraft:
        document = _json_document(invocation, "hypothesis")
        claim = _compose_text(document, "claim", "claim")
        disagreement = _compose_text(document, "disagreement", "disagreement")
        model = _compose_research_model(document.get("model"))
        universe = _compose_universe(document.get("universe"), model)
        falsifiers = _compose_falsifiers(document.get("falsifiers"))
        sizing = document.get("sizing")
        if not (isinstance(sizing, str) and sizing.strip()):
            sizing = None
        body = document.get("body", "")
        if body is None:
            body = ""
        if not isinstance(body, str):
            raise FalsificationComposeError("hypothesis body must be text")
        hypothesis_id = document.get("hypothesisId")
        if hypothesis_id is not None and not (
            isinstance(hypothesis_id, str) and hypothesis_id.strip()
        ):
            hypothesis_id = None
        try:
            return HypothesisDraft(
                claim=claim,
                disagreement=disagreement,
                risk_factor=_optional_compose_text(
                    document, "riskFactor", _DEFAULT_RISK_FACTOR, max_length=4_096
                ),
                horizon=_optional_compose_text(
                    document, "horizon", _DEFAULT_HORIZON, max_length=4_096
                ),
                universe=universe,
                falsifiers=falsifiers,
                model=model,
                sizing=None if sizing is None else sizing.strip(),
                body=body,
                hypothesis_id=None if hypothesis_id is None else hypothesis_id.strip(),
            )
        except WorkflowError as error:
            raise FalsificationComposeError(str(error)) from error


def _compose_research_model(value: object) -> ResearchModel:
    if not isinstance(value, Mapping):
        raise FalsificationComposeError("hypothesis requires a research model object")
    indicators = value.get("indicators", [])
    outputs = value.get("outputs")
    tests = value.get("tests")
    configuration = value.get("harnessConfiguration", {})
    if isinstance(indicators, str) or not isinstance(indicators, Sequence):
        raise FalsificationComposeError("model indicators must be a sequence")
    if isinstance(outputs, str) or not isinstance(outputs, Sequence):
        raise FalsificationComposeError("model outputs must be a sequence")
    if not isinstance(tests, Mapping):
        raise FalsificationComposeError("model tests must be a mapping")
    if not isinstance(configuration, Mapping):
        raise FalsificationComposeError("model harnessConfiguration must be an object")
    try:
        return ResearchModel(
            model_id=_model_text(value.get("modelId"), "modelId"),
            version=_model_text(value.get("version"), "model version"),
            description=_model_text(value.get("description"), "model description"),
            source=_model_text(value.get("source"), "model source"),
            fit_entrypoint=_model_text(
                value.get("fitEntrypoint", "fit"), "model fit entrypoint"
            ),
            observe_entrypoint=_model_text(
                value.get("observeEntrypoint", "observe"),
                "model observe entrypoint",
            ),
            outputs=tuple(outputs),
            indicators=tuple(_compose_indicator_binding(item) for item in indicators),
            tests=dict(tests),
            harness_configuration=dict(configuration),
        )
    except (IndicatorArtifactError, ResearchModelError, TypeError) as error:
        raise FalsificationComposeError(str(error)) from error


def _compose_indicator_binding(value: object) -> IndicatorBinding:
    if not isinstance(value, Mapping):
        raise FalsificationComposeError("each model indicator must be an object")
    try:
        scope = IndicatorScope(value.get("scope"))
    except ValueError as error:
        raise FalsificationComposeError(
            "model indicator scope must be global or workspace"
        ) from error
    parameters = value.get("parameters", {})
    if not isinstance(parameters, Mapping):
        raise FalsificationComposeError("model indicator parameters must be an object")
    definition_value = value.get("definition")
    definition: WorkspaceIndicator | None = None
    if definition_value is not None:
        if not isinstance(definition_value, Mapping):
            raise FalsificationComposeError(
                "workspace indicator definition must be an object"
            )
        tests = definition_value.get("tests", {})
        schema = definition_value.get("parameterSchema", {})
        if not isinstance(tests, Mapping) or not isinstance(schema, Mapping):
            raise FalsificationComposeError(
                "workspace indicator tests and parameterSchema must be objects"
            )
        try:
            definition = WorkspaceIndicator(
                name=definition_value.get("name", value.get("name")),
                version=definition_value.get("version"),
                description=definition_value.get("description"),
                source=definition_value.get("source"),
                entrypoint=definition_value.get("entrypoint", "calculate"),
                parameter_schema=dict(schema),
                tests=dict(tests),
            )
        except (IndicatorArtifactError, TypeError) as error:
            raise FalsificationComposeError(str(error)) from error
    try:
        return IndicatorBinding(
            alias=_model_text(value.get("alias"), "indicator alias"),
            scope=scope,
            name=_model_text(value.get("name"), "indicator name"),
            symbol=_model_text(value.get("symbol"), "indicator symbol"),
            parameters=dict(parameters),
            content_sha256=value.get("contentSha256"),
            definition=definition,
        )
    except (IndicatorArtifactError, ResearchModelError, TypeError) as error:
        raise FalsificationComposeError(str(error)) from error


def _model_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FalsificationComposeError(f"{label} must be non-empty text")
    return value.strip()


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
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise FalsificationComposeError("falsifiers must be a sequence")
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


class JsonFalsificationPlanner:
    """Parse the canonical JSON falsification-plan envelope."""

    def compose(self, invocation: InvocationResult) -> FalsificationPlanDraft:
        document = _json_document(invocation, "falsification plan")
        tests = _compose_tests(document.get("tests"))
        controls = _compose_controls(document.get("controls"))
        return FalsificationPlanDraft(tests=tests, controls=controls)


def _compose_tests(value: object) -> tuple[PlannedTest, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise FalsificationComposeError("plan tests must be a sequence")
    tests: list[PlannedTest] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise FalsificationComposeError("each plan test must be a JSON object")
        try:
            tests.append(
                PlannedTest(
                    test_id=item.get("testId"),
                    falsifier_id=item.get("falsifierId"),
                    description=item.get("description"),
                )
            )
        except WorkflowError as error:
            raise FalsificationComposeError(str(error)) from error
    return tuple(tests)


def _compose_controls(value: object) -> tuple[PlaceboControl, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise FalsificationComposeError("plan controls must be a sequence")
    controls: list[PlaceboControl] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise FalsificationComposeError("each control must be a JSON object")
        try:
            controls.append(
                PlaceboControl(
                    control_id=item.get("controlId"),
                    description=item.get("description"),
                    expectation=item.get("expectation"),
                )
            )
        except WorkflowError as error:
            raise FalsificationComposeError(str(error)) from error
    return tuple(controls)


class JsonRcaComposer:
    """Parse the canonical JSON RCA-diagnosis envelope."""

    def compose(self, invocation: InvocationResult) -> RcaDiagnosis:
        document = _json_document(invocation, "rca")
        confidence = document.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, int | float):
            raise FalsificationComposeError("rca confidence must be numeric")
        follow_ups = document.get("followUps", [])
        if isinstance(follow_ups, str) or not isinstance(follow_ups, Sequence):
            raise FalsificationComposeError("rca followUps must be a sequence")
        for item in follow_ups:
            if not isinstance(item, str) or not item.strip():
                raise FalsificationComposeError(
                    "rca followUps must contain non-empty strings"
                )
        raw_class = document.get("failureClass")
        if isinstance(raw_class, str):
            lowered = raw_class.lower()
            if "harness" in lowered:
                failure_class = "harness"
            elif "data" in lowered:
                failure_class = "data"
            elif (
                "implementation" in lowered
                or "syntax" in lowered
                or "import" in lowered
            ):
                failure_class = "implementation"
            elif "market" in lowered or "regime" in lowered:
                failure_class = "market_regime"
            elif "strategy" in lowered:
                failure_class = "strategy"
            else:
                failure_class = _compose_text(document, "failureClass", "failureClass")[
                    :64
                ]
        else:
            failure_class = _compose_text(document, "failureClass", "failureClass")[:64]
        try:
            return RcaDiagnosis(
                falsifier_id=_compose_text(document, "falsifierId", "falsifierId"),
                failure_class=failure_class,
                rationale=_compose_text(document, "rationale", "rationale"),
                confidence=float(confidence),
                follow_ups=tuple(item.strip() for item in follow_ups),
            )
        except WorkflowError as error:
            raise FalsificationComposeError(str(error)) from error


class JsonStrategyDesigner:
    """Parse the canonical JSON strategy-design envelope."""

    def compose(self, invocation: InvocationResult) -> StrategyDesignDraft:
        document = _json_document(invocation, "strategy design")
        content = _compose_strategy(document, "strategy")
        criteria = _compose_exit_criteria(document.get("exitCriteria"))
        return StrategyDesignDraft(content=content, exit_criteria=criteria)


def _compose_strategy(value: object, label: str) -> StrategyContent:
    if not isinstance(value, Mapping):
        raise FalsificationComposeError(f"{label} must be a JSON object")
    files = value.get("files")
    tests = value.get("tests")
    strategy_id = value.get("strategyId")
    version = value.get("version")
    if not isinstance(files, Mapping) or not files:
        raise FalsificationComposeError(f"{label} requires source files")
    if not isinstance(tests, Mapping) or not tests:
        raise FalsificationComposeError(f"{label} requires tests")
    if not isinstance(strategy_id, str) or not strategy_id.strip():
        raise FalsificationComposeError(f"{label} requires strategyId")
    if not isinstance(version, str) or not version.strip():
        raise FalsificationComposeError(f"{label} requires version")
    dependencies = value.get("dependencies", [])
    limitations = value.get("limitations", [])
    for sequence, name in (
        (dependencies, "dependencies"),
        (limitations, "limitations"),
    ):
        if isinstance(sequence, str) or not isinstance(sequence, Sequence):
            raise FalsificationComposeError(f"{label} {name} must be a sequence")
        for item in sequence:
            if not isinstance(item, str) or not item.strip():
                raise FalsificationComposeError(
                    f"{label} {name} must contain non-empty strings"
                )
    harness_configuration = value.get("harnessConfiguration", {})
    if not isinstance(harness_configuration, Mapping):
        raise FalsificationComposeError(
            f"{label} harnessConfiguration must be a JSON object"
        )
    try:
        return StrategyContent(
            strategy_id=strategy_id,
            version=version,
            source=dict(files),
            tests=dict(tests),
            entrypoint=_model_text(
                value.get("entrypoint", value.get("className", "Strategy")),
                f"{label} entrypoint",
            ),
            harness_configuration=dict(harness_configuration),
            dependencies=tuple(dict.fromkeys(item.strip() for item in dependencies)),
            limitations=tuple(dict.fromkeys(item.strip() for item in limitations)),
        )
    except (WorkflowError, StrategyBundleError) as error:
        raise FalsificationComposeError(str(error)) from error


def _compose_exit_criteria(value: object) -> tuple[ExitCriterion, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise FalsificationComposeError("exitCriteria must be a sequence")
    criteria: list[ExitCriterion] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise FalsificationComposeError("each exit criterion must be an object")
        try:
            criteria.append(
                ExitCriterion(
                    observable=item.get("observable"),
                    condition=item.get("condition"),
                    threshold=item.get("threshold"),
                    description=item.get("description"),
                    falsifier_id=item.get("falsifierId"),
                )
            )
        except WorkflowError as error:
            raise FalsificationComposeError(str(error)) from error
    return tuple(criteria)


# -- workflow ---------------------------------------------------------------
