"""Pure parsing of the current single-source Rust strategy design contract."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from thesis.research.models import (
    MARKET_SUBSCRIPTIONS_SCHEMA,
    ResearchModelError,
    validate_market_configuration,
)
from thesis.strategies.bundles import StrategyBundle, StrategyBundleError

_TEXT: dict[str, object] = {"type": "string", "minLength": 1, "pattern": r"\S"}
_IDENTIFIER: dict[str, object] = {
    "type": "string",
    "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$",
}
_VERSION: dict[str, object] = {
    "type": "string",
    "pattern": r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$",
}

STRATEGY_DESIGN_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "strategyId",
        "version",
        "entrypoint",
        "files",
        "tests",
        "harnessConfiguration",
        "exitCriteria",
        "limitations",
    ],
    "properties": {
        "strategyId": _IDENTIFIER,
        "version": _VERSION,
        "parentVersion": {"anyOf": [_VERSION, {"type": "null"}]},
        "entrypoint": {"type": "string", "enum": ["src/lib.rs"]},
        "files": {
            "type": "object",
            "additionalProperties": False,
            "required": ["src/lib.rs"],
            "properties": {
                "src/lib.rs": {
                    **_TEXT,
                    "description": "Complete Rust StrategyProgram source using "
                    "harness_strategy_sdk and export_strategy_program!.",
                },
            },
        },
        "tests": {
            "type": "object",
            "minProperties": 1,
            "additionalProperties": _TEXT,
            "description": "Rust test sources keyed by tests/*.rs paths; "
            "never inline test modules in src/lib.rs.",
        },
        "harnessConfiguration": {
            "type": "object",
            "properties": {
                "subscriptions": MARKET_SUBSCRIPTIONS_SCHEMA,
                "data": {
                    "type": "object",
                    "properties": {"subscriptions": MARKET_SUBSCRIPTIONS_SCHEMA},
                },
            },
        },
        "exitCriteria": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "falsifierId",
                    "observable",
                    "condition",
                    "threshold",
                    "description",
                ],
                "properties": {
                    "falsifierId": _TEXT,
                    "observable": _TEXT,
                    "condition": {"type": "string", "enum": ["lt", "lte", "gt", "gte"]},
                    "threshold": {"type": "number"},
                    "description": _TEXT,
                },
            },
        },
        "limitations": {"type": "array", "items": _TEXT},
        "dependencies": {
            "anyOf": [
                {"type": "object"},
                {"type": "array", "items": _TEXT},
            ]
        },
        "platformVersions": {"type": "object"},
        "riskDeclarations": {"type": "object"},
        "capabilityDeclarations": {"type": "object"},
        "dataManifest": {"type": "object"},
        "backtestManifest": {"type": "object"},
        "evidenceSummary": {"type": "object"},
        "harnessManifestReference": {"anyOf": [_IDENTIFIER, {"type": "null"}]},
    },
}


def _validate_design(schema: Mapping[str, object], value: object, path: str) -> None:
    """Enforce the domain schema without depending on agent-runtime validation."""
    branches = schema.get("anyOf")
    if isinstance(branches, list):
        for branch in branches:
            try:
                _validate_design(branch, value, path)
            except StrategyBundleError:
                continue
            return
        raise StrategyBundleError(f"{path} does not match an accepted shape")
    expected = schema.get("type")
    matches = {
        "object": isinstance(value, Mapping),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "number": isinstance(value, int | float) and not isinstance(value, bool),
        "null": value is None,
    }
    if not matches.get(str(expected), False):
        raise StrategyBundleError(f"{path} must be {expected}")
    if expected == "number" and isinstance(value, float) and not math.isfinite(value):
        raise StrategyBundleError(f"{path} must be finite")
    if "enum" in schema and value not in cast(list[object], schema["enum"]):
        raise StrategyBundleError(f"{path} must be one of {schema['enum']}")
    if isinstance(value, str):
        if len(value) < cast(int, schema.get("minLength", 0)):
            raise StrategyBundleError(f"{path} must be non-empty text")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            raise StrategyBundleError(f"{path} does not match {pattern}")
    elif isinstance(value, list):
        if len(value) < cast(int, schema.get("minItems", 0)):
            raise StrategyBundleError(f"{path} must be non-empty")
        for index, item in enumerate(value):
            _validate_design(
                cast(Mapping[str, object], schema["items"]), item, f"{path}[{index}]"
            )
    elif isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise StrategyBundleError(f"{path} keys must be text")
        properties = cast(Mapping[str, object], schema.get("properties", {}))
        missing = set(cast(list[str], schema.get("required", []))) - value.keys()
        if missing:
            raise StrategyBundleError(
                f"{path} missing required fields: {', '.join(sorted(missing))}"
            )
        if len(value) < cast(int, schema.get("minProperties", 0)):
            raise StrategyBundleError(f"{path} must be a non-empty object")
        additional = schema.get("additionalProperties")
        for key, item in value.items():
            child = properties.get(key, additional)
            if child is False:
                raise StrategyBundleError(f"{path}.{key} is an unsupported field")
            if isinstance(child, Mapping):
                _validate_design(child, item, f"{path}.{key}")


@dataclass(frozen=True, slots=True)
class StrategyDesign:
    """A canonical bundle and design metadata, independent of workspace scope."""

    bundle: StrategyBundle
    parent_version: str | None
    exit_criteria: tuple[dict[str, object], ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StrategyDesign:
        """Parse the closed camel-case design contract, never caller scope IDs."""
        _validate_design(STRATEGY_DESIGN_SCHEMA, value, "strategy")
        source = _text_files(value.get("files"), "files")
        tests = _text_files(value.get("tests"), "tests")
        if set(source) != {"src/lib.rs"}:
            raise StrategyBundleError("current JIT boundary accepts only src/lib.rs")
        for path in tests:
            if not path.startswith("tests/") or not path.endswith(".rs"):
                raise StrategyBundleError("runnable Rust tests must live under tests/")
        parent = cast(str | None, value.get("parentVersion"))
        criteria = cast(list[Mapping[str, object]], value["exitCriteria"])
        limitations = cast(list[str], value["limitations"])
        configuration = cast(Mapping[str, object], value["harnessConfiguration"])
        try:
            validate_market_configuration(configuration)
        except ResearchModelError as error:
            raise StrategyBundleError(str(error)) from error
        if "subscriptions" in configuration:
            configuration = dict(configuration)
            data = configuration.get("data", {})
            if not isinstance(data, Mapping):
                raise StrategyBundleError("harnessConfiguration.data must be an object")
            subscriptions = configuration.pop("subscriptions")
            if "subscriptions" in data and data["subscriptions"] != subscriptions:
                raise StrategyBundleError("conflicting subscription echoes")
            configuration["data"] = {**data, "subscriptions": subscriptions}
        # Include design metadata in the canonical bundle, not only its manifest.
        evidence = cast(Mapping[str, object], value.get("evidenceSummary", {}))
        dependencies = value.get("dependencies", {})
        platform = cast(Mapping[str, object], value.get("platformVersions", {}))
        reference = cast(str | None, value.get("harnessManifestReference"))
        bundle = StrategyBundle.create(
            strategy_id=_text(value.get("strategyId"), "strategyId"),
            version=_text(value.get("version"), "version"),
            source=source,
            tests=tests,
            dependencies=cast(Mapping[str, object] | list[str], dependencies),
            platform_versions=platform,
            harness_configuration=configuration,
            risk_declarations=value.get("riskDeclarations", {}),
            capability_declarations=value.get("capabilityDeclarations", {}),
            data_manifest=value.get("dataManifest", {}),
            backtest_manifest=value.get("backtestManifest", {}),
            evidence_summary={
                "design": {"exitCriteria": criteria, "parentVersion": parent},
                "evidence": dict(evidence),
            },
            limitations=limitations,
            harness_manifest_reference=reference,
        )
        _ = bundle.canonical_bytes  # Reject non-JSON metadata before persistence.
        return cls(bundle, parent, tuple(dict(item) for item in criteria))


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StrategyBundleError(f"{label} must be non-empty text")
    return value


def _text_files(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise StrategyBundleError(f"{label} must be a non-empty path-to-source object")
    result: dict[str, str] = {}
    for path, source in value.items():
        if (
            not isinstance(path, str)
            or not isinstance(source, str)
            or not source.strip()
        ):
            raise StrategyBundleError(
                f"{label} must contain text paths and non-empty Rust source"
            )
        if "\\" in path or not path.endswith(".rs"):
            raise StrategyBundleError("source paths must be POSIX Rust .rs paths")
        result[path] = source
    return result


__all__ = ["STRATEGY_DESIGN_SCHEMA", "StrategyDesign"]
