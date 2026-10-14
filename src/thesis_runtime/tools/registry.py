"""Provider-neutral tool descriptors, explicit trusted invocation, and dispatch policy."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import CancelledError
from dataclasses import dataclass, field, replace

from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.contracts.datum import Datum, ToolOperation
from thesis_runtime.storage.ownership import assert_owned
from thesis_runtime.tools.schema import (
    check_schema,
    render_violations,
    validate_arguments,
)


class SessionPersistenceError(RuntimeError):
    """A durable activity write failed inside an otherwise recoverable tool."""


@dataclass(frozen=True, slots=True)
class ToolExecution:
    """The typed outcome of one dispatched tool call.

    ``datums`` carries the typed values the tool returned.  When it is empty the
    session derives one generic datum from ``structured``/``text``/``failure``,
    typed by the tool name, so every tool result is renderable.
    """

    ok: bool
    structured: Mapping[str, object] = field(default_factory=dict)
    text: str = ""
    failure: Mapping[str, object] | None = None
    terminal: bool = False
    datums: tuple[Datum, ...] = ()

    def as_content(self) -> str:
        payload: dict[str, object] = {
            "ok": self.ok,
            "structured": dict(self.structured),
            "text": self.text,
        }
        if self.failure is not None:
            payload["failure"] = dict(self.failure)
        return json.dumps(
            payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False
        )


ToolHandler = Callable[[Mapping[str, object], ToolContext], ToolExecution]


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """One tool advertised to the model and its local handler."""

    descriptor: Mapping[str, object]
    handler: ToolHandler
    operation: ToolOperation = "execute"

    @property
    def name(self) -> str:
        name = self.descriptor.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("registered tool descriptor is missing a name")
        return name

    @property
    def input_schema(self) -> Mapping[str, object]:
        parameters = self.descriptor.get("parameters", {})
        if not isinstance(parameters, Mapping):
            raise ValueError("tool parameters must be a JSON Schema object")
        return parameters


class SessionToolRegistry:
    """A default-deny catalog of tools available to one session.

    ``policy`` is the effective allowed-tool policy for the calling sub-agent.
    When supplied, only permitted tools are advertised and a request for any
    other tool is rejected before its handler runs; the registry still knows the
    full catalog so the same instance can serve several policies.
    """

    def __init__(
        self,
        tools: Sequence[RegisteredTool] = (),
        *,
        policy: Sequence[str] | None = None,
    ) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        for tool in tools:
            self.register(tool)
        self._policy: frozenset[str] | None = (
            None if policy is None else frozenset(policy)
        )

    @property
    def policy(self) -> tuple[str, ...] | None:
        """Return the effective allowed-tool policy, or ``None`` for all."""
        if self._policy is None:
            return None
        return tuple(sorted(self._policy))

    def register(self, tool: RegisteredTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate session tool: {tool.name}")
        check_schema(tool.input_schema)
        # Detach advertised contracts from caller-owned nested containers.
        self._tools[tool.name] = replace(
            tool, descriptor=json.loads(json.dumps(tool.descriptor, allow_nan=False))
        )

    def permits(self, name: str) -> bool:
        """Return whether the effective policy allows ``name``."""
        return self._policy is None or name in self._policy

    def catalog(self) -> tuple[Mapping[str, object], ...]:
        return tuple(
            json.loads(json.dumps(tool.descriptor))
            for tool in self._tools.values()
            if self.permits(tool.name)
        )

    def operation(self, name: str) -> ToolOperation | None:
        """Return trusted operation semantics, independently of returned data."""
        tool = self._tools.get(name)
        return tool.operation if tool is not None else None

    def dispatch(
        self, name: str, arguments: Mapping[str, object], *, context: ToolContext
    ) -> ToolExecution:
        assert_owned()
        tool = self._tools.get(name)
        if tool is None:
            return ToolExecution(
                ok=False,
                failure={
                    "kind": "unknown_tool",
                    "message": f"unknown tool: {name}",
                    "certainty": "not_started",
                    "retry_safe": True,
                },
            )
        if not self.permits(name) or not context.permits(name):
            return ToolExecution(
                ok=False,
                failure={
                    "kind": "denied",
                    "message": f"tool is not permitted by the effective policy: {name}",
                    "tool": name,
                    "certainty": "not_started",
                    "retry_safe": True,
                },
            )
        # One schema abstraction for every dispatched tool call: the arguments
        # must fit the schema the tool advertised before its handler can run.
        started = False
        try:
            misses = validate_arguments(tool.input_schema, arguments)
            if misses:
                return ToolExecution(
                    ok=False,
                    failure={
                        "kind": "invalid_arguments",
                        "message": f"invalid arguments: {render_violations(misses)}",
                        "tool": name,
                        "certainty": "not_started",
                        "retry_safe": True,
                        "violations": [
                            {"path": miss.path, "message": miss.message}
                            for miss in misses
                        ],
                    },
                )
            started = True
            execution = tool.handler(arguments, context)
            if not execution.ok:
                execution = replace(
                    execution,
                    terminal=False,
                    failure={
                        "certainty": (execution.failure or {}).get(
                            "executionCertainty", "observed_failure"
                        ),
                        "retry_safe": False,
                        **(execution.failure or {}),
                    },
                )
            return execution
        except (CancelledError, SessionPersistenceError):
            raise
        except Exception as error:  # noqa: BLE001 - a tool fault is a normal turn
            return ToolExecution(
                ok=False,
                failure={
                    "kind": "tool",
                    "message": str(error) or type(error).__name__,
                    "certainty": "unknown_outcome" if started else "not_started",
                    "retry_safe": not started,
                },
            )
