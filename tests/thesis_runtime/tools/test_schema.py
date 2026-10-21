"""Thesis validation boundary: finite JSON, immutable containers, repair paths."""

from types import MappingProxyType
from typing import Any, cast

import pytest
from jsonschema.exceptions import SchemaError
from referencing.exceptions import Unresolvable

from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.tools.registry import (
    RegisteredTool,
    SessionToolRegistry,
    ToolExecution,
)
from thesis_runtime.tools.schema import check_schema, render_violations, violations


def test_local_references_and_nested_repair_paths() -> None:
    schema = {
        "$defs": {"bounded": {"type": "integer", "minimum": 1}},
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {"$ref": "#/$defs/bounded"}}
        },
    }
    assert violations(schema, MappingProxyType({"items": (1, 2)})) == ()
    misses = violations(schema, {"items": [0]})
    assert [miss.path for miss in misses] == ["items[0]"]
    assert "minimum" in render_violations(misses)


@pytest.mark.parametrize(
    "schema,value",
    [
        ({"oneOf": [{"type": "integer"}, {"minimum": 0}]}, 1),
        ({"allOf": [{"type": "integer"}, {"maximum": 2}]}, 3),
        ({"const": "fixed"}, "other"),
        ({"type": "integer"}, True),
        ({"type": "number"}, float("nan")),
        ({"type": "number"}, float("inf")),
        ({"type": "object"}, {1: "bad key"}),
        (False, {}),
    ],
)
def test_invalid_arguments_are_rejected(schema: object, value: object) -> None:
    assert violations(schema, value)


@pytest.mark.parametrize("schema", [None, {"type": "not-a-type"}, {"minimum": "bad"}])
def test_invalid_schemas_are_not_silently_accepted(schema: object) -> None:
    with pytest.raises(SchemaError):
        check_schema(schema)


def test_duplicate_registration_and_validation_never_invoke_handler() -> None:
    calls: list[object] = []
    tool = RegisteredTool(
        {
            "name": "bounded",
            "parameters": {
                "type": "object",
                "properties": {"count": {"type": "integer", "minimum": 1}},
                "required": ["count"],
            },
        },
        lambda args, context: (calls.append(args), ToolExecution(ok=True))[1],
    )
    registry = SessionToolRegistry([tool])
    with pytest.raises(ValueError, match="duplicate"):
        registry.register(tool)
    outcome = registry.dispatch(
        "bounded",
        {"count": 0},
        context=ToolContext("workspace", "chat", "conversation", "actor"),
    )
    assert not outcome.ok
    assert outcome.failure and outcome.failure["certainty"] == "not_started"
    assert calls == []


@pytest.mark.parametrize(
    "schema",
    [{"$ref": "https://untrusted.invalid/schema"}, {"$ref": "#/$defs/missing"}],
)
def test_unresolvable_schema_is_rejected_before_advertisement(schema: object) -> None:
    with pytest.raises((ValueError, Unresolvable)):
        check_schema(schema)


def test_registered_schema_is_detached_from_both_caller_and_advertisement() -> None:
    schema: dict[str, Any] = {"type": "object", "properties": {"count": {"minimum": 1}}}
    tool = RegisteredTool(
        {"name": "work", "parameters": schema},
        lambda args, context: ToolExecution(ok=True),
    )
    registry = SessionToolRegistry([tool])
    schema["properties"]["count"]["minimum"] = -10
    advertised = cast(Any, registry.catalog())
    advertised[0]["parameters"]["properties"]["count"]["minimum"] = -10
    assert not registry.dispatch(
        "work",
        {"count": 0},
        context=ToolContext("workspace", "chat", "conversation", "actor"),
    ).ok


@pytest.mark.parametrize(
    ("keyword", "limit", "value"),
    [
        ("maxLength", 4096, "x" * 5000),
        ("minLength", 3, "x"),
        ("maximum", 8, 9),
        ("pattern", "^[a-z]+$", "INVALID"),
    ],
)
def test_constraint_diagnostics_reach_tool_caller_without_echoing_large_arguments(
    keyword, limit, value
):
    registry = SessionToolRegistry(
        [
            RegisteredTool(
                {
                    "name": "bounded",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "completion": {
                                "type": "object",
                                "properties": {"reason": {keyword: limit}},
                            }
                        },
                    },
                },
                lambda args, ctx: ToolExecution(ok=True),
            )
        ]
    )
    result = registry.dispatch(
        "bounded",
        {"completion": {"reason": value}},
        context=ToolContext("workspace", "chat", "conversation", "actor"),
    )
    assert not result.ok and result.failure is not None
    assert result.failure["certainty"] == "not_started"
    miss = cast(list[dict[str, str]], result.failure["violations"])[0]
    assert miss["path"] == "completion.reason"
    assert keyword in miss["message"] and str(limit) in miss["message"]
    assert len(miss["message"]) < 512
    if keyword == "maxLength":
        assert "5000" in miss["message"] and value not in str(result.failure["message"])
