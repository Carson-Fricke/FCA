"""ID-only program tools bind workspace authority out of band."""

from collections.abc import Mapping
from typing import cast

from thesis.agent.runtime.context import ToolContext
from thesis.agent.runtime.program_tools import (
    PROGRAM_TOOL_NAMES,
    program_registered_tools,
)
from thesis.workspaces.programs import ProgramPublicationError, WorkspacePrograms

CONFIG: dict[str, object] = {
    "start": "2024-01-01T00:00:00Z",
    "end": "2024-01-02T00:00:00Z",
    "subscriptions": [{"data_type": "bars", "symbols": ["MSFT"], "interval": "1d"}],
}


class Service:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def author(self, *args: object, **kwargs: object) -> dict[str, object]:
        self.calls.append((args, kwargs))
        return {"programId": "alpha", "version": "a" * 64}

    def list(self, *args: object, **kwargs: object) -> dict[str, object]:
        self.calls.append((args, kwargs))
        return {"programs": [], "count": 0}

    update = read = validate = run = publish = author

    def read_sdk_reference(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self.author(*args, **kwargs)


def test_catalog_and_no_attribution_or_inline_execution_source() -> None:
    service = Service()
    context = ToolContext("trusted-workspace", "chat", "conversation", "actor")
    tools = program_registered_tools(cast(WorkspacePrograms, service), context)
    assert (
        tuple(
            cast(Mapping[str, object], tool.spec["function"])["name"] for tool in tools
        )
        == PROGRAM_TOOL_NAMES
    )
    for tool in tools:
        function = cast(Mapping[str, object], tool.spec["function"])
        schema = cast(Mapping[str, object], function["parameters"])
        properties = cast(Mapping[str, object], schema["properties"])
        assert schema["additionalProperties"] is False
        assert "workspace_id" not in properties and "execution_id" not in properties
        name = cast(str, function["name"])
        if name.startswith(("validate_", "run_")):
            assert set(properties) == {"program_id"}
        if name.startswith("list_"):
            assert set(properties) == {"prefix", "limit"}
            assert schema["required"] == []
        assert not tool.handler({"program_id": "alpha", "workspace_id": "untrusted"}).ok
    assert not service.calls


def test_template_authoring_and_configuration_only_update() -> None:
    service = Service()
    tools = program_registered_tools(
        cast(WorkspacePrograms, service),
        ToolContext("workspace", "chat", "conversation", "actor"),
    )
    author, update = tools[:2]
    assert author.handler(
        {"program_id": "alpha", "template": "basic", "configuration": CONFIG}
    ).ok
    assert service.calls[-1][1]["template"] == "basic"
    for invalid in (
        {"program_id": "alpha", "configuration": CONFIG},
        {
            "program_id": "alpha",
            "source": "source",
            "template": "basic",
            "configuration": CONFIG,
        },
        {
            "program_id": "alpha",
            "template": "basic",
            "configuration": {"start": "no offset", "end": "bad"},
        },
    ):
        count = len(service.calls)
        assert not author.handler(invalid).ok
        assert len(service.calls) == count
    assert update.handler(
        {"program_id": "alpha", "expected_version": "a" * 64, "configuration": CONFIG}
    ).ok
    assert service.calls[-1][1]["expected"] is None
    assert not update.handler(
        {"program_id": "alpha", "expected_version": "a" * 64, "expected": "unpaired"}
    ).ok


def test_publication_failure_exposes_only_compact_known_receipt() -> None:
    class FailingService(Service):
        def author(self, *args: object, **kwargs: object) -> dict[str, object]:
            raise ProgramPublicationError({"programId": "alpha", "gitCommit": "a" * 40})

    tools = program_registered_tools(
        cast(WorkspacePrograms, FailingService()),
        ToolContext("workspace", "chat", "conversation", "actor"),
    )
    result = tools[0].handler(
        {"program_id": "alpha", "template": "basic", "configuration": CONFIG}
    )
    assert not result.ok and result.structured == {
        "programId": "alpha",
        "gitCommit": "a" * 40,
    }
    assert result.failure and result.failure["executionCertainty"] == "local_commit"
    assert "publish_study" in str(result.failure["retryGuidance"])


def test_all_operations_dispatch_with_trusted_workspace() -> None:
    service = Service()
    context = ToolContext("trusted-workspace", "chat", "conversation", "actor")
    tools = program_registered_tools(cast(WorkspacePrograms, service), context)
    for tool in tools:
        name = cast(str, cast(Mapping[str, object], tool.spec["function"])["name"])
        args: dict[str, object] = {"program_id": "alpha"}
        if name.startswith("list_"):
            args = {}
        if name.startswith("author_"):
            args["source"] = "Rust source"
            args["configuration"] = {
                "start": "2024-01-01T00:00:00Z",
                "end": "2024-01-02T00:00:00Z",
                "subscriptions": [
                    {"data_type": "bars", "symbols": ["MSFT"], "interval": "1d"}
                ],
            }
        if name.startswith("update_"):
            args.update(
                {
                    "expected_version": "a" * 64,
                    "expected": "before",
                    "replacement": "after",
                }
            )
        if name.startswith("publish_"):
            args["commit"] = "a" * 40
        if name == "read_sdk_reference":
            args = {"topic": "study"}
        assert tool.handler(args).ok
        positional, _ = service.calls[-1]
        if name.startswith("list_"):
            assert positional[:2] == (
                "trusted-workspace",
                {
                    "studies": "study",
                    "strategies": "strategy",
                    "indicators": "indicator",
                }[name.split("_")[1]],
            )
        elif name != "read_sdk_reference":
            assert positional[:3] == (
                "trusted-workspace",
                "strategy" if name == "run_backtest" else name.split("_")[1],
                "alpha",
            )


def test_denied_tools_never_dispatch() -> None:
    service = Service()
    context = ToolContext(
        "workspace", "chat", "conversation", "actor", allowed_tools=()
    )
    tools = program_registered_tools(cast(WorkspacePrograms, service), context)
    assert not tools[0].handler({"program_id": "alpha", "source": "Rust"}).ok
    assert not service.calls


def test_publish_tools_require_pinned_commit_and_never_execute() -> None:
    service = Service()
    tools = program_registered_tools(
        cast(WorkspacePrograms, service),
        ToolContext("trusted-workspace", "chat", "conversation", "actor"),
    )
    for tool in tools:
        if not tool.name.startswith("publish_"):
            continue
        assert not tool.handler({"program_id": "alpha"}).ok
        assert not tool.handler({"program_id": "alpha", "commit": "HEAD"}).ok
        assert not tool.handler(
            {"program_id": "alpha", "commit": "a" * 40, "source": "override"}
        ).ok
        assert tool.handler({"program_id": "alpha", "commit": "a" * 40}).ok
        assert service.calls[-1] == (
            ("trusted-workspace", tool.name.split("_")[1], "alpha", "a" * 40),
            {},
        )
