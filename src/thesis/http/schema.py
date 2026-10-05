"""Export the authoritative OpenAPI 3.1 contract and runtime components.

Run ``uv run --extra dev python -m thesis.http.schema`` for stdout, or supply
``--output <path>`` with an existing parent to write deterministic JSON.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from thesis.agent.messages import SESSION_MESSAGE_ADAPTER
from thesis.agent.runtime.activity import chat_message_schema, runtime_activity_schema
from thesis.agent.runtime.datum import (
    DeliveredDatumWire,
    datum_schema,
    tool_result_schema,
)
from thesis.http.application import create_app
from thesis.http.wire import TimelineItemWire


def _components(schema: dict[str, Any], name: str, fragment: dict[str, Any]) -> None:
    """Hoist generated definitions and rebase all refs into OpenAPI components."""
    fragment = copy.deepcopy(fragment)
    definitions = fragment.pop("$defs", {})

    def rebase(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: rebase(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rebase(item) for item in value]
        if isinstance(value, str) and value.startswith("#/$defs/"):
            return value.replace("#/$defs/", "#/components/schemas/", 1)
        return value

    components = schema["components"]["schemas"]
    for key, definition in definitions.items():
        definition = rebase(definition)
        if key in components and components[key] != definition:
            raise ValueError(f"conflicting generated schema definition: {key}")
        components[key] = definition
    components[name] = rebase(fragment)


def enrich_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return OpenAPI plus canonical runtime schemas with one reference namespace."""
    schema["openapi"] = "3.1.0"
    schema["jsonSchemaDialect"] = "https://json-schema.org/draft/2020-12/schema"
    _components(schema, "RuntimeActivityWire", runtime_activity_schema())
    _components(schema, "DatumWire", datum_schema())
    _components(schema, "ToolResultWire", tool_result_schema())
    _components(
        schema,
        "SessionMessageWire",
        SESSION_MESSAGE_ADAPTER.json_schema(mode="serialization"),
    )
    _components(
        schema,
        "DeliveredDatumWire",
        DeliveredDatumWire.model_json_schema(by_alias=True, mode="serialization"),
    )
    # Runtime owns metadata; the durable record adapter supplies the field inventory.
    message = chat_message_schema()
    _components(schema, "ChatMessageRecordWire", message)
    components = schema["components"]["schemas"]
    canonical_activity = copy.deepcopy(components["RuntimeActivityWire"])
    for reference in canonical_activity["oneOf"]:
        definition = components[reference["$ref"].rsplit("/", 1)[-1]]
        definition["required"] = sorted(
            set(definition.get("required", [])) | {"activityKind"}
        )
    metadata = {
        "anyOf": [
            canonical_activity,
            {
                "type": "object",
                "additionalProperties": True,
                "not": {"required": ["activityKind"]},
            },
            {"type": "null"},
        ]
    }
    metadata = {
        "allOf": [
            metadata,
            {
                "properties": {
                    "sessionMessage": {
                        "$ref": "#/components/schemas/SessionMessageWire"
                    },
                    "datums": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/DatumWire"},
                    },
                }
            },
        ]
    }
    for name in (
        "ChatMessageWire",
        "CreateMessageResponseWire",
        "NodeInputReceiptWire",
        "ChatMessageRecordWire",
    ):
        components[name]["properties"]["data"] = copy.deepcopy(metadata)
        components[name]["properties"]["tool_result"] = {
            "anyOf": [
                {"$ref": "#/components/schemas/ToolResultWire"},
                {"$ref": "#/components/schemas/DeliveredDatumWire"},
                {"type": "null"},
            ]
        }
    components.pop("ChatMessageRecordWire")
    timeline = TypeAdapter(TimelineItemWire).json_schema(
        by_alias=True, mode="serialization", ref_template="#/components/schemas/{model}"
    )
    # These definitions already belong to FastAPI; only the named union is new.
    timeline.pop("$defs", None)
    _components(schema, "TimelineItemWire", timeline)
    for path, operations in schema["paths"].items():
        if path.endswith("/timeline/stream"):
            operations["get"]["responses"]["200"]["content"] = {
                "text/event-stream": {
                    "schema": {"type": "string"},
                    "x-thesis-event-schema": {
                        "$ref": "#/components/schemas/TimelineItemWire"
                    },
                }
            }
    return schema


def export_schema() -> dict[str, Any]:
    return create_app(in_memory=False).openapi()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, help="Existing-parent output path; defaults to stdout"
    )
    args = parser.parse_args()
    content = json.dumps(export_schema(), indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(content, end="")
    else:
        if not args.output.parent.is_dir():
            parser.error("output parent directory must already exist")
        args.output.write_text(content, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
