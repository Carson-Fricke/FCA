"""Inspect canonical exploratory outputs through ordinary session tool dispatch."""

from collections.abc import Mapping
from typing import cast

from thesis.agent.runtime.schema import render_violations, validate_arguments
from thesis.agent.runtime.session import RegisteredTool, ToolExecution
from thesis.integrations.mcp import HarnessMcpError
from thesis.workspaces.programs import WorkspacePrograms

EXPLORATION_TOOL_NAMES = ("read_study_output",)


def exploration_registered_tools(
    service: WorkspacePrograms,
) -> tuple[RegisteredTool, ...]:
    """Advertise paged sequences, events and tables; jobs remain evidence authority."""
    schema: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["job_id"],
        "properties": {
            "job_id": {"type": "string", "minLength": 1},
            "instance_id": {"type": "string", "minLength": 1},
            "output_name": {"type": "string", "minLength": 1},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
        },
    }

    def execute(arguments: Mapping[str, object]) -> ToolExecution:
        misses = validate_arguments(schema, arguments)
        if misses:
            return ToolExecution(
                ok=False,
                failure={"kind": "validation", "message": render_violations(misses)},
            )
        try:
            result = service.read_study_output(
                cast(str, arguments["job_id"]),
                instance_id=cast(str | None, arguments.get("instance_id")),
                output_name=cast(str | None, arguments.get("output_name")),
                offset=cast(int, arguments.get("offset", 0)),
                limit=cast(int, arguments.get("limit", 100)),
            )
        except (ValueError, KeyError, HarnessMcpError) as error:
            return ToolExecution(
                ok=False, failure={"kind": "exploration", "message": str(error)}
            )
        return ToolExecution(ok=True, structured=result)

    return (
        RegisteredTool(
            spec={
                "type": "function",
                "function": {
                    "name": "read_study_output",
                    "description": "Inspect immutable exploratory_outputs.json from a study or strategy job. Omit output_name to list output names/instances; then select instance_id/output_name and page with nextOffset. Preserves availability times and undefined reasons. Use the full job artifact for complete export.",
                    "parameters": schema,
                },
            },
            handler=execute,
        ),
    )
