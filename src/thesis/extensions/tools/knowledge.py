"""Bind knowledge retrieval to runtime-owned workspace and source authority."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from thesis.knowledge.service import (
    DATA_ONLY,
    MAX_READ_CHARS,
    KnowledgeCollectionError,
    KnowledgeScope,
    KnowledgeService,
    validate_scope,
)
from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.tools.registry import RegisteredTool, ToolExecution
from thesis_runtime.tools.schema import render_violations, validate_arguments

KNOWLEDGE_TOOL_NAMES = ("search_knowledge", "read_knowledge")
MAX_SEARCH_RESULTS = 5
SEARCH_EXCERPT_CHARS = 400


def knowledge_registered_tools(
    service: KnowledgeService,
    context: ToolContext,
    allowed_scope: KnowledgeScope = "both",
    allowed_collections: tuple[str, ...] | None = None,
) -> tuple[RegisteredTool, ...]:
    """Register data-only tools; composition supplies the trusted scope.

    ``allowed_collections`` is the trusted phase allowlist.  When it is
    restricted the advertised description names the available collections;
    ``None`` leaves retrieval unrestricted by collection.
    """
    allowed = validate_scope(allowed_scope)
    restriction = f"allowed: {allowed}"
    if allowed_collections is not None:
        names = ", ".join(sorted(allowed_collections)) or "none"
        restriction = f"{restriction}; collections: {names}"
    search_schema: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["query"],
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 4000},
            "scope": {
                "type": "string",
                "enum": ["shared", "workspace", "both"],
                "default": allowed,
                "description": f"Omission uses {allowed}; both intersects trusted scope.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_SEARCH_RESULTS,
                "default": 3,
            },
        },
    }
    read_schema: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["reference"],
        "properties": {
            "reference": {"type": "string", "minLength": 1, "maxLength": 512},
            "max_chars": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_READ_CHARS,
                "default": 6000,
            },
        },
    }

    def execute(name: str, arguments: Mapping[str, object]) -> ToolExecution:
        schema = search_schema if name == "search_knowledge" else read_schema
        misses = validate_arguments(schema, arguments)
        if misses:
            return ToolExecution(
                ok=False,
                structured={"data_only": True, "content_notice": DATA_ONLY},
                failure={
                    "kind": "validation",
                    "message": render_violations(misses),
                },
            )
        parameter = "limit" if name == "search_knowledge" else "max_chars"
        maximum = MAX_SEARCH_RESULTS if name == "search_knowledge" else MAX_READ_CHARS
        bound = arguments.get(parameter, 3 if name == "search_knowledge" else 6000)
        if (
            isinstance(bound, bool)
            or not isinstance(bound, int)
            or not 1 <= bound <= maximum
        ):
            return ToolExecution(
                ok=False,
                structured={"data_only": True, "content_notice": DATA_ONLY},
                failure={
                    "kind": "validation",
                    "message": f"{parameter} must be an integer from 1 to {maximum}.",
                },
            )
        try:
            if name == "search_knowledge":
                result = service.search(
                    context.workspace_id,
                    cast(str, arguments["query"]),
                    scope=validate_scope(arguments.get("scope", allowed)),
                    allowed_scope=allowed,
                    limit=bound,
                    allowed_collections=allowed_collections,
                )
                # Project before delivery, without mutating the service's rich results.
                compact: list[dict[str, object]] = []
                for hit in cast(list[dict[str, object]], result["results"])[:bound]:
                    excerpt = cast(str, hit["excerpt"])
                    provenance = cast(Mapping[str, object], hit["provenance"])
                    compact.append(
                        {
                            key: hit[key]
                            for key in (
                                "title",
                                "reference",
                                "page_id",
                                "score",
                                "source",
                                "content_hash",
                                "excerpt_offset",
                            )
                        }
                        | {
                            "excerpt": excerpt[:SEARCH_EXCERPT_CHARS],
                            "truncated": bool(hit["truncated"])
                            or len(excerpt) > SEARCH_EXCERPT_CHARS,
                            "provenance": {
                                key: provenance[key]
                                for key in (
                                    "path",
                                    "source_url",
                                    "source_publisher",
                                    "source_content_hash",
                                    "trust_tier",
                                    "trust_state",
                                )
                            },
                        }
                    )
                result = {
                    "data_only": True,
                    "content_notice": DATA_ONLY,
                    "sources": result["sources"],
                    "retrieval_baseline": result["retrieval_baseline"],
                    "results": compact,
                }
            else:
                result = service.read(
                    context.workspace_id,
                    cast(str, arguments["reference"]),
                    allowed_scope=allowed,
                    max_chars=bound,
                    allowed_collections=allowed_collections,
                )
        except KnowledgeCollectionError as error:
            # The reference is real but this phase's allowlist excludes it; the
            # reason is actionable and contains no filesystem paths.
            return ToolExecution(
                ok=False,
                structured={"data_only": True, "content_notice": DATA_ONLY},
                failure={
                    "kind": "knowledge",
                    "message": str(error),
                },
            )
        except (ValueError, OSError, RuntimeError):
            # Loader/provider errors may contain filesystem paths; do not expose them.
            return ToolExecution(
                ok=False,
                structured={"data_only": True, "content_notice": DATA_ONLY},
                failure={
                    "kind": "knowledge",
                    "message": "Knowledge request rejected: invalid scope, source, or stale reference.",
                },
            )
        return ToolExecution(ok=True, structured=result)

    return (
        RegisteredTool(
            descriptor={
                "name": "search_knowledge",
                "description": f"Search shared/workspace knowledge ({restriction}). "
                "Returns up to 5 compact hits (default 3), with excerpts up to "
                "400 characters and opaque references. Use read_knowledge for "
                "selected full content. " + DATA_ONLY,
                "parameters": search_schema,
            },
            handler=lambda arguments, context: execute("search_knowledge", arguments),
            operation="read",
        ),
        RegisteredTool(
            descriptor={
                "name": "read_knowledge",
                "description": "Read a current reference returned by search_knowledge; "
                "never pass a filesystem path. Returns full reference metadata "
                "and content bounded by max_chars (default 6000). " + DATA_ONLY,
                "parameters": read_schema,
            },
            handler=lambda arguments, context: execute("read_knowledge", arguments),
            operation="read",
        ),
    )


__all__ = ["KNOWLEDGE_TOOL_NAMES", "knowledge_registered_tools"]
