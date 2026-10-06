"""Serving composes ID-owned programs instead of inline MCP execution tools."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from thesis.agent.harness_tools import (
    HarnessToolProvider,
    HarnessToolResult,
    HarnessToolSpec,
)
from thesis.chat import ChatService
from thesis.chat.contracts import ChatMessage as DurableChatMessage
from thesis.http.serving import build_serving_stack
from thesis.providers.invocations import ChatMessage, ModelCompletion, Role, ToolCall
from thesis.providers.openrouter import OpenRouterProvider
from thesis.workspaces.local import LocalWorkspaceProvider
from thesis.workspaces.manifest import WorkspaceManifest, load_workspace_manifest
from thesis.workspaces.programs import WorkspacePrograms


def test_serving_authors_template_validates_and_runs_by_id(tmp_path: Path) -> None:
    repositories = LocalWorkspaceProvider(tmp_path / "workspaces")
    manifest = load_workspace_manifest("config/thesis/workspace.yaml").model_dump(
        mode="json"
    )
    manifest.update(
        workspace_id="workspace-one",
        repository_identity={
            "provider": "local",
            "repository_id": "workspace-one",
            "default_branch": "main",
        },
    )
    repositories.create(WorkspaceManifest.model_validate(manifest))
    calls: list[tuple[str, Mapping[str, object]]] = []
    catalogs: list[Sequence[Mapping[str, object]]] = []

    class Harness(HarnessToolProvider):
        validation_identity = "compiler-one"

        def current_server_info(self) -> Mapping[str, object]:
            local = WorkspacePrograms(repositories)
            version, abi = local._sdk_metadata(local.sdk_root)
            return {
                "serverInfo": {
                    "sdkIdentity": local._sdk_identity(),
                    "sdkVersion": version,
                    "sdkAbiVersion": abi,
                    "validationIdentity": self.validation_identity,
                }
            }

        def __init__(self) -> None:
            pass

        def list_tools(self) -> tuple[HarnessToolSpec, ...]:
            return tuple(
                HarnessToolSpec(
                    name,
                    "inline native",
                    {"type": "object", "properties": {"source": {"type": "string"}}},
                )
                for name in (
                    "validate_study",
                    "run_study",
                    "run_backtest",
                    "validate_strategy",
                )
            )

        def call_tool(
            self, name: str, arguments: Mapping[str, object] | None = None
        ) -> HarnessToolResult:
            calls.append((name, arguments or {}))
            return HarnessToolResult(
                name, True, {"valid": True, "run_id": "fake-study-run"}
            )

    class Provider:
        def complete(
            self,
            *,
            messages: Sequence[ChatMessage],
            role: Role,
            tools: Sequence[Mapping[str, object]] | None = None,
            response_format: Mapping[str, object] | None = None,
        ) -> ModelCompletion:
            catalogs.append(tools or ())
            index = len(catalogs) - 1
            arguments = (
                '{"program_id":"alpha","template":"basic","configuration":'
                '{"start":"2024-01-01T00:00:00Z","end":"2024-01-02T00:00:00Z",'
                '"subscriptions":[{"data_type":"bars","symbols":["MSFT"],"interval":"1d"}]}}',
                '{"program_id":"alpha"}',
                '{"program_id":"alpha"}',
            )
            names = ("author_study", "validate_study", "run_study")
            return ModelCompletion(
                content="Finished" if index == 3 else "Working",
                model="fake",
                provider="fake",
                role=role,
                prompt_tokens=0,
                completion_tokens=0,
                tool_calls=()
                if index == 3
                else (ToolCall(f"call-{index}", names[index], arguments[index]),),
            )

    chat = ChatService()
    thread = chat.create_chat(
        "workspace-one", title="Programs", owner_actor_id="operator"
    )
    chat.send_message(
        "workspace-one",
        thread.chat_id,
        actor_id="operator",
        body="Author and run a study",
    )
    stack = build_serving_stack(
        chat_service=chat,
        model_provider=cast(OpenRouterProvider, Provider()),
        harness_tools=cast(HarnessToolProvider, Harness()),
        workspace_repositories=repositories,
        detach=None,
    )
    assert stack.conversations is not None
    reply = stack.conversations.root("workspace-one", thread.chat_id).run_turn()
    assert isinstance(reply, DurableChatMessage) and reply.body == "Finished"
    assert [name for name, _ in calls] == ["validate_study", "run_study"]
    for catalog in catalogs:
        functions = {
            cast(dict[str, object], tool["function"])["name"]: cast(
                dict[str, object], tool["function"]
            )
            for tool in catalog
        }
        for name in (
            "validate_study",
            "run_study",
            "run_backtest",
            "validate_strategy",
        ):
            schema = cast(dict[str, object], functions[name]["parameters"])
            assert schema["required"] == ["program_id"]
            assert schema["additionalProperties"] is False
            assert "source" not in cast(dict[str, object], schema["properties"])
    validated = cast(dict[str, object], calls[0][1]["model"])
    document = cast(dict[str, object], calls[1][1]["config"])
    instance = cast(list[dict[str, object]], document["strategy_instances"])[0]
    assert (
        cast(dict[str, object], instance["parameters"])["source"] == validated["source"]
    )
    assert (
        cast(dict[str, object], document["data"])["symbols"]
        == instance["symbols"]
        == ["MSFT"]
    )
    assert '"MSFT".into()' in str(validated["source"])
    assert document["data"] == {
        "subscriptions": cast(dict[str, object], validated["harnessConfiguration"])[
            "subscriptions"
        ],
        "symbols": ["MSFT"],
    }
