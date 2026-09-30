"""JSON Schema argument validation for agent tool calls.

Reconstructed during the T319 consolidation after the original test module was
lost in a merge. Covers the keywords the Thesis and harness catalogs use and
the precise repair detail every miss carries.
"""

from __future__ import annotations

from thesis.agent.runtime.schema import (
    SchemaViolation,
    render_violations,
    validate_arguments,
    violations,
)


def _paths(misses: tuple[SchemaViolation, ...]) -> list[str]:
    return [miss.path for miss in misses]


def _render(misses: tuple[SchemaViolation, ...]) -> str:
    return render_violations(misses)


def test_a_valid_document_reports_nothing() -> None:
    schema = {
        "type": "object",
        "required": ["name"],
        "properties": {"name": {"type": "string"}},
        "additionalProperties": False,
    }
    assert validate_arguments(schema, {"name": "x"}) == ()


def test_type_mismatch_reports_path_and_both_types() -> None:
    misses = validate_arguments({"type": "string"}, {"value": 1})
    assert _paths(misses) == [""]
    assert "expected string, got object" in misses[0].render()


def test_required_property_miss_uses_child_path() -> None:
    schema = {"type": "object", "required": ["symbol"]}
    misses = validate_arguments(schema, {})
    assert _paths(misses) == ["symbol"]
    assert misses[0].render() == "symbol: required property is missing"


def test_unexpected_property_lists_allowed_names() -> None:
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "additionalProperties": False,
    }
    misses = validate_arguments(schema, {"a": "x", "b": 1})
    assert _paths(misses) == ["b"]
    assert "unexpected property; allowed properties: a" in misses[0].render()


def test_additional_properties_schema_validates_extra_entries() -> None:
    schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": {"type": "integer"},
    }
    misses = validate_arguments(schema, {"count": "many"})
    assert _paths(misses) == ["count"]
    assert "expected integer, got string" in misses[0].render()


def test_string_length_and_pattern_misses() -> None:
    schema = {"type": "string", "minLength": 2, "maxLength": 3, "pattern": "^[a-z]+$"}
    short = violations(schema, "a")
    assert "expected at least 2 character(s), got 1" in short[0].render()
    long = violations(schema, "abcd")
    assert "expected at most 3 character(s), got 4" in long[0].render()
    pattern = violations(schema, "AB")
    assert "does not match ^[a-z]+$" in pattern[0].render()


def test_enum_reports_every_allowed_value() -> None:
    misses = violations({"enum": ["1min", "1d"]}, "5min")
    assert _paths(misses) == [""]
    assert '"1min", "1d"' in misses[0].render()


def test_array_bounds_uniqueness_and_item_paths() -> None:
    schema = {
        "type": "array",
        "minItems": 1,
        "maxItems": 2,
        "uniqueItems": True,
        "items": {"type": "string"},
    }
    assert "expected at least 1 item(s), got 0" in violations(schema, [])[0].render()
    assert (
        "expected at most 2 item(s), got 3"
        in violations(schema, ["a", "b", "c"])[0].render()
    )
    duplicate = violations(schema, ["a", "a"])
    assert _paths(duplicate) == [""]
    assert duplicate[0].render() == "duplicate items are not allowed"
    item = violations(schema, ["a", 2])
    assert _paths(item) == ["[1]"]
    assert "expected string, got integer" in item[0].render()


def test_min_properties_and_nested_paths() -> None:
    schema = {
        "type": "object",
        "minProperties": 1,
        "properties": {
            "model": {"type": "object", "properties": {"window": {"type": "string"}}}
        },
    }
    empty = validate_arguments(schema, {})
    assert "expected at least 1 propert(ies), got 0" in empty[0].render()
    nested = validate_arguments(schema, {"model": {"window": 3}})
    assert _paths(nested) == ["model.window"]


def test_any_of_accepts_the_first_matching_branch() -> None:
    schema = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
    assert violations(schema, 7) == ()
    assert violations(schema, "seven") == ()


def test_any_of_failure_lists_every_branch() -> None:
    schema = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
    misses = violations(schema, True)
    assert len(misses) == 1
    rendered = misses[0].render()
    assert "does not match any accepted argument shape" in rendered
    assert "[1]" in rendered and "[2]" in rendered


def test_every_miss_accumulates_and_renders_with_semicolons() -> None:
    schema = {
        "type": "object",
        "required": ["a", "b"],
        "properties": {"a": {"type": "string"}},
    }
    misses = validate_arguments(schema, {"a": 1})
    assert _paths(misses) == ["b", "a"]
    assert _render(misses) == (
        "b: required property is missing; a: expected string, got integer"
    )


def test_non_mapping_schema_reports_nothing() -> None:
    assert violations(None, {"a": 1}) == ()
    assert validate_arguments("not a schema", {}) == ()
