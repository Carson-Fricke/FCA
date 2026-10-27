"""Agent-independent node, port and completion contracts.

Routes deliver one immutable output to declared input ports. Activity selection,
conversations, persistence and scheduling belong to the host, not this model.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


class WorkflowSpecError(ValueError):
    """An invalid definition, conflicting delivery or outdated activation."""


@dataclass(frozen=True, slots=True)
class Destination:
    node: str
    port: str

    @classmethod
    def parse(cls, value: str) -> "Destination":
        node, separator, port = value.partition(".")
        if not separator or not node.strip() or not port.strip() or "." in port:
            raise WorkflowSpecError("a route destination must be 'node.port'")
        return cls(node, port)

    def __str__(self) -> str:
        return f"{self.node}.{self.port}"


@dataclass(frozen=True, slots=True)
class Node:
    name: str
    inputs: tuple[str, ...]
    routes: Mapping[str, tuple[Destination, ...]]
    outcome: str | None = None

    def __post_init__(self) -> None:
        if (
            not self.name
            or not self.inputs
            or len(set(self.inputs)) != len(self.inputs)
        ):
            raise WorkflowSpecError(
                "a node requires a name and distinct named input ports"
            )
        if any(not port.strip() or "." in port for port in self.inputs):
            raise WorkflowSpecError("input ports must be nonempty names without dots")
        if self.outcome not in {None, "completed", "failed"}:
            raise WorkflowSpecError("unknown terminal outcome")
        if bool(self.routes) == (self.outcome is not None):
            raise WorkflowSpecError("only terminal nodes have no routes")
        for route, destinations in self.routes.items():
            if not route.strip() or not destinations:
                raise WorkflowSpecError("each route requires a name and destinations")
            if len(set(destinations)) != len(destinations):
                raise WorkflowSpecError(
                    "a route cannot deliver twice to the same input"
                )
        object.__setattr__(self, "routes", MappingProxyType(dict(self.routes)))


@dataclass(frozen=True, slots=True)
class ActivityCompletion:
    route: str
    output: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.route, str) or not self.route.strip():
            raise WorkflowSpecError("completion requires an explicit route")
        if not isinstance(self.output, Mapping):
            raise WorkflowSpecError("activity output must be a JSON object")
        detached = json.loads(json.dumps(dict(self.output), allow_nan=False))
        object.__setattr__(self, "output", MappingProxyType(detached))

    def as_dict(self) -> dict[str, object]:
        return {"route": self.route, "output": dict(self.output)}
