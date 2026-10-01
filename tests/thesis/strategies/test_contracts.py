"""Closed domain strategy designs and the shared authoring input schema."""

from __future__ import annotations

from typing import cast

import pytest

from thesis.agent.runtime.schema import validate_arguments
from thesis.agent.runtime.strategy_tools import AUTHOR_STRATEGY_SCHEMA
from thesis.strategies.bundles import StrategyBundleError
from thesis.strategies.contracts import STRATEGY_DESIGN_SCHEMA, StrategyDesign


def _design(**overrides: object) -> dict[str, object]:
    return {
        "strategyId": "momentum",
        "version": "1.0.0",
        "entrypoint": "src/lib.rs",
        "files": {
            "src/lib.rs": "// harness_strategy_sdk StrategyProgram export_strategy_program!"
        },
        "tests": {"tests/program.rs": "#[test] fn example() { assert!(true); }"},
        "harnessConfiguration": {},
        "exitCriteria": [
            {
                "falsifierId": "f1",
                "observable": "sharpe",
                "condition": "lt",
                "threshold": 0.0,
                "description": "Exit when the premise fails.",
            }
        ],
        "limitations": [],
        **overrides,
    }


def test_tool_uses_authoritative_domain_schema() -> None:
    properties = cast(dict[str, object], AUTHOR_STRATEGY_SCHEMA["properties"])
    assert properties["strategy"] is STRATEGY_DESIGN_SCHEMA


@pytest.mark.parametrize(
    "dependencies", [{"harness-strategy-sdk": "pinned"}, ["harness-strategy-sdk"]]
)
def test_supported_metadata_is_preserved_and_advertised(dependencies: object) -> None:
    value = _design(
        parentVersion="0.9.0",
        dependencies=dependencies,
        platformVersions={"rust": "pinned"},
        riskDeclarations={"maxPosition": 1},
        capabilityDeclarations={"network": False},
        dataManifest={"symbols": ["SPY"]},
        backtestManifest={"seed": 1},
        evidenceSummary={"reason": "surviving premise"},
        harnessManifestReference="manifest-1",
    )
    assert not validate_arguments(STRATEGY_DESIGN_SCHEMA, value)
    parsed = StrategyDesign.from_mapping(value)
    assert parsed.parent_version == "0.9.0"
    document = parsed.bundle.canonical_document
    for key in (
        "dependencies",
        "platformVersions",
        "riskDeclarations",
        "capabilityDeclarations",
        "dataManifest",
        "backtestManifest",
        "harnessManifestReference",
    ):
        assert document[key] == value[key]
    assert document["evidenceSummary"] == {
        "design": {"exitCriteria": value["exitCriteria"], "parentVersion": "0.9.0"},
        "evidence": value["evidenceSummary"],
    }
    assert (
        parsed.bundle.bundle_sha256
        != StrategyDesign.from_mapping(_design()).bundle.bundle_sha256
    )


def test_optional_nullable_metadata_and_all_operators() -> None:
    for operator in ("lt", "lte", "gt", "gte"):
        value = _design(parentVersion=None, harnessManifestReference=None)
        criteria = cast(list[dict[str, object]], value["exitCriteria"])
        criteria[0]["condition"] = operator
        criteria[0]["threshold"] = -1
        assert not validate_arguments(STRATEGY_DESIGN_SCHEMA, value)
        assert (
            StrategyDesign.from_mapping(value).exit_criteria[0]["condition"] == operator
        )


@pytest.mark.parametrize(
    "field",
    [
        "strategyId",
        "version",
        "entrypoint",
        "files",
        "tests",
        "harnessConfiguration",
        "exitCriteria",
        "limitations",
    ],
)
def test_required_fields_cannot_be_defaulted(field: str) -> None:
    value = _design()
    del value[field]
    assert validate_arguments(STRATEGY_DESIGN_SCHEMA, value)
    with pytest.raises(StrategyBundleError, match=field):
        StrategyDesign.from_mapping(value)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("falsifierId", " "),
        ("observable", 1),
        ("condition", "positive"),
        ("threshold", True),
        ("threshold", "0"),
        ("description", ""),
    ],
)
def test_exit_criterion_types_and_operators(field: str, bad: object) -> None:
    value = _design()
    criterion = cast(list[dict[str, object]], value["exitCriteria"])[0]
    criterion[field] = bad
    assert validate_arguments(STRATEGY_DESIGN_SCHEMA, value)
    with pytest.raises(StrategyBundleError, match=field):
        StrategyDesign.from_mapping(value)


@pytest.mark.parametrize(
    "field", ["falsifierId", "observable", "condition", "threshold", "description"]
)
def test_exit_criteria_require_every_field(field: str) -> None:
    value = _design()
    del cast(list[dict[str, object]], value["exitCriteria"])[0][field]
    assert validate_arguments(STRATEGY_DESIGN_SCHEMA, value)
    with pytest.raises(StrategyBundleError, match=field):
        StrategyDesign.from_mapping(value)


@pytest.mark.parametrize("criteria", [[], [{}], ["not an object"], None])
def test_exit_criteria_are_nonempty_objects(criteria: object) -> None:
    value = _design(exitCriteria=criteria)
    assert validate_arguments(STRATEGY_DESIGN_SCHEMA, value)
    with pytest.raises(StrategyBundleError, match="exitCriteria"):
        StrategyDesign.from_mapping(value)


@pytest.mark.parametrize("threshold", [float("nan"), float("inf"), float("-inf")])
def test_domain_rejects_nonfinite_exit_thresholds(threshold: float) -> None:
    value = _design()
    cast(list[dict[str, object]], value["exitCriteria"])[0]["threshold"] = threshold
    with pytest.raises(StrategyBundleError, match="threshold must be finite"):
        StrategyDesign.from_mapping(value)


def test_closed_criterion_and_design_reject_extra_fields() -> None:
    value = _design()
    cast(list[dict[str, object]], value["exitCriteria"])[0]["extra"] = "unknown"
    assert validate_arguments(STRATEGY_DESIGN_SCHEMA, value)
    with pytest.raises(StrategyBundleError, match="extra"):
        StrategyDesign.from_mapping(value)
    for field in ("workspaceId", "executionId", "artifactPath", "unknown"):
        with pytest.raises(StrategyBundleError, match=field):
            StrategyDesign.from_mapping(_design(**{field: "forged"}))


@pytest.mark.parametrize(
    "field,bad",
    [
        ("parentVersion", "latest"),
        ("dependencies", [" "]),
        ("platformVersions", []),
        ("riskDeclarations", "risk"),
        ("capabilityDeclarations", None),
        ("dataManifest", []),
        ("backtestManifest", True),
        ("evidenceSummary", "evidence"),
        ("harnessManifestReference", "unsafe/path"),
        ("limitations", [" "]),
        ("entrypoint", "src/main.rs"),
        ("files", {"src/lib.rs": "source", "src/helper.rs": "source"}),
        ("tests", {}),
    ],
)
def test_schema_and_domain_reject_invalid_metadata(field: str, bad: object) -> None:
    value = _design(**{field: bad})
    assert validate_arguments(STRATEGY_DESIGN_SCHEMA, value)
    with pytest.raises(StrategyBundleError, match=field):
        StrategyDesign.from_mapping(value)


@pytest.mark.parametrize(
    "path", ["src/tests.rs", "tests/example.py", "tests/../example.rs"]
)
def test_domain_keeps_rust_tests_confined(path: str) -> None:
    with pytest.raises(StrategyBundleError):
        StrategyDesign.from_mapping(_design(tests={path: "#[test] fn example() {}"}))
