"""Request, hypothesis, plan, verdict, and strategy contracts.

Extracted from the original single-module hypothesis-development
implementation; behavior is unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType

from thesis.chat.contracts import WorkflowExecution
from thesis.chat.service import ChatService
from thesis.research.hypothesis import FALSIFIER_CONDITIONS, HypothesisFalsifier
from thesis.research.models import ResearchModel
from thesis.strategies.bundles import StrategyBundle

from .errors import (
    FalsificationComposeError,
    HypothesisDevelopmentIntakeError,
    WorkflowError,
)
from .primitives import (
    _DIGEST,
    _VERDICTS,
    VERDICT_REFUTED,
    _identifier,
    _iso,
    _text,
    _workspace_id,
)


@dataclass(frozen=True, slots=True)
class HypothesisDevelopmentRequest:
    """The chat request context that triggers one workflow execution."""

    workspace_id: str
    chat_id: str
    execution_id: str
    trigger_message_id: str
    requested_by: str
    ideas: str
    intent_epoch: int = 0
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        _workspace_id(self.workspace_id)
        for name in ("chat_id", "execution_id", "trigger_message_id", "requested_by"):
            _identifier(getattr(self, name), name)
        try:
            _text(self.ideas, "ideas", max_length=8_192)
        except WorkflowError as error:
            raise HypothesisDevelopmentIntakeError(str(error)) from error
        if self.intent_epoch < 0:
            raise HypothesisDevelopmentIntakeError("intent_epoch must not be negative")
        if self.created_at is not None:
            if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
                raise HypothesisDevelopmentIntakeError(
                    "created_at must be timezone-aware"
                )
            object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))

    def as_dict(self) -> dict[str, object]:
        """Return the detached request representation for transport."""
        return {
            "chatId": self.chat_id,
            "createdAt": None if self.created_at is None else _iso(self.created_at),
            "executionId": self.execution_id,
            "ideas": self.ideas,
            "intentEpoch": self.intent_epoch,
            "requestedBy": self.requested_by,
            "triggerMessageId": self.trigger_message_id,
            "workspaceId": self.workspace_id,
        }

    @classmethod
    def from_execution(
        cls, chat: ChatService, execution: WorkflowExecution
    ) -> HypothesisDevelopmentRequest:
        """Rebuild the runnable request for a durable execution.

        Used when an operator message resumes the hypothesis-creation
        conversation: the original idea is recovered from the trigger message
        and everything else from the execution record.
        """
        try:
            trigger = chat.get_message(
                execution.workspace_id, execution.trigger_message_id
            )
            ideas = _ideas_from_trigger(trigger.body)
        except (LookupError, ValueError):
            ideas = "Continue the hypothesis-creation conversation."
        return cls(
            workspace_id=execution.workspace_id,
            chat_id=execution.chat_id,
            execution_id=execution.execution_id,
            trigger_message_id=execution.trigger_message_id,
            requested_by=execution.requested_by,
            ideas=ideas,
            intent_epoch=execution.intent_epoch,
            created_at=execution.created_at,
        )


def _ideas_from_trigger(body: str) -> str:
    """Recover the operator's original idea from the workflow trigger message."""
    text = body.strip()
    if text.startswith("{"):
        try:
            document = json.loads(text)
        except json.JSONDecodeError:
            return text or "Continue the hypothesis-creation conversation."
        if isinstance(document, Mapping):
            ideas = document.get("ideas")
            if isinstance(ideas, str) and ideas.strip():
                return ideas.strip()
    return text or "Continue the hypothesis-creation conversation."


@dataclass(frozen=True, slots=True)
class StrategyContent:
    """Content-addressed strategy content authored after Layer A survives."""

    strategy_id: str
    version: str
    source: Mapping[str, str]
    tests: Mapping[str, str]
    entrypoint: str = "Strategy"
    harness_configuration: Mapping[str, object] = field(default_factory=dict)
    dependencies: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    harness_manifest_reference: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.strategy_id, "strategy_id")
        _text(self.version, "strategy version", max_length=64)
        if not isinstance(self.entrypoint, str) or not self.entrypoint.isidentifier():
            raise FalsificationComposeError(
                "strategy entrypoint must be a Python class identifier"
            )
        for name in ("source", "tests"):
            files = getattr(self, name)
            if not isinstance(files, Mapping) or not files:
                raise FalsificationComposeError(f"{name} must be a non-empty mapping")
            for path, content in files.items():
                if not isinstance(path, str) or not path.strip():
                    raise FalsificationComposeError(f"{name} paths must be non-empty")
                if not isinstance(content, str):
                    raise FalsificationComposeError(
                        f"{name} values must be path-to-text mappings"
                    )
        object.__setattr__(self, "source", MappingProxyType(dict(self.source)))
        object.__setattr__(self, "tests", MappingProxyType(dict(self.tests)))
        if not isinstance(self.harness_configuration, Mapping):
            raise FalsificationComposeError("harness_configuration must be a mapping")
        object.__setattr__(
            self,
            "harness_configuration",
            MappingProxyType(dict(self.harness_configuration)),
        )

    def bundle(self) -> StrategyBundle:
        """Return the sandbox-ready immutable bundle for this content."""
        return StrategyBundle.create(
            strategy_id=self.strategy_id,
            version=self.version,
            source=dict(self.source),
            tests=dict(self.tests),
            dependencies=self.dependencies,
            harness_configuration=dict(self.harness_configuration),
            limitations=self.limitations,
            harness_manifest_reference=self.harness_manifest_reference,
        )


@dataclass(frozen=True, slots=True)
class HypothesisDraft:
    """The falsifiable hypothesis and its separate statistical model."""

    claim: str
    disagreement: str
    risk_factor: str
    horizon: str
    universe: tuple[str, ...]
    falsifiers: tuple[HypothesisFalsifier, ...]
    model: ResearchModel
    sizing: str | None = None
    body: str = ""
    hypothesis_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.claim, "claim", max_length=8_192)
        _text(self.disagreement, "disagreement", max_length=8_192)
        _text(self.risk_factor, "risk_factor", max_length=4_096)
        _text(self.horizon, "horizon", max_length=4_096)
        if isinstance(self.universe, str) or not isinstance(self.universe, Sequence):
            raise FalsificationComposeError("universe must be a sequence")
        universe = tuple(_identifier(item, "universe") for item in self.universe)
        if not universe:
            raise FalsificationComposeError("universe must contain at least one symbol")
        object.__setattr__(self, "universe", universe)
        if any(not isinstance(item, HypothesisFalsifier) for item in self.falsifiers):
            raise FalsificationComposeError(
                "hypothesis draft requires HypothesisFalsifier values"
            )
        if not isinstance(self.model, ResearchModel):
            raise FalsificationComposeError(
                "hypothesis draft requires a research model"
            )
        if self.sizing is not None:
            _text(self.sizing, "sizing", max_length=4_096)
        if self.hypothesis_id is not None:
            _identifier(self.hypothesis_id, "hypothesis_id")


@dataclass(frozen=True, slots=True)
class PlannedTest:
    """One planned falsifier test before it is bound to a declared falsifier."""

    test_id: str
    falsifier_id: str
    description: str

    def __post_init__(self) -> None:
        _identifier(self.test_id, "test_id")
        _identifier(self.falsifier_id, "falsifier_id")
        _text(self.description, "test description", max_length=2_048)


@dataclass(frozen=True, slots=True)
class PlaceboControl:
    """One negative/placebo control that should not show the claimed edge."""

    control_id: str
    description: str
    expectation: str

    def __post_init__(self) -> None:
        _identifier(self.control_id, "control_id")
        _text(self.description, "control description", max_length=2_048)
        _text(self.expectation, "control expectation", max_length=2_048)


@dataclass(frozen=True, slots=True)
class FalsificationPlanDraft:
    """The model-authored plan before falsifier binding."""

    tests: tuple[PlannedTest, ...]
    controls: tuple[PlaceboControl, ...]


@dataclass(frozen=True, slots=True)
class FalsifierTest:
    """One planned falsifier test bound to its observable and threshold."""

    test_id: str
    falsifier_id: str
    observable: str
    condition: str
    threshold: float
    description: str

    def __post_init__(self) -> None:
        _identifier(self.test_id, "test_id")
        _identifier(self.falsifier_id, "falsifier_id")
        _identifier(self.observable, "observable")
        if self.condition not in FALSIFIER_CONDITIONS:
            raise WorkflowError(
                f"falsifier condition must be one of {sorted(FALSIFIER_CONDITIONS)}"
            )
        if isinstance(self.threshold, bool) or not isinstance(
            self.threshold, int | float
        ):
            raise WorkflowError("falsifier threshold must be a finite number")
        object.__setattr__(self, "threshold", float(self.threshold))
        _text(self.description, "test description", max_length=2_048)


@dataclass(frozen=True, slots=True)
class FalsificationPlan:
    """The concrete falsification plan: tests plus negative controls."""

    tests: tuple[FalsifierTest, ...]
    controls: tuple[PlaceboControl, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.tests, tuple) or not isinstance(self.controls, tuple):
            raise WorkflowError("falsification plan tests and controls must be tuples")


@dataclass(frozen=True, slots=True)
class FalsificationTrial:
    """One recorded falsifier trial with bound run evidence."""

    test_id: str
    falsifier_id: str
    run_id: str
    digest: str
    run_state: str
    observed: float | None
    falsified: bool
    detail: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.test_id, "test_id")
        _identifier(self.falsifier_id, "falsifier_id")
        _text(self.run_id, "run_id", max_length=256)
        if not isinstance(self.digest, str) or _DIGEST.fullmatch(self.digest) is None:
            raise WorkflowError("trial digest must be a lowercase SHA-256 digest")
        _text(self.run_state, "run_state", max_length=64)
        if self.observed is not None and (
            isinstance(self.observed, bool)
            or not isinstance(self.observed, int | float)
        ):
            raise WorkflowError("trial observation must be numeric when present")
        if self.detail is not None:
            _text(self.detail, "trial detail", max_length=4_096)

    def as_dict(self) -> dict[str, object]:
        """Return the portable trial representation."""
        return {
            "detail": self.detail,
            "digest": self.digest,
            "falsified": self.falsified,
            "falsifierId": self.falsifier_id,
            "observed": self.observed,
            "runId": self.run_id,
            "runState": self.run_state,
            "testId": self.test_id,
        }


@dataclass(frozen=True, slots=True)
class RcaDiagnosis:
    """One fresh-subagent root-cause diagnosis of a failed trial."""

    falsifier_id: str
    failure_class: str
    rationale: str
    confidence: float
    follow_ups: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.falsifier_id, "falsifier_id")
        _text(self.failure_class, "failure_class", max_length=64)
        _text(self.rationale, "rca rationale", max_length=4_096)
        if not 0.0 <= self.confidence <= 1.0:
            raise WorkflowError("rca confidence must fall within [0, 1]")

    def as_dict(self) -> dict[str, object]:
        """Return the portable diagnosis representation."""
        return {
            "confidence": self.confidence,
            "failureClass": self.failure_class,
            "falsifierId": self.falsifier_id,
            "followUps": list(self.follow_ups),
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class ExitCriterion:
    """A strategy exit phrased as a falsifier re-check."""

    observable: str
    condition: str
    threshold: float
    description: str
    falsifier_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.observable, "exit observable")
        if self.condition not in FALSIFIER_CONDITIONS:
            raise WorkflowError(
                f"exit condition must be one of {sorted(FALSIFIER_CONDITIONS)}"
            )
        if isinstance(self.threshold, bool) or not isinstance(
            self.threshold, int | float
        ):
            raise WorkflowError("exit threshold must be a finite number")
        object.__setattr__(self, "threshold", float(self.threshold))
        _text(self.description, "exit description", max_length=2_048)
        if self.falsifier_id is not None:
            _identifier(self.falsifier_id, "exit falsifier_id")

    def as_dict(self) -> dict[str, object]:
        """Return the portable exit-criterion representation."""
        return {
            "condition": self.condition,
            "description": self.description,
            "falsifierId": self.falsifier_id,
            "observable": self.observable,
            "threshold": self.threshold,
        }


@dataclass(frozen=True, slots=True)
class StrategyDesignDraft:
    """The model-authored strategy with falsifier-recheck exit criteria."""

    content: StrategyContent
    exit_criteria: tuple[ExitCriterion, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.content, StrategyContent):
            raise FalsificationComposeError("strategy design requires content")
        if not isinstance(self.exit_criteria, tuple):
            raise FalsificationComposeError(
                "strategy design exit criteria must be a tuple"
            )


@dataclass(frozen=True, slots=True)
class FalsificationVerdict:
    """The conservative falsification verdict for one hypothesis."""

    outcome: str
    reason: str
    trials: tuple[FalsificationTrial, ...] = ()

    def __post_init__(self) -> None:
        if self.outcome not in _VERDICTS:
            raise WorkflowError(f"verdict outcome must be one of {sorted(_VERDICTS)}")
        _text(self.reason, "verdict reason", max_length=2_048)

    @property
    def falsified(self) -> bool:
        """Return whether the verdict refutes the hypothesis."""
        return self.outcome == VERDICT_REFUTED

    def as_dict(self) -> dict[str, object]:
        """Return the portable verdict representation."""
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "trials": [trial.as_dict() for trial in self.trials],
        }


# -- gate checks ------------------------------------------------------------
