"""Adapt an injected strategy authoring service to trusted session tools.

Composition decides which executor receives this capability. The adapter never
resolves a workspace path or accepts model-supplied execution attribution.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, cast

from thesis.agent.runtime.context import ToolContext
from thesis.agent.runtime.schema import render_violations, validate_arguments
from thesis.agent.runtime.session import RegisteredTool, ToolExecution
from thesis.strategies.contracts import STRATEGY_DESIGN_SCHEMA

TOOL_AUTHOR_STRATEGY = "author_strategy"


class StrategyAuthor(Protocol):
    """The callable authoring boundary supplied by composition."""

    def __call__(
        self,
        *,
        workspace_id: str,
        execution_id: str,
        strategy: Mapping[str, object],
        name: str,
        symbol: str,
        description: str,
    ) -> dict[str, object]:
        """Validate and persist a design, returning its receipt or diagnostics."""


class StrategyAuthoringService(Protocol):
    """A service exposing the same authoring boundary as a callable."""

    def author(
        self,
        *,
        workspace_id: str,
        execution_id: str,
        strategy: Mapping[str, object],
        name: str,
        symbol: str,
        description: str,
    ) -> dict[str, object]:
        """Validate and persist a design, returning its receipt or diagnostics."""


_TEXT: dict[str, object] = {"type": "string", "minLength": 1, "pattern": r"\S"}

_RECEIPT_PROPERTIES: dict[str, object] = {
    "artifactPath": {
        **_TEXT,
        "pattern": r"^strategies/(?!\.{1,2}/)[^/\\:]+/(?!\.{1,2}$)[^/\\:]+$",
    },
    "bundleSha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
    "strategyId": {
        "type": "string",
        "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$",
    },
    "version": {
        "type": "string",
        "pattern": r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$",
    },
}

STRATEGY_RECEIPT_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["artifactPath", "bundleSha256", "strategyId", "version"],
    "properties": _RECEIPT_PROPERTIES,
}

AUTHOR_STRATEGY_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["strategy", "name", "symbol", "description"],
    "properties": {
        "name": _TEXT,
        "symbol": _TEXT,
        "description": _TEXT,
        "strategy": STRATEGY_DESIGN_SCHEMA,
    },
}


def strategy_registered_tools(
    authoring: StrategyAuthor | StrategyAuthoringService,
    context: ToolContext,
) -> tuple[RegisteredTool, ...]:
    """Bind the injected authoring capability to runtime-owned attribution."""
    author = cast(StrategyAuthor, getattr(authoring, "author", authoring))

    def handle(arguments: Mapping[str, object]) -> ToolExecution:
        if not context.workflow_execution_id:
            return ToolExecution(
                ok=False,
                failure={
                    "kind": "tool",
                    "message": "strategy authoring requires an execution",
                },
            )
        result = author(
            workspace_id=context.workspace_id,
            execution_id=context.workflow_execution_id,
            strategy=cast(Mapping[str, object], arguments["strategy"]),
            name=cast(str, arguments["name"]),
            symbol=cast(str, arguments["symbol"]),
            description=cast(str, arguments["description"]),
        )
        validation = result.get("validation")
        if isinstance(validation, Mapping) and validation.get("accepted") is False:
            return ToolExecution(
                ok=False,
                structured=result,
                failure={
                    "kind": "validation",
                    "message": "strategy validation rejected",
                },
            )
        misses = validate_arguments(STRATEGY_RECEIPT_SCHEMA, result)
        if misses:
            return ToolExecution(
                ok=False,
                structured=result,
                failure={
                    "kind": "tool",
                    "message": "authoring service returned an invalid receipt: "
                    + render_violations(misses),
                },
            )
        return ToolExecution(ok=True, structured=result)

    return (
        RegisteredTool(
            spec={
                "type": "function",
                "function": {
                    "name": TOOL_AUTHOR_STRATEGY,
                    "description": "Validate and persist the operator-accepted Rust "
                    "strategy. Return the persisted strategy receipt for phase completion. "
                    "Workspace and execution are supplied by the runtime.",
                    "parameters": AUTHOR_STRATEGY_SCHEMA,
                },
            },
            handler=handle,
        ),
    )


__all__ = [
    "AUTHOR_STRATEGY_SCHEMA",
    "STRATEGY_RECEIPT_SCHEMA",
    "TOOL_AUTHOR_STRATEGY",
    "StrategyAuthor",
    "StrategyAuthoringService",
    "strategy_registered_tools",
]
