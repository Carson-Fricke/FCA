"""DeepSeek reasoning-field requirements and equivalent tool-schema presentation."""

from collections.abc import Mapping, Sequence
from dataclasses import replace

from thesis_runtime.adapters.providers.families.base import NativeToolsAdapter
from thesis_runtime.adapters.providers.invocations import ProviderMessage


def _patterns(schema: Mapping[str, object]) -> dict[str, object]:
    result = dict(schema)
    # JSON Schema patterns search anywhere. Some DeepSeek endpoints instead
    # constrain the whole generated string, reducing this pattern to one letter.
    # Spell out its existing meaning, including surrounding whitespace/newlines.
    if result.get("pattern") == r"\S":
        result["pattern"] = r"^[\s\S]*\S[\s\S]*$"
    # Traverse schema positions only; defaults, examples, enum and const are data.
    for keyword in (
        "$defs",
        "definitions",
        "properties",
        "patternProperties",
        "dependentSchemas",
    ):
        children = schema.get(keyword)
        if isinstance(children, Mapping):
            result[keyword] = {
                name: _patterns(child) if isinstance(child, Mapping) else child
                for name, child in children.items()
            }
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
        child = schema.get(keyword)
        if isinstance(child, Mapping):
            result[keyword] = _patterns(child)
    for keyword in ("allOf", "anyOf", "oneOf", "prefixItems"):
        children = schema.get(keyword)
        if isinstance(children, list | tuple):
            result[keyword] = [
                _patterns(child) if isinstance(child, Mapping) else child
                for child in children
            ]
    return result


def _explicit_type(schema: Mapping[str, object]) -> dict[str, object]:
    result = dict(schema)
    if "type" not in result and "const" in result:
        value = result["const"]
        if isinstance(value, str):
            result["type"] = "string"
        elif isinstance(value, bool):
            result["type"] = "boolean"
        elif isinstance(value, int):
            result["type"] = "integer"
        elif value is None:
            result["type"] = "null"
    return result


def _parameters(schema: Mapping[str, object]) -> dict[str, object]:
    result = dict(schema)
    alternatives = schema.get("oneOf")
    if not isinstance(alternatives, list | tuple) or not alternatives:
        return result
    if not all(
        isinstance(branch, Mapping)
        and branch.get("type") == "object"
        and isinstance(branch.get("properties"), Mapping)
        for branch in alternatives
    ):
        return result
    branches = [dict(branch["properties"]) for branch in alternatives]
    # Only fields constrained by every alternative can be lifted without changing
    # the accepted arguments. Keep oneOf intact to preserve branch relationships,
    # local reference locations, required fields, and additional-property rules.
    common = set(branches[0]).intersection(*(set(branch) for branch in branches[1:]))
    existing = result.get("properties", {})
    properties = dict(existing) if isinstance(existing, Mapping) else {}
    for name in sorted(common - properties.keys()):
        values = [branch[name] for branch in branches]
        if not all(isinstance(value, Mapping) for value in values):
            continue
        values = [_explicit_type(value) for value in values]
        if all(value == values[0] for value in values):
            properties[name] = values[0]
        else:
            combined: dict[str, object] = {"anyOf": values}
            types = [value.get("type") for value in values]
            if isinstance(types[0], str) and all(kind == types[0] for kind in types):
                combined["type"] = types[0]
            properties[name] = combined
    result["type"] = "object"
    result["properties"] = properties
    return result


class DeepSeekAdapter(NativeToolsAdapter):
    """Preserve tool semantics with explicit union types and nonblank patterns."""

    family = "deepseek"
    version = "3"

    def prepare_messages(
        self, messages: Sequence[ProviderMessage]
    ) -> tuple[ProviderMessage, ...]:
        """DeepSeek requires the reasoning field even when a turn has none."""
        return tuple(
            replace(message, continuation={"reasoning_content": ""})
            if message.role == "assistant" and not message.continuation
            else message
            for message in messages
        )

    def prepare_tools(
        self, tools: Sequence[Mapping[str, object]]
    ) -> tuple[Mapping[str, object], ...]:
        prepared = []
        for tool in tools:
            descriptor = dict(tool)
            schema = descriptor.get("parameters")
            if isinstance(schema, Mapping):
                descriptor["parameters"] = _parameters(_patterns(schema))
            prepared.append(descriptor)
        return tuple(prepared)
