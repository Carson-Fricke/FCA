"""Adapt committed program operations to ordinary, runtime-attributed tools."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import cast

from thesis.experiments.mandate import DataWindowPolicy
from thesis.integrations.mcp import HarnessMcpError
from thesis.persistence.git import GitPublicationError
from thesis.workspaces.local import WorkspaceRepositoryError
from thesis.workspaces.programs import (
    INDICATOR_ID_PATTERN,
    MAX_LIST_RESULTS,
    MAX_NULL_BLOCK_SIZE,
    MAX_READ_CHARS,
    NULL_TRANSFORM_MODES,
    PROGRAM_CONFIGURATION_SCHEMA,
    PROGRAM_ID_PATTERN,
    JobRetention,
    JobSettlement,
    ProgramArtifactError,
    ProgramKind,
    ProgramPublicationError,
    WorkspacePrograms,
)
from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.tools.registry import RegisteredTool, ToolExecution
from thesis_runtime.tools.schema import render_violations, validate_arguments

#: Retain one job id for a trusted session context before harness dispatch.
ContextJobRetention = Callable[
    [ToolContext, str, str, Mapping[str, object]], JobSettlement
]

PROGRAM_TOOL_NAMES = (
    "author_study",
    "update_study",
    "read_study",
    "list_studies",
    "validate_study",
    "run_study",
    "author_strategy",
    "update_strategy",
    "read_strategy",
    "list_strategies",
    "validate_strategy",
    "run_backtest",
    "run_backtest_batch",
    "run_null_batch",
    "author_indicator",
    "update_indicator",
    "read_indicator",
    "list_indicators",
    "validate_indicator",
    "read_sdk_reference",
    "publish_study",
    "publish_strategy",
    "publish_indicator",
)
_LIST_KINDS = {"studies": "study", "strategies": "strategy", "indicators": "indicator"}
_ID = {"type": "string", "pattern": PROGRAM_ID_PATTERN}
_LINE = {"type": "integer", "minimum": 1}
_SOURCE = {"type": "string", "minLength": 1, "maxLength": 1_048_576}
_INDICATOR_ID = {
    "type": "string",
    "pattern": INDICATOR_ID_PATTERN,
    "description": "Indicator Rust module ID: lowercase letters, digits and underscores; no hyphens or Rust reserved identifiers.",
}
_DEPENDENCIES = {"type": "array", "uniqueItems": True, "items": _INDICATOR_ID}


def _context_retention(
    retention: ContextJobRetention, context: ToolContext
) -> JobRetention:
    """Bind one trusted session context to the service retention signature."""

    def retain(
        job_id: str, tool_name: str, attribution: Mapping[str, object]
    ) -> JobSettlement:
        return retention(context, job_id, tool_name, attribution)

    return retain


def program_registered_tools(
    service: WorkspacePrograms,
    context: ToolContext,
    *,
    data_window: DataWindowPolicy | None = None,
    job_retention: ContextJobRetention | None = None,
) -> tuple[RegisteredTool, ...]:
    """Use the existing session dispatcher and inbox, never a parallel executor.

    Only author/update accept source; execution consumes committed IDs. Workspace
    authority comes out of band from ``context``, not program contents or metadata.
    Composition must replace conflicting harness/legacy author tool registrations.
    ``data_window`` is the workflow execution's research-mandate policy, forwarded
    to every execution handler; a missing policy leaves execution unrestricted.
    ``job_retention`` durably records each fresh execution id before dispatch for
    contexts that own a steerable workflow execution; chat-level contexts leave
    it unset and run unretained.
    """
    retain = (
        None if job_retention is None else _context_retention(job_retention, context)
    )
    tools = []
    for name in PROGRAM_TOOL_NAMES:
        operation, _, suffix = name.partition("_")
        if operation == "list":
            kind = cast(ProgramKind, _LIST_KINDS[suffix])
        elif name in ("run_backtest", "run_backtest_batch", "run_null_batch"):
            kind = "strategy"
        else:
            kind = cast(ProgramKind, suffix)
        properties: dict[str, object] = {
            "program_id": _INDICATOR_ID if kind == "indicator" else _ID
        }
        required = ["program_id"]
        if operation == "list":
            properties = {
                "prefix": {"type": "string", "pattern": r"^[a-z0-9_-]{1,64}$"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_LIST_RESULTS,
                },
            }
            required = []
        elif operation == "author":
            properties.update(
                {
                    "source": _SOURCE,
                    "template": {"type": "string", "enum": ["basic"]},
                    "configuration": PROGRAM_CONFIGURATION_SCHEMA,
                    "indicator_ids": _DEPENDENCIES,
                }
            )
            required.append("configuration")
            if kind == "indicator":
                properties["indicator_type"] = {
                    "type": "string",
                    "pattern": r"^[A-Za-z_][A-Za-z0-9_]*$",
                }
            elif kind == "strategy":
                properties["parameter_declaration"] = {"type": "object"}
        elif operation == "update":
            properties.update(
                {
                    "expected_version": {
                        "type": "string",
                        "pattern": r"^[0-9a-f]{64}$",
                    },
                    "expected": _SOURCE,
                    "replacement": {"type": "string", "maxLength": 1_048_576},
                    "configuration": PROGRAM_CONFIGURATION_SCHEMA,
                    "indicator_ids": _DEPENDENCIES,
                }
            )
            required.append("expected_version")
            if kind == "strategy":
                properties["parameter_declaration"] = {"type": "object"}
        elif operation == "read":
            properties.update(
                {
                    "start_line": _LINE,
                    "end_line": _LINE,
                    "max_chars": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_READ_CHARS,
                    },
                }
            )
            if kind in ("study", "strategy"):
                properties["job_id"] = {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                }
        elif operation == "publish":
            properties["commit"] = {
                "type": "string",
                "pattern": r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$",
            }
            required.append("commit")
        elif name == "run_backtest":
            properties["parameters"] = {"type": "object"}
        elif name == "run_backtest_batch":
            properties.update(
                {
                    "assignments": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "object"},
                    },
                    "label": {"type": "string", "minLength": 1, "maxLength": 200},
                }
            )
            required.append("assignments")
        elif name == "run_null_batch":
            properties.update(
                {
                    "parameters": {"type": "object"},
                    "mode": {"type": "string", "enum": list(NULL_TRANSFORM_MODES)},
                    "offset": {"type": "integer", "minimum": 0},
                    "chunk_size": {"type": "integer", "minimum": 1, "maximum": 256},
                    "block_size": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_NULL_BLOCK_SIZE,
                    },
                    "seeds": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": {"type": "integer", "minimum": 0},
                    },
                    "label": {"type": "string", "minLength": 1, "maxLength": 200},
                }
            )
            required.extend(["mode", "block_size", "seeds"])
        if name == "read_sdk_reference":
            properties = {
                "topic": {
                    "type": "string",
                    "enum": [
                        "study",
                        "strategy",
                        "indicator",
                        "indicator_api",
                        "wavelet",
                        "consumer",
                        "program",
                        "events",
                        "exports",
                        "decimal",
                        "building_blocks",
                        "research_views",
                        "research_publishing",
                        "research_recipes",
                        "outputs",
                    ],
                },
                "start_line": _LINE,
                "end_line": _LINE,
                "max_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_READ_CHARS,
                },
            }
            required = ["topic"]
        schema: dict[str, object] = {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": required,
        }
        if operation == "author":
            schema["anyOf"] = [{"required": ["source"]}, {"required": ["template"]}]

        def handle(
            arguments: Mapping[str, object],
            context: ToolContext,
            *,
            tool_name: str = name,
            tool_kind: ProgramKind = kind,
            tool_operation: str = operation,
            tool_schema: dict[str, object] = schema,
        ) -> ToolExecution:
            if not context.permits(tool_name):
                return ToolExecution(
                    ok=False,
                    failure={
                        "kind": "denied",
                        "message": "Program tool is not permitted.",
                    },
                )
            misses = validate_arguments(tool_schema, arguments)
            if misses:
                return ToolExecution(
                    ok=False,
                    failure={
                        "kind": "validation",
                        "message": render_violations(misses),
                    },
                )
            try:
                if tool_operation == "author" and ("source" in arguments) == (
                    "template" in arguments
                ):
                    raise ProgramArtifactError(
                        "provide exactly one of source or template"
                    )
                if tool_operation == "update" and ("expected" in arguments) != (
                    "replacement" in arguments
                ):
                    raise ProgramArtifactError(
                        "expected and replacement must be provided together"
                    )
                if tool_name == "read_sdk_reference":
                    result = service.read_sdk_reference(
                        cast(str, arguments["topic"]),
                        start_line=cast(int, arguments.get("start_line", 1)),
                        end_line=cast(int | None, arguments.get("end_line")),
                        max_chars=cast(int, arguments.get("max_chars", MAX_READ_CHARS)),
                    )
                elif tool_operation == "list":
                    result = service.list(
                        context.workspace_id,
                        tool_kind,
                        prefix=cast(str | None, arguments.get("prefix")),
                        limit=cast(int, arguments.get("limit", 50)),
                    )
                else:
                    workspace = context.workspace_id
                    program_id = cast(str, arguments["program_id"])
                    if tool_operation == "author":
                        result = service.author(
                            workspace,
                            tool_kind,
                            program_id,
                            cast(str | None, arguments.get("source")),
                            template=cast(str | None, arguments.get("template")),
                            configuration=cast(
                                Mapping[str, object] | None,
                                arguments.get("configuration"),
                            ),
                            indicator_ids=cast(
                                list[str], arguments.get("indicator_ids", [])
                            ),
                            indicator_type=cast(
                                str,
                                arguments.get("indicator_type", "AuthoredIndicator"),
                            ),
                            parameter_declaration=cast(
                                Mapping[str, object] | None,
                                arguments.get("parameter_declaration"),
                            ),
                        )
                    elif tool_operation == "update":
                        result = service.update(
                            workspace,
                            tool_kind,
                            program_id,
                            expected_version=cast(str, arguments["expected_version"]),
                            expected=cast(str | None, arguments.get("expected")),
                            replacement=cast(str | None, arguments.get("replacement")),
                            configuration=cast(
                                Mapping[str, object] | None,
                                arguments.get("configuration"),
                            ),
                            indicator_ids=cast(
                                list[str] | None, arguments.get("indicator_ids")
                            ),
                            parameter_declaration=cast(
                                Mapping[str, object] | None,
                                arguments.get("parameter_declaration"),
                            ),
                        )
                    elif tool_operation == "read":
                        result = service.read(
                            workspace,
                            tool_kind,
                            program_id,
                            start_line=cast(int, arguments.get("start_line", 1)),
                            end_line=cast(int | None, arguments.get("end_line")),
                            max_chars=cast(
                                int, arguments.get("max_chars", MAX_READ_CHARS)
                            ),
                            job_id=cast(str | None, arguments.get("job_id")),
                        )
                    elif tool_operation == "validate":
                        result = service.validate(workspace, tool_kind, program_id)
                    elif tool_operation == "publish":
                        result = service.publish(
                            workspace,
                            tool_kind,
                            program_id,
                            cast(str, arguments["commit"]),
                        )
                    elif tool_name == "run_backtest":
                        result = service.run(
                            workspace,
                            "strategy",
                            program_id,
                            parameters=cast(
                                Mapping[str, object] | None,
                                arguments.get("parameters"),
                            ),
                            data_window=data_window,
                            retain=retain,
                        )
                    elif tool_name == "run_backtest_batch":
                        result = service.run_batch(
                            workspace,
                            "strategy",
                            program_id,
                            cast(
                                Sequence[Mapping[str, object]],
                                arguments["assignments"],
                            ),
                            label=cast(str | None, arguments.get("label")),
                            data_window=data_window,
                            retain=retain,
                        )
                    elif tool_name == "run_null_batch":
                        result = service.run_null_batch(
                            workspace,
                            "strategy",
                            program_id,
                            parameters=cast(
                                Mapping[str, object] | None,
                                arguments.get("parameters"),
                            ),
                            block_size=cast(int, arguments["block_size"]),
                            seeds=cast(Sequence[int], arguments["seeds"]),
                            mode=cast(str, arguments["mode"]),
                            offset=cast(int, arguments.get("offset", 0)),
                            chunk_size=cast(int, arguments.get("chunk_size", 64)),
                            label=cast(str | None, arguments.get("label")),
                            data_window=data_window,
                            retain=retain,
                        )
                    else:
                        result = service.run(
                            workspace,
                            tool_kind,
                            program_id,
                            data_window=data_window,
                            retain=retain,
                        )
            except ProgramPublicationError as error:
                return ToolExecution(
                    ok=False,
                    structured=error.receipt,
                    failure={
                        "kind": "publication",
                        "message": str(error),
                        "executionCertainty": "local_commit",
                        "retryGuidance": f"Inspect the pinned receipt; call publish_{tool_kind} with program_id and commit=gitCommit only to retry repository publication. Checkout/index repair requires the operator; do not re-author or rerun.",
                    },
                )
            except HarnessMcpError as error:
                return ToolExecution(
                    ok=False,
                    failure={
                        "kind": "harness",
                        "message": "Harness program request failed.",
                        "executionCertainty": getattr(
                            error, "execution_certainty", "unknown_outcome"
                        ),
                        "retryGuidance": "Inspect job/run state before an explicit retry; never automatically replay execution.",
                    },
                )
            except (
                ProgramArtifactError,
                GitPublicationError,
                WorkspaceRepositoryError,
                OSError,
            ) as error:
                return ToolExecution(
                    ok=False,
                    failure={
                        "kind": "program",
                        "message": str(error)
                        if isinstance(error, ProgramArtifactError)
                        else "Program persistence request failed.",
                        "executionCertainty": "unknown_outcome"
                        if isinstance(error, GitPublicationError)
                        else "not_started",
                        "retryGuidance": "Inspect committed program state before explicitly retrying a mutation.",
                    },
                )
            if result.get("accepted") is False or result.get("ok") is False:
                return ToolExecution(
                    ok=False,
                    structured=result,
                    failure={
                        "kind": "validation"
                        if tool_operation == "validate"
                        else "harness",
                        "message": result.get("message")
                        or "Committed program request rejected; inspect diagnostics.",
                        "executionCertainty": result.get("executionCertainty"),
                        "retryGuidance": result.get("retryGuidance"),
                    },
                )
            return ToolExecution(ok=True, structured=result)

        tools.append(
            RegisteredTool(
                descriptor={
                    "name": name,
                    "description": (
                        "Read exact serving SDK signatures by topic, or locally tested Rust templates only when packaged and fresh serving SDK identities match. Without a harness, references are local guidance only."
                        if name == "read_sdk_reference"
                        else "Read a bounded program source range. With job_id, read the immutable accepted source for that executed program_id; without job_id, read the latest committed workspace source. Use job_id when reviewing results."
                        if name in ("read_study", "read_strategy")
                        else "Explicitly retry repository publication with program_id and pinned commit from the failure receipt. Never edits checkout/index state, re-authors, validates, or reruns. Ask the operator to reconcile checkout/index failures."
                        if operation == "publish"
                        else f"List committed workspace {kind} IDs at HEAD with content version, hashes, and compact configuration. Read a selected ID before patching; author only a new ID."
                        if operation == "list"
                        else "Submit distinct validated parameter assignments for a strategy through the existing run boundary; every point is validated before any submission and the deterministic batch id carries per-point run receipts. Unknown-outcome points are recorded truthfully and never replayed automatically; inspect each run before an explicit retry."
                        if name == "run_backtest_batch"
                        else "Submit a bounded chunk of ordinary or block-permutation null jobs from the complete frozen seed list; advance offset to nextOffset until completedSubmission for a committed strategy; the parameter assignment and block_size are validated before any submission, every point carries the frozen null config and a fresh opaque run id, and unknown outcomes are recorded truthfully and never replayed."
                        if name == "run_null_batch"
                        else "Run a committed workspace strategy by program_id with an optional parameters assignment validated against its parameter_declaration before submission; omitted names receive declared defaults."
                        if name == "run_backtest"
                        else f"{operation.title()} a workspace {kind} by program_id. Latest committed contents are authoritative. "
                        "Author with source or template=basic and complete dated configuration with exact subscriptions. "
                        "Updates require a current content version; provide paired expected/replacement for a source patch or configuration alone. Validate before running. "
                        "Indicators are Rust modules; expose a public Default type for wrapper validation. "
                        "No workspace paths, execution attribution, or inline source on validation/run."
                    ),
                    "parameters": schema,
                },
                handler=handle,
                operation="read"
                if operation in {"read", "list"}
                else "write"
                if operation in {"author", "update", "publish"}
                else "execute",
            )
        )
    return tuple(tools)


__all__ = ["PROGRAM_TOOL_NAMES", "program_registered_tools"]
