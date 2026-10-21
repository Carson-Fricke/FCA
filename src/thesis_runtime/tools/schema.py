"""Pure Draft 2020-12 validation shared by tool dispatch and workflows.

Local references are supported; validation never retrieves remote documents.
Invalid schemas fail at catalog construction.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from jsonschema.protocols import Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012


@dataclass(frozen=True, slots=True)
class SchemaViolation:
    """One argument violation with a JSON path and repair detail."""

    path: str
    message: str

    def render(self) -> str:
        return self.message if not self.path else f"{self.path}: {self.message}"


class _JsonValueError(ValueError):
    def __init__(self, path: str, message: str) -> None:
        super().__init__(message)
        self.path = path


def _json(value: object, path: str = "") -> object:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise _JsonValueError(path, "JSON object keys must be strings")
        return {
            key: _json(item, f"{path}.{key}" if path else key)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_json(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise _JsonValueError(path, "value is not finite JSON")


@lru_cache(maxsize=256)
def _validator(encoded: str) -> Validator:
    normalized = json.loads(encoded)
    Draft202012Validator.check_schema(normalized)
    if isinstance(normalized, dict):
        dialect = normalized.get("$schema")
        if dialect not in (None, "https://json-schema.org/draft/2020-12/schema"):
            raise ValueError(f"unsupported JSON Schema dialect: {dialect}")

    # Walk only schema-bearing keywords; enum/const/example objects are data.
    def references(node: object):
        if not isinstance(node, dict):
            return
        for keyword in ("$ref", "$dynamicRef"):
            if keyword in node:
                reference = node[keyword]
                if not isinstance(reference, str) or not reference.startswith("#"):
                    raise ValueError("tool schema references must be local fragments")
                yield reference
        if "$id" in node:
            raise ValueError("tool schemas use local references without $id resources")
        for keyword in (
            "$defs",
            "definitions",
            "properties",
            "patternProperties",
            "dependentSchemas",
        ):
            children = node.get(keyword, {})
            if isinstance(children, dict):
                for child in children.values():
                    yield from references(child)
        for keyword in (
            "items",
            "contains",
            "additionalProperties",
            "unevaluatedProperties",
            "unevaluatedItems",
            "propertyNames",
            "not",
            "if",
            "then",
            "else",
            "contentSchema",
        ):
            yield from references(node.get(keyword))
        for keyword in ("allOf", "anyOf", "oneOf", "prefixItems"):
            for child in node.get(keyword, []):
                yield from references(child)

    resolver = (
        Registry()
        .with_resource(
            "", Resource.from_contents(normalized, default_specification=DRAFT202012)
        )
        .resolver()
    )
    for reference in references(normalized):
        resolver.lookup(reference)
    # Empty registry has no retriever: validation is pure even for untrusted refs.
    return Draft202012Validator(normalized, registry=Registry())


def check_schema(schema: object) -> None:
    """Reject invalid schemas before advertising a tool."""
    _validator(json.dumps(_json(schema), sort_keys=True, allow_nan=False))


def violations(
    schema: object, value: object, path: str = ""
) -> tuple[SchemaViolation, ...]:
    """Validate finite JSON without remote-reference retrieval."""
    check_schema(schema)
    try:
        instance = _json(value, path)
    except _JsonValueError as error:
        return (SchemaViolation(error.path, str(error)),)
    validator = _validator(json.dumps(_json(schema), sort_keys=True, allow_nan=False))
    found: list[SchemaViolation] = []

    def record(error: ValidationError) -> None:
        if error.context:
            for child in error.context:
                record(child)
            return
        location = path
        for part in error.absolute_path:
            if isinstance(part, int):
                location += f"[{part}]"
            else:
                location = f"{location}.{part}" if location else part
        # jsonschema attaches propertyNames errors to the containing object.
        # Report the offending key itself, just as additionalProperties does.
        container = instance
        for part in error.absolute_path:
            if isinstance(container, dict):
                container = container[part]
            elif isinstance(container, list) and isinstance(part, int):
                container = container[part]
            else:
                break
        if (
            "propertyNames" in error.absolute_schema_path
            and isinstance(container, dict)
            and isinstance(error.instance, str)
            and error.instance in container
        ):
            location = f"{location}.{error.instance}" if location else error.instance
        # jsonschema often prefixes diagnostics with the entire offending value.
        # Never let a large argument displace the constraint needed to repair it.
        actual = error.instance
        if isinstance(actual, str):
            received = f"string of length {len(actual)}: {actual[:96]!r}"
        elif isinstance(actual, list | dict):
            received = f"{type(actual).__name__} of length {len(actual)}"
        else:
            received = repr(actual)[:96]
        expected = json.dumps(error.validator_value, ensure_ascii=False)
        detail = f"constraint {error.validator} requires {expected[:256]}; received {received}"
        if error.validator == "required":
            for key in error.validator_value:
                if (
                    key not in error.instance
                    and error.message == f"{key!r} is a required property"
                ):
                    found.append(
                        SchemaViolation(
                            f"{location}.{key}" if location else key,
                            "required property is missing",
                        )
                    )
            return
        if (
            error.validator == "additionalProperties"
            and error.validator_value is False
            and isinstance(error.schema, Mapping)
        ):
            properties = error.schema.get("properties", {})
            patterns = error.schema.get("patternProperties", {})
            for key in error.instance:
                if key not in properties and not any(
                    re.search(pattern, key) for pattern in patterns
                ):
                    found.append(
                        SchemaViolation(
                            f"{location}.{key}" if location else key,
                            "additional property is not allowed",
                        )
                    )
            return
        if error.validator == "type":
            value = error.instance
            actual = (
                "null"
                if value is None
                else "boolean"
                if isinstance(value, bool)
                else "string"
                if isinstance(value, str)
                else "object"
                if isinstance(value, dict)
                else "array"
                if isinstance(value, list)
                else "integer"
                if isinstance(value, int)
                else "number"
            )
            detail = f"expected {error.validator_value}; received {actual}"
        elif error.validator in {"oneOf", "anyOf"}:
            detail = f"arguments must satisfy {error.validator} constraints"
        elif error.validator in {"maxLength", "minLength"}:
            detail = f"{error.validator} is {error.validator_value}; received length {len(error.instance)}"
        elif error.validator == "enum":
            detail = (
                f"{repr(error.instance)[:96]} is not one of {expected[:256]} (enum)"
            )
        elif error.validator == "minItems":
            detail = (
                ("array should be non-empty; " if error.validator_value == 1 else "")
                + f"minItems is {error.validator_value}; received length {len(error.instance)}"
            )
        found.append(SchemaViolation(location, detail[:512]))

    for error in validator.iter_errors(instance):
        record(error)
    return tuple(dict.fromkeys(found))


def validate_arguments(
    schema: object, arguments: Mapping[str, object]
) -> tuple[SchemaViolation, ...]:
    """Validate one tool call against its advertised input contract."""
    return violations(schema, arguments)


def render_violations(misses: Sequence[SchemaViolation]) -> str:
    """Join violations into caller-readable repair detail."""
    return "; ".join(miss.render() for miss in misses)
