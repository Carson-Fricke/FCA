"""Authoring is an injected capability, never inherited authority."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import cast

import pytest

from thesis.agent.runtime.context import ToolContext
from thesis.agent.runtime.registry import THESIS_TOOL_NAMES, build_session_registry


def _context() -> ToolContext:
    return ToolContext(
        workspace_id="workspace-1",
        chat_id="chat-1",
        conversation_id="design-1",
        sub_agent_id="design-1",
        actor_id="operator-1",
        workflow_execution_id="execution-1",
        allowed_tools=("author_strategy",),
    )


def _arguments() -> dict[str, object]:
    return {
        "name": "Momentum",
        "symbol": "SPY",
        "description": "Trade the surviving momentum premise.",
        "strategy": {
            "strategyId": "momentum",
            "version": "1.0.0",
            "entrypoint": "src/lib.rs",
            "files": {
                "src/lib.rs": "// harness_strategy_sdk StrategyProgram export_strategy_program!"
            },
            "tests": {"tests/momentum.rs": "// Rust tests"},
            "harnessConfiguration": {
                "subscriptions": [{"data_type": "quote", "symbols": ["SPY"]}]
            },
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
        },
    }


def _receipt() -> dict[str, object]:
    return {
        "workspaceId": "workspace-1",
        "executionId": "execution-1",
        "publicationId": "publication-1",
        "gitCommit": "b" * 40,
        "strategyId": "momentum",
        "version": "1.0.0",
        "bundleSha256": "a" * 64,
        "artifactPath": "strategies/momentum/1.0.0",
        "status": "draft",
        "validation": {"accepted": True, "diagnostics": []},
    }


class _Authoring:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.result = _receipt()

    def author(
        self,
        *,
        workspace_id: str,
        execution_id: str,
        strategy: Mapping[str, object],
        name: str,
        symbol: str,
        description: str,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "workspace_id": workspace_id,
                "execution_id": execution_id,
                "strategy": strategy,
                "name": name,
                "symbol": symbol,
                "description": description,
            }
        )
        return self.result


@pytest.mark.parametrize("use_callable", [False, True])
def test_authoring_binds_attribution_and_returns_canonical_result(
    use_callable: bool,
) -> None:
    service = _Authoring()
    registry = build_session_registry(
        context=_context(),
        strategy_authoring=service.author if use_callable else service,
    )
    result = registry.dispatch("author_strategy", _arguments())
    assert result.ok and not result.terminal
    assert result.structured == _receipt()
    assert json.loads(result.as_content()) == {
        "ok": True,
        "structured": _receipt(),
        "text": "",
    }
    assert service.calls == [
        {
            "workspace_id": "workspace-1",
            "execution_id": "execution-1",
            **_arguments(),
        }
    ]
    assert "author_strategy" in THESIS_TOOL_NAMES
    assert [
        cast(Mapping[str, object], tool["function"])["name"]
        for tool in registry.catalog()
    ] == ["author_strategy"]


def test_inherited_grant_without_injection_offers_no_authority() -> None:
    helper = replace(
        _context(),
        conversation_id="helper",
        sub_agent_id="helper",
        parent_sub_agent_id="design-1",
    )
    registry = build_session_registry(context=helper)
    assert registry.catalog() == ()
    assert not registry.dispatch("author_strategy", _arguments()).ok


def test_injection_still_respects_empty_policy() -> None:
    service = _Authoring()
    registry = build_session_registry(
        context=replace(_context(), allowed_tools=()),
        strategy_authoring=service,
    )
    assert registry.catalog() == ()
    result = registry.dispatch("author_strategy", _arguments())
    assert result.failure is not None and result.failure["kind"] == "denied"
    assert not service.calls


@pytest.mark.parametrize(
    "key",
    [
        "workspace_id",
        "workspaceId",
        "execution_id",
        "executionId",
        "artifactPath",
        "root",
    ],
)
def test_model_attribution_is_rejected_before_authoring(key: str) -> None:
    service = _Authoring()
    registry = build_session_registry(context=_context(), strategy_authoring=service)
    result = registry.dispatch("author_strategy", {**_arguments(), key: "forged"})
    assert result.failure is not None and result.failure["kind"] == "invalid_arguments"
    assert not service.calls


def test_execution_is_required_without_domain_phase_checks() -> None:
    service = _Authoring()
    registry = build_session_registry(
        context=replace(_context(), workflow_execution_id=None),
        strategy_authoring=service,
    )
    assert not registry.dispatch("author_strategy", _arguments()).ok
    assert not service.calls


@pytest.mark.parametrize(
    "result",
    [
        {
            "validation": {
                "accepted": False,
                "diagnostics": ["compiler rejected source"],
            }
        },
        {"strategyId": "momentum"},
    ],
)
def test_rejections_and_missing_receipts_are_not_success(
    result: dict[str, object],
) -> None:
    service = _Authoring()
    service.result = result
    registry = build_session_registry(context=_context(), strategy_authoring=service)
    execution = registry.dispatch("author_strategy", _arguments())
    assert not execution.ok
    assert execution.structured == result


def test_authoring_fault_is_a_normal_tool_failure() -> None:
    def author(**kwargs: object) -> dict[str, object]:
        raise RuntimeError("publish failed")

    registry = build_session_registry(context=_context(), strategy_authoring=author)
    result = registry.dispatch("author_strategy", _arguments())
    assert not result.ok
    assert result.failure is not None and result.failure["message"] == "publish failed"


def test_non_rust_entrypoint_and_extra_source_are_rejected() -> None:
    service = _Authoring()
    registry = build_session_registry(context=_context(), strategy_authoring=service)
    arguments = _arguments()
    strategy = arguments["strategy"]
    assert isinstance(strategy, dict)
    strategy["entrypoint"] = "src/strategy.js"
    strategy["files"] = {"src/lib.rs": "source", "src/strategy.js": "source"}
    assert not registry.dispatch("author_strategy", arguments).ok
    assert not service.calls
