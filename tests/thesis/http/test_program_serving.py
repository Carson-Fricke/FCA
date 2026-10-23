"""Serving composes ID-owned programs instead of inline MCP execution tools."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import pytest
from tests.thesis.workspaces._support import MemoryGitFactory, Repositories

from thesis.agent.harness_tools import (
    HarnessToolProvider,
    HarnessToolResult,
    HarnessToolSpec,
)
from thesis.application.composition import build_serving_stack
from thesis.workspaces import programs as program_module
from thesis.workspaces.manifest import WorkspaceManifest, load_workspace_manifest
from thesis.workspaces.programs import WorkspacePrograms
from thesis_runtime.adapters.providers.invocations import (
    ChatMessage,
    ModelCompletion,
    Role,
    ToolCall,
)
from thesis_runtime.adapters.providers.openrouter import OpenRouterProvider
from thesis_runtime.contracts.records import ChatMessage as DurableChatMessage
from thesis_runtime.conversations import ChatService
from thesis_runtime.workflows.execution_store import InMemoryExecutionStore


def _sdk_checkout(source: Path, destination: Path, newlines: str) -> Path:
    for relative in (
        "harness/strategy-sdk",
        "harness/contracts",
        "harness/num",
        "lake/contracts",
    ):
        crate = source.parents[1] / relative
        for path in [crate / "Cargo.toml", *sorted((crate / "src").rglob("*.rs"))]:
            target = destination / relative / path.relative_to(crate)
            target.parent.mkdir(parents=True, exist_ok=True)
            data = path.read_bytes().replace(b"\r\n", b"\n")
            if newlines == "crlf":
                data = data.replace(b"\n", b"\r\n")
            elif newlines == "mixed":
                data = b"".join(
                    line.replace(b"\n", b"\r\n") if index % 2 else line
                    for index, line in enumerate(data.splitlines(keepends=True))
                )
            target.write_bytes(data)
    return destination / "harness/strategy-sdk"


@pytest.mark.parametrize("newlines", ["lf", "crlf", "mixed"])
def test_serving_authors_template_validates_and_runs_by_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, newlines: str
) -> None:
    git_factory = MemoryGitFactory()
    repositories = Repositories(tmp_path / "workspaces", git_factory)
    # Keep the real serving composition; replace only its external Git adapter.
    # Real commit/read/dirty-worktree fencing remains in test_programs.py.
    monkeypatch.setattr(program_module, "GitCoordinator", git_factory)
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
    source_sdk = program_module._PACKAGED_SDK_ROOT
    serving = WorkspacePrograms(
        repositories,
        sdk_root=_sdk_checkout(source_sdk, tmp_path / "serving", "lf"),
    )
    sdk_version, sdk_abi = serving._sdk_metadata(serving.sdk_root)
    serving_identity = serving._sdk_identity()
    monkeypatch.setattr(
        program_module,
        "_PACKAGED_SDK_ROOT",
        _sdk_checkout(source_sdk, tmp_path / "local", newlines),
    )

    class Harness(HarnessToolProvider):
        validation_identity = "compiler-one"

        def current_server_info(self) -> Mapping[str, object]:
            return {
                "serverInfo": {
                    "sdkIdentity": serving_identity,
                    "sdkVersion": sdk_version,
                    "sdkAbiVersion": sdk_abi,
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
                    "run_backtest_batch",
                    "validate_strategy",
                )
            )

        def call_tool(
            self, name: str, arguments: Mapping[str, object] | None = None
        ) -> HarnessToolResult:
            calls.append((name, arguments or {}))
            structured: dict[str, object] = {"valid": True}
            config = (arguments or {}).get("config")
            job_id = config.get("job_id") if isinstance(config, Mapping) else None
            structured["job_id"] = job_id if isinstance(job_id, str) else "job-one"
            return HarnessToolResult(name, True, structured)

    class Provider:
        model = "test/model"

        def stream(self, *, message_id=None, **kwargs):
            from tests.thesis_runtime.provider_fakes import completion_frames

            yield from completion_frames(self.complete(**kwargs), message_id)

        streaming = True

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
        execution_store=InMemoryExecutionStore(),
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
            properties = cast(dict[str, object], schema["properties"])
            assert "source" not in properties
            if name == "run_backtest":
                assert set(properties) == {"program_id", "parameters"}
            else:
                assert set(properties) == {"program_id"}
        batch = cast(dict[str, object], functions["run_backtest_batch"]["parameters"])
        assert batch["required"] == ["program_id", "assignments"]
        assert batch["additionalProperties"] is False
        batch_properties = cast(dict[str, object], batch["properties"])
        assert set(batch_properties) == {"program_id", "assignments", "label"}
        assignments = cast(dict[str, object], batch_properties["assignments"])
        assert assignments["minItems"] == 1 and "maxItems" not in assignments
        assert "source" not in batch_properties
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
