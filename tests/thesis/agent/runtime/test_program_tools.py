"""ID-only program tools bind workspace authority out of band."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import cast

from thesis.agent.runtime.context import ToolContext
from thesis.agent.runtime.program_tools import (
    PROGRAM_TOOL_NAMES,
    program_registered_tools,
)
from thesis.agent.runtime.session import RegisteredTool
from thesis.experiments.mandate import (
    DataWindowPolicy,
    LaunchPartition,
    derive_mandate,
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

    def run_batch(self, *args: object, **kwargs: object) -> dict[str, object]:
        self.calls.append((args, kwargs))
        return {
            "ok": True,
            "batchId": "batch-alpha-" + "b" * 12,
            "programId": "alpha",
            "version": "a" * 64,
            "points": [],
        }

    def run_null_batch(self, *args: object, **kwargs: object) -> dict[str, object]:
        self.calls.append((args, kwargs))
        return {
            "ok": True,
            "batchId": "null-alpha-" + "c" * 12,
            "programId": "alpha",
            "version": "a" * 64,
            "points": [],
        }

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
        if name.startswith("validate_"):
            assert set(properties) == {"program_id"}
        if name == "run_study":
            assert set(properties) == {"program_id"}
        if name == "run_backtest":
            assert set(properties) == {"program_id", "parameters"}
        if name == "run_backtest_batch":
            assert set(properties) == {"program_id", "assignments", "label"}
        if name == "run_null_batch":
            assert set(properties) == {
                "program_id",
                "parameters",
                "block_size",
                "mode",
                "offset",
                "chunk_size",
                "seeds",
                "label",
            }
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
        if name == "run_backtest":
            args["parameters"] = {"lookback": 20}
        if name == "run_backtest_batch":
            args["assignments"] = [{"lookback": 20}, {"lookback": 40}]
            args["label"] = "grid"
        if name == "run_null_batch":
            args.update(
                {
                    "parameters": {"lookback": 20},
                    "mode": "block_permutation",
                    "block_size": 5,
                    "seeds": [1, 2],
                    "label": "seeds",
                }
            )
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
                "strategy"
                if name in ("run_backtest", "run_backtest_batch", "run_null_batch")
                else name.split("_")[1],
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


def _tool_map(
    service: Service,
    *,
    data_window: DataWindowPolicy | None = None,
    job_retention: Callable[
        [ToolContext, str, str, Mapping[str, object]],
        Callable[[str, str | None], None],
    ]
    | None = None,
) -> dict[str, RegisteredTool]:
    return {
        cast(str, cast(Mapping[str, object], tool.spec["function"])["name"]): tool
        for tool in program_registered_tools(
            cast(WorkspacePrograms, service),
            ToolContext("trusted-workspace", "chat", "conversation", "actor"),
            data_window=data_window,
            job_retention=job_retention,
        )
    }


def _properties(tool: RegisteredTool) -> Mapping[str, object]:
    function = cast(Mapping[str, object], tool.spec["function"])
    schema = cast(Mapping[str, object], function["parameters"])
    return cast(Mapping[str, object], schema["properties"])


def test_strategy_tools_expose_parameter_declaration_only_for_strategies() -> None:
    service = Service()
    tools = _tool_map(service)
    assert "parameter_declaration" in _properties(tools["author_strategy"])
    assert "parameter_declaration" in _properties(tools["update_strategy"])
    assert "parameter_declaration" not in _properties(tools["author_study"])
    assert "parameter_declaration" not in _properties(tools["author_indicator"])
    assert "parameter_declaration" not in _properties(tools["update_study"])
    assert "parameter_declaration" not in _properties(tools["update_indicator"])
    declaration = {"lookback": {"type": "integer", "default": 20}}
    count = len(service.calls)
    assert (
        not tools["author_study"]
        .handler(
            {
                "program_id": "alpha",
                "source": "Rust source",
                "configuration": CONFIG,
                "parameter_declaration": declaration,
            }
        )
        .ok
    )
    assert len(service.calls) == count
    assert (
        tools["author_strategy"]
        .handler(
            {
                "program_id": "alpha",
                "source": "Rust source",
                "configuration": CONFIG,
                "parameter_declaration": declaration,
            }
        )
        .ok
    )
    assert service.calls[-1][1]["parameter_declaration"] == declaration
    assert (
        tools["update_strategy"]
        .handler(
            {
                "program_id": "alpha",
                "expected_version": "a" * 64,
                "configuration": CONFIG,
                "parameter_declaration": declaration,
            }
        )
        .ok
    )
    assert service.calls[-1][1]["parameter_declaration"] == declaration


def test_run_backtest_passes_parameters_and_run_study_stays_id_only() -> None:
    service = Service()
    tools = _tool_map(service)
    assert tools["run_study"].handler({"program_id": "alpha"}).ok
    assert service.calls[-1][1] == {"data_window": None, "retain": None}
    assert (
        tools["run_backtest"]
        .handler({"program_id": "alpha", "parameters": {"lookback": 20}})
        .ok
    )
    assert service.calls[-1][1]["parameters"] == {"lookback": 20}
    assert tools["run_backtest"].handler({"program_id": "alpha"}).ok
    assert service.calls[-1][1]["parameters"] is None
    count = len(service.calls)
    assert (
        not tools["run_backtest"]
        .handler({"program_id": "alpha", "parameters": ["not", "an", "object"]})
        .ok
    )
    assert len(service.calls) == count


def test_run_backtest_batch_schema_bounds_and_dispatch() -> None:
    service = Service()
    tools = _tool_map(service)
    batch = tools["run_backtest_batch"]
    function = cast(Mapping[str, object], batch.spec["function"])
    schema = cast(Mapping[str, object], function["parameters"])
    properties = cast(Mapping[str, object], schema["properties"])
    assignments = cast(Mapping[str, object], properties["assignments"])
    assert schema["required"] == ["program_id", "assignments"]
    assert assignments["minItems"] == 1 and "maxItems" not in assignments
    count = len(service.calls)
    for invalid in (
        {"program_id": "alpha"},
        {"program_id": "alpha", "assignments": []},
        {"program_id": "alpha", "assignments": "nope"},
        {"program_id": "alpha", "assignments": [{"lookback": 20}], "label": ""},
        {"program_id": "alpha", "assignments": [{"lookback": 20}], "extra": True},
    ):
        assert not batch.handler(invalid).ok
    assert len(service.calls) == count
    assert batch.handler(
        {
            "program_id": "alpha",
            "assignments": [{"lookback": 20}, {"lookback": 40}],
            "label": "grid",
        }
    ).ok
    positional, kwargs = service.calls[-1]
    assert positional[:4] == (
        "trusted-workspace",
        "strategy",
        "alpha",
        [{"lookback": 20}, {"lookback": 40}],
    )
    assert kwargs == {"label": "grid", "data_window": None, "retain": None}
    assert batch.handler({"program_id": "alpha", "assignments": [{"lookback": 20}]}).ok
    assert service.calls[-1][1]["label"] is None


def test_run_null_batch_schema_bounds_and_dispatch() -> None:
    service = Service()
    tools = _tool_map(service)
    null_batch = tools["run_null_batch"]
    function = cast(Mapping[str, object], null_batch.spec["function"])
    schema = cast(Mapping[str, object], function["parameters"])
    properties = cast(Mapping[str, object], schema["properties"])
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["program_id", "mode", "block_size", "seeds"]
    assert set(properties) == {
        "program_id",
        "parameters",
        "block_size",
        "mode",
        "offset",
        "chunk_size",
        "seeds",
        "label",
    }
    block_size = cast(Mapping[str, object], properties["block_size"])
    assert block_size == {"type": "integer", "minimum": 1, "maximum": 10000}
    seeds = cast(Mapping[str, object], properties["seeds"])
    assert seeds["minItems"] == 1 and "maxItems" not in seeds
    assert seeds["uniqueItems"] is True
    assert seeds["items"] == {"type": "integer", "minimum": 0}
    count = len(service.calls)
    for invalid in (
        {"program_id": "alpha"},
        {"program_id": "alpha", "block_size": 5},
        {"program_id": "alpha", "seeds": [1]},
        {
            "program_id": "alpha",
            "mode": "block_permutation",
            "block_size": 5,
            "seeds": [],
        },
        {
            "program_id": "alpha",
            "mode": "block_permutation",
            "block_size": 5,
            "seeds": [1] * 17,
        },
        {
            "program_id": "alpha",
            "mode": "block_permutation",
            "block_size": 5,
            "seeds": "nope",
        },
        {
            "program_id": "alpha",
            "mode": "block_permutation",
            "block_size": 5,
            "seeds": [1.5],
        },
        {"program_id": "alpha", "block_size": "5", "seeds": [1]},
        {
            "program_id": "alpha",
            "mode": "block_permutation",
            "block_size": 5,
            "seeds": [1],
            "extra": True,
        },
        {
            "program_id": "alpha",
            "mode": "block_permutation",
            "block_size": 5,
            "seeds": [1],
            "label": "",
        },
        {
            "program_id": "alpha",
            "mode": "block_permutation",
            "block_size": 5,
            "seeds": [1],
            "parameters": ["not", "an", "object"],
        },
    ):
        assert not null_batch.handler(invalid).ok
    assert len(service.calls) == count
    assert null_batch.handler(
        {
            "program_id": "alpha",
            "parameters": {"lookback": 20},
            "mode": "block_permutation",
            "block_size": 5,
            "seeds": [1, 2],
            "label": "seeds",
        }
    ).ok
    positional, kwargs = service.calls[-1]
    assert positional == ("trusted-workspace", "strategy", "alpha")
    assert kwargs == {
        "parameters": {"lookback": 20},
        "mode": "block_permutation",
        "block_size": 5,
        "seeds": [1, 2],
        "offset": 0,
        "chunk_size": 64,
        "label": "seeds",
        "data_window": None,
        "retain": None,
    }
    assert null_batch.handler(
        {
            "program_id": "alpha",
            "mode": "block_permutation",
            "block_size": 5,
            "seeds": [1],
        }
    ).ok
    assert service.calls[-1][1] == {
        "parameters": None,
        "mode": "block_permutation",
        "block_size": 5,
        "seeds": [1],
        "offset": 0,
        "chunk_size": 64,
        "label": None,
        "data_window": None,
        "retain": None,
    }


def _launch_request(cutoff: datetime) -> dict[str, object]:
    """The launch request persisted for a cutoff with the default partition."""
    return {
        "createdAt": cutoff.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "partition": LaunchPartition(1825, 90, 1, 0).as_dict(),
    }


def test_execution_handlers_forward_the_research_mandate_policy() -> None:
    mandate = derive_mandate(
        "trusted-workspace",
        "execution-1",
        _launch_request(datetime(2026, 9, 4, tzinfo=UTC)),
    )
    assert mandate is not None
    policy = mandate.default_window
    service = Service()
    tools = _tool_map(service, data_window=policy)
    assert tools["run_study"].handler({"program_id": "alpha"}).ok
    assert service.calls[-1][1] == {"data_window": policy, "retain": None}
    assert (
        tools["run_backtest"]
        .handler({"program_id": "alpha", "parameters": {"lookback": 20}})
        .ok
    )
    assert service.calls[-1][1] == {
        "parameters": {"lookback": 20},
        "data_window": policy,
        "retain": None,
    }
    assert (
        tools["run_backtest_batch"]
        .handler({"program_id": "alpha", "assignments": [{"lookback": 20}]})
        .ok
    )
    positional, kwargs = service.calls[-1]
    assert positional[:4] == (
        "trusted-workspace",
        "strategy",
        "alpha",
        [{"lookback": 20}],
    )
    assert kwargs == {"label": None, "data_window": policy, "retain": None}
    assert (
        tools["run_null_batch"]
        .handler(
            {
                "program_id": "alpha",
                "mode": "block_permutation",
                "block_size": 5,
                "seeds": [1, 2],
            }
        )
        .ok
    )
    positional, kwargs = service.calls[-1]
    assert positional == ("trusted-workspace", "strategy", "alpha")
    assert kwargs == {
        "parameters": None,
        "mode": "block_permutation",
        "block_size": 5,
        "seeds": [1, 2],
        "offset": 0,
        "chunk_size": 64,
        "label": None,
        "data_window": policy,
        "retain": None,
    }


def test_job_retention_binds_the_trusted_context_before_dispatch() -> None:
    service = Service()
    context = ToolContext("trusted-workspace", "chat", "conversation", "actor")
    retained: list[tuple[ToolContext, str, str, Mapping[str, object]]] = []
    settled: list[tuple[str, str | None]] = []

    def retention(
        bound_context: ToolContext,
        job_id: str,
        tool_name: str,
        attribution: Mapping[str, object],
    ) -> Callable[[str, str | None], None]:
        retained.append((bound_context, job_id, tool_name, attribution))
        return lambda certainty, message: settled.append((certainty, message))

    tools = _tool_map(service, job_retention=retention)
    assert tools["run_study"].handler({"program_id": "alpha"}).ok
    kwargs = service.calls[-1][1]
    retain = cast(Callable[..., Callable[[str, str | None], None]], kwargs["retain"])
    assert callable(retain)
    settle = retain("job-opaque", "run_study", {"programId": "alpha"})
    assert retained == [(context, "job-opaque", "run_study", {"programId": "alpha"})]
    settle("not_started", "refused")
    assert settled == [("not_started", "refused")]


def test_session_registry_forwards_the_policy_to_program_tools() -> None:
    from thesis.agent.runtime.registry import build_session_registry

    mandate = derive_mandate(
        "trusted-workspace",
        "execution-1",
        _launch_request(datetime(2026, 9, 4, tzinfo=UTC)),
    )
    assert mandate is not None
    policy = mandate.default_window
    service = Service()
    retained: list[str] = []

    def retention(
        context: ToolContext,
        job_id: str,
        tool_name: str,
        attribution: Mapping[str, object],
    ) -> Callable[[str, str | None], None]:
        retained.append(job_id)
        return lambda _certainty, _message: None

    registry = build_session_registry(
        context=ToolContext(
            "trusted-workspace",
            "chat",
            "conversation",
            "actor",
            allowed_tools=("run_study",),
        ),
        programs=cast(WorkspacePrograms, service),
        data_window=policy,
        job_retention=retention,
    )
    assert registry.dispatch("run_study", {"program_id": "alpha"}).ok
    kwargs = service.calls[-1][1]
    assert kwargs["data_window"] == policy
    retain = cast(Callable[..., None], kwargs["retain"])
    retain("job-opaque", "run_study", {"programId": "alpha"})
    assert retained == ["job-opaque"]
