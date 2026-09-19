"""Content-addressed strategy content and its falsifier-bound exits."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from thesis.research.hypothesis import FALSIFIER_CONDITIONS
from thesis.strategies.bundles import StrategyBundle

from ..errors import (
    FalsificationComposeError,
    WorkflowError,
)
from ..primitives import (
    _identifier,
    _text,
)


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
