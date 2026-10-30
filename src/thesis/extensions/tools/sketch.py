"""Runtime-scoped authoring and inspection of committed strategy sketches."""

from collections.abc import Mapping
from typing import cast

from thesis.experiments.mandate import DataWindowResolver, MandateError
from thesis.persistence.git import GitPublicationError
from thesis.workspaces.local import WorkspaceRepositoryError
from thesis.workspaces.sketch_contract import SKETCH_SCHEMA
from thesis.workspaces.sketches import SketchPublicationError, WorkspaceSketches
from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.contracts.failures import failure_from_exception
from thesis_runtime.tools.registry import RegisteredTool, ToolExecution
from thesis_runtime.tools.schema import render_violations, validate_arguments

SKETCH_TOOL_NAMES = (
    "author_strategy_sketch",
    "read_strategy_sketch",
    "update_strategy_sketch",
    "list_strategy_sketches",
)


def sketch_registered_tools(
    service: WorkspaceSketches,
    context: ToolContext,
    *,
    resolve_data_window: DataWindowResolver | None = None,
) -> tuple[RegisteredTool, ...]:
    """Use ordinary tool dispatch and neutral inbox delivery with trusted workspace scope."""
    tools = []
    for name in SKETCH_TOOL_NAMES:
        properties: dict[str, object] = {
            "sketch_id": {"type": "string", "pattern": r"^[a-z][a-z0-9_-]{0,63}$"}
        }
        required = ["sketch_id"]
        if name.startswith(("author_", "update_")):
            properties["sketch"] = SKETCH_SCHEMA
            required.append("sketch")
        if name.startswith("update_"):
            properties["expected_version"] = {
                "type": "string",
                "pattern": r"^[0-9a-f]{64}$",
            }
            required.append("expected_version")
        if name.startswith("read_"):
            properties.update(
                {
                    "git_commit": {
                        "type": "string",
                        "pattern": r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$",
                    },
                    "version": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
                }
            )
        if name.startswith("list_"):
            properties = {
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            }
            required = []
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": required,
        }

        def execute(
            arguments: Mapping[str, object],
            context: ToolContext,
            *,
            operation: str = name,
            parameters: Mapping[str, object] = schema,
        ) -> ToolExecution:
            if resolve_data_window is not None:
                try:
                    resolve_data_window()
                except MandateError as error:
                    return ToolExecution(
                        ok=False,
                        failure=failure_from_exception(
                            error,
                            **{
                                "kind": "research_window",
                                "message": str(error),
                                "certainty": "not_started",
                                "retry_safe": True,
                            },
                        ),
                    )
            misses = validate_arguments(parameters, arguments)
            if misses:
                return ToolExecution(
                    ok=False,
                    failure={
                        "kind": "validation",
                        "message": render_violations(misses),
                    },
                )
            try:
                if operation.startswith("list_"):
                    result = service.list(
                        context.workspace_id,
                        offset=cast(int, arguments.get("offset", 0)),
                        limit=cast(int, arguments.get("limit", 50)),
                    )
                elif operation.startswith("read_"):
                    result = service.read(
                        context.workspace_id,
                        cast(str, arguments["sketch_id"]),
                        git_commit=cast(str | None, arguments.get("git_commit")),
                        version=cast(str | None, arguments.get("version")),
                    )
                else:
                    result = service.save(
                        context.workspace_id,
                        cast(str, arguments["sketch_id"]),
                        cast(Mapping[str, object], arguments["sketch"]),
                        expected_version=cast(
                            str | None, arguments.get("expected_version")
                        ),
                    )
            except SketchPublicationError as error:
                return ToolExecution(
                    ok=False,
                    structured={
                        "reference": error.reference,
                        "executionCertainty": "committed_locally",
                    },
                    failure=failure_from_exception(
                        error, **{"kind": "publication", "message": str(error)}
                    ),
                )
            except (
                ValueError,
                KeyError,
                GitPublicationError,
                WorkspaceRepositoryError,
            ) as error:
                return ToolExecution(
                    ok=False,
                    failure=failure_from_exception(
                        error, **{"kind": "sketch", "message": str(error)}
                    ),
                )
            return ToolExecution(ok=True, structured=result)

        tools.append(
            RegisteredTool(
                descriptor={
                    "name": name,
                    "description": "Develop and inspect the evolving strategy sketch. Unknowns and alternatives are explicit; evidence references authoritative jobs. Writes commit a revision and return its immutable reference; updates require the current version. Read a pinned git_commit/version to inspect a handoff.",
                    "parameters": schema,
                },
                handler=execute,
                operation="read" if name.startswith(("read_", "list_")) else "write",
            )
        )
    return tuple(tools)
