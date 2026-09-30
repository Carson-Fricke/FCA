"""The single JSON Schema validation abstraction for agent tool arguments.

Every dispatched tool call validates its arguments here, against the schema
the tool advertised, before its handler runs.  The abstraction supports the
JSON Schema keywords the Thesis and harness catalogs use (`type`, `enum`,
`pattern`, `minLength`/`maxLength`, `minItems`/`maxItems`, `uniqueItems`,
`items`, `minProperties`, `required`, `properties`, `additionalProperties`,
`anyOf`) and returns every miss with its JSON path and the expected shape, so a
model's malformed submission is repaired from the precise detail instead of a
generic rejection.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "SchemaViolation",
    "render_violations",
    "validate_arguments",
    "violations",
]


@dataclass(frozen=True, slots=True)
class SchemaViolation:
    """One precise schema miss: the JSON path and what was expected."""

    path: str
    message: str

    def render(self) -> str:
        """Render the miss as one repair line."""
        return self.message if not self.path else f"{self.path}: {self.message}"


def violations(
    schema: object, value: object, path: str = ""
) -> tuple[SchemaViolation, ...]:
    """Validate one JSON value against one schema; returns every miss."""
    if not isinstance(schema, Mapping):
        return ()
    found: list[SchemaViolation] = []

    branches = schema.get("anyOf")
    if isinstance(branches, Sequence) and not isinstance(branches, str | bytes):
        failures: list[tuple[SchemaViolation, ...]] = []
        satisfied = False
        for branch in branches:
            branch_misses = violations(branch, value, path)
            if not branch_misses:
                satisfied = True
                break
            failures.append(branch_misses)
        if not satisfied:
            listed = " | ".join(
                "[{}] {}".format(
                    index + 1, "; ".join(miss.render() for miss in branch_misses)
                )
                for index, branch_misses in enumerate(failures)
            )
            found.append(
                SchemaViolation(
                    path,
                    f"does not match any accepted argument shape: {listed}",
                )
            )
            return tuple(found)

    expected_type = schema.get("type")
    if expected_type is not None and not _matches_type(expected_type, value):
        found.append(
            SchemaViolation(
                path,
                f"expected {_expected_type_label(expected_type)}, "
                f"got {_type_name(value)}",
            )
        )
        return tuple(found)

    options = schema.get("enum")
    if isinstance(options, Sequence) and not isinstance(options, str | bytes):
        if not any(_equal(option, value) for option in options):
            allowed = ", ".join(_render_value(option) for option in options)
            found.append(
                SchemaViolation(
                    path, f"value {_render_value(value)} is not one of: {allowed}"
                )
            )

    if isinstance(value, str):
        found.extend(_string_misses(schema, value, path))
    elif isinstance(value, list | tuple):
        found.extend(_array_misses(schema, list(value), path))
    elif isinstance(value, Mapping):
        found.extend(_object_misses(schema, value, path))

    return tuple(found)


def validate_arguments(
    schema: object, arguments: Mapping[str, object]
) -> tuple[SchemaViolation, ...]:
    """Validate one tool call's arguments against the tool's input schema."""
    return violations(schema, arguments)


def render_violations(misses: Sequence[SchemaViolation]) -> str:
    """Render every miss as one repair line joined with semicolons."""
    return "; ".join(miss.render() for miss in misses)


def _string_misses(
    schema: Mapping[object, object], text: str, path: str
) -> list[SchemaViolation]:
    found: list[SchemaViolation] = []
    length = len(text)
    minimum = schema.get("minLength")
    if isinstance(minimum, int) and not isinstance(minimum, bool) and length < minimum:
        found.append(
            SchemaViolation(
                path, f"expected at least {minimum} character(s), got {length}"
            )
        )
    maximum = schema.get("maxLength")
    if isinstance(maximum, int) and not isinstance(maximum, bool) and length > maximum:
        found.append(
            SchemaViolation(
                path, f"expected at most {maximum} character(s), got {length}"
            )
        )
    pattern = schema.get("pattern")
    if isinstance(pattern, str) and re.search(pattern, text) is None:
        found.append(
            SchemaViolation(
                path, f"value {_render_value(text)} does not match {pattern}"
            )
        )
    return found


def _array_misses(
    schema: Mapping[object, object], items: list[object], path: str
) -> list[SchemaViolation]:
    found: list[SchemaViolation] = []
    minimum = schema.get("minItems")
    if (
        isinstance(minimum, int)
        and not isinstance(minimum, bool)
        and len(items) < minimum
    ):
        found.append(
            SchemaViolation(
                path, f"expected at least {minimum} item(s), got {len(items)}"
            )
        )
    maximum = schema.get("maxItems")
    if (
        isinstance(maximum, int)
        and not isinstance(maximum, bool)
        and len(items) > maximum
    ):
        found.append(
            SchemaViolation(
                path, f"expected at most {maximum} item(s), got {len(items)}"
            )
        )
    if schema.get("uniqueItems") is True:
        rendered = [_canonical(item) for item in items]
        if len(set(rendered)) != len(rendered):
            found.append(SchemaViolation(path, "duplicate items are not allowed"))
    item_schema = schema.get("items")
    if item_schema is not None:
        for index, item in enumerate(items):
            found.extend(violations(item_schema, item, f"{path}[{index}]"))
    return found


def _object_misses(
    schema: Mapping[object, object], entries: Mapping[object, object], path: str
) -> list[SchemaViolation]:
    found: list[SchemaViolation] = []
    properties = schema.get("properties")
    declared = properties if isinstance(properties, Mapping) else {}
    required = schema.get("required")
    if isinstance(required, Sequence) and not isinstance(required, str | bytes):
        for name in required:
            if isinstance(name, str) and name not in entries:
                found.append(
                    SchemaViolation(
                        _child_path(path, name), "required property is missing"
                    )
                )
    minimum = schema.get("minProperties")
    if (
        isinstance(minimum, int)
        and not isinstance(minimum, bool)
        and len(entries) < minimum
    ):
        found.append(
            SchemaViolation(
                path, f"expected at least {minimum} propert(ies), got {len(entries)}"
            )
        )
    for name, property_schema in declared.items():
        if isinstance(name, str) and name in entries:
            found.extend(
                violations(property_schema, entries[name], _child_path(path, name))
            )
    extra = schema.get("additionalProperties")
    for name, entry in entries.items():
        if not isinstance(name, str) or name in declared:
            continue
        if extra is False:
            allowed = ", ".join(sorted(key for key in declared if isinstance(key, str)))
            suffix = f"; allowed properties: {allowed}" if allowed else ""
            found.append(
                SchemaViolation(_child_path(path, name), f"unexpected property{suffix}")
            )
        elif isinstance(extra, Mapping):
            found.extend(violations(extra, entry, _child_path(path, name)))
    return found


def _child_path(path: str, name: str) -> str:
    return name if not path else f"{path}.{name}"


def _matches_type(expected: object, value: object) -> bool:
    if isinstance(expected, list | tuple):
        return any(_matches_type(item, value) for item in expected)
    if not isinstance(expected, str):
        return True
    if expected == "string":
        return isinstance(value, str)
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, list | tuple)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _expected_type_label(expected: object) -> str:
    if isinstance(expected, list | tuple):
        return " or ".join(str(item) for item in expected)
    return str(expected)


def _type_name(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list | tuple):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return type(value).__name__


def _render_value(value: object) -> str:
    return _canonical(value)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)


def _equal(left: object, right: object) -> bool:
    return _canonical(left) == _canonical(right)
