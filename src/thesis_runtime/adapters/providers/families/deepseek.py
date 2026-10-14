"""Expose parameter types to DeepSeek endpoints that inspect root properties."""

from collections.abc import Mapping, Sequence

from thesis_runtime.adapters.providers.families.base import NativeToolsAdapter


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
    """Keep canonical names/arguments while making union parameter types explicit."""

    family = "deepseek"

    def prepare_tools(
        self, tools: Sequence[Mapping[str, object]]
    ) -> tuple[Mapping[str, object], ...]:
        prepared = []
        for tool in tools:
            descriptor = dict(tool)
            schema = descriptor.get("parameters")
            if isinstance(schema, Mapping):
                descriptor["parameters"] = _parameters(schema)
            prepared.append(descriptor)
        return tuple(prepared)
