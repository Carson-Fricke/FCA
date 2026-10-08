"""Catalog-only caching and server-owned validation results without replay."""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import cast

import pytest

from thesis.agent.harness_tools import (
    GetJobResultArgs,
    HarnessToolError,
    HarnessToolProvider,
    ListArtifactsArgs,
    ReadArtifactArgs,
)
from thesis.integrations.mcp import (
    ExecutionCertainty,
    HarnessMcpAuthError,
    HarnessMcpClient,
    HarnessMcpError,
    HarnessMcpProtocolError,
    HarnessMcpTransportError,
    McpToolResult,
)


class Transport:
    def __init__(self) -> None:
        self.calls: list[Mapping[str, object]] = []
        self.error: Exception | None = None
        self.result: dict[str, object] = {
            "isError": False,
            "structuredContent": {"valid": True},
            "content": [],
        }
        self.catalog: object = [{"name": "validate_study", "inputSchema": {}}]

    def exchange(self, request: Mapping[str, object]) -> Mapping[str, object]:
        self.calls.append(request)
        if self.error:
            raise self.error
        result = (
            {"serverInfo": {"name": "harness", "version": "1"}}
            if request["method"] == "initialize"
            else {"tools": self.catalog}
            if request["method"] == "tools/list"
            else self.result
        )
        return {"jsonrpc": "2.0", "id": request["id"], "result": result}

    def notify(self, request: Mapping[str, object]) -> None:
        pass

    def close(self) -> None:
        pass

    def count(self, method: str) -> int:
        return sum(call["method"] == method for call in self.calls)


def test_catalog_success_cached_and_failures_visible_and_retried() -> None:
    transport = Transport()
    provider = HarnessToolProvider(HarnessMcpClient(transport))
    transport.error = HarnessMcpTransportError("offline")
    assert not provider.discover().ok
    with pytest.raises(HarnessToolError):
        provider.list_tools()
    transport.error = None
    assert provider.list_tools()[0].name == "validate_study"
    provider.list_tools()
    assert transport.count("tools/list") == 3
    provider.invalidate()
    provider.list_tools()
    assert transport.count("tools/list") == 4
    provider.close()


@pytest.mark.parametrize("catalog", [None, {}, [42], [{"name": ""}]])
def test_malformed_catalog_is_not_silent_empty_success(catalog: object) -> None:
    transport = Transport()
    transport.catalog = catalog
    provider = HarnessToolProvider(HarnessMcpClient(transport))
    failure = provider.discover().failure
    assert failure is not None and failure.kind == "protocol"


@pytest.mark.parametrize("is_error", [False, True])
def test_identical_validation_always_calls_server_and_preserves_result(
    is_error: bool,
) -> None:
    transport = Transport()
    provider = HarnessToolProvider(HarnessMcpClient(transport))
    model: dict[str, object] = {"source": "study", "params": {"a": 1, "b": 2}}
    for reused in (False, True, False):
        structured = {
            "valid": not is_error,
            "reused": reused,
            "cacheable": True,
            "deterministicCompilerDiagnostic": is_error,
            "diagnostics": [{"message": "server diagnostic", "line": 3}],
            "validationIdentity": "compiler-owned-identity",
        }
        transport.result = {
            "isError": is_error,
            "structuredContent": structured,
            "content": [{"type": "text", "text": "server validation result"}],
        }
        for result in (
            provider.validate_study(model=model),
            provider.call_tool("validate_study", {"model": model}),
        ):
            assert result.ok is not is_error
            assert result.structured == structured
            assert result.text == "server validation result"
            if is_error:
                assert result.failure is not None
                assert result.failure.payload == structured
                assert result.failure.execution_certainty == "observed_failure"
                assert "Fix the authored source" in result.failure.retry_guidance
    assert transport.count("tools/call") == 6
    assert transport.count("initialize") == 0
    assert all(
        call["params"] == {"name": "validate_study", "arguments": {"model": model}}
        for call in transport.calls
    )


def test_invalidation_and_refresh_only_affect_catalog() -> None:
    transport = Transport()
    client = HarnessMcpClient(transport)
    initialized = client.initialize()
    provider = HarnessToolProvider(client)
    provider.list_tools()
    transport.result["structuredContent"] = {
        "reused": True,
        "validationIdentity": "compiler-owned-identity",
    }
    for _ in range(2):
        provider.invalidate()
        assert provider.refresh().ok
        assert provider.validate_study(model={}).structured == {
            "reused": True,
            "validationIdentity": "compiler-owned-identity",
        }
    assert client.server_info == initialized
    assert transport.count("initialize") == 1
    assert transport.count("tools/list") == 3
    assert transport.count("tools/call") == 2


@pytest.mark.parametrize("flag", [None, False, True, "true", 1, {}])
def test_compiler_classification_uses_only_structured_boolean(flag: object) -> None:
    transport = Transport()
    structured = {} if flag is None else {"deterministicCompilerDiagnostic": flag}
    transport.result = {
        "isError": True,
        "structuredContent": structured,
        "content": [
            {
                "type": "text",
                "text": "authored program "
                + "a" * 64
                + " failed to compile: error[E0432]: unresolved import\n --> src/lib.rs:1:5",
            }
        ],
    }
    provider = HarnessToolProvider(HarnessMcpClient(transport))
    for _ in range(2):
        result = provider.validate_study(model={})
        assert result.structured == structured
        assert result.failure is not None
        assert result.failure.deterministic_compiler_diagnostic is (flag is True)
        assert result.failure.execution_certainty == "observed_failure"
        if flag is not True:
            assert "never automatically replay" in result.failure.retry_guidance
    assert transport.count("tools/call") == 2


@pytest.mark.parametrize(
    "error",
    [
        HarnessMcpAuthError("unauthorized", execution_certainty="not_started"),
        HarnessMcpTransportError("TLS failed"),
        HarnessMcpProtocolError("invalid response"),
        RuntimeError("unexpected"),
    ],
)
@pytest.mark.parametrize("tool", ["validate_study", "run_study"])
def test_exchange_errors_preserve_certainty_without_replay(
    error: Exception, tool: str
) -> None:
    transport = Transport()
    transport.error = error
    provider = HarnessToolProvider(HarnessMcpClient(transport))
    for _ in range(2):
        result = provider.call_tool(tool, {"model": {}})
        assert result.failure is not None
        certainty: ExecutionCertainty = (
            error.execution_certainty
            if isinstance(error, HarnessMcpError)
            else "unknown_outcome"
        )
        assert result.failure.execution_certainty == certainty
        if certainty == "not_started":
            assert "execution did not start" in result.failure.retry_guidance
        else:
            assert "never automatically replay" in result.failure.retry_guidance
    assert transport.count("initialize") == 0
    assert transport.count("tools/call") == 2


def test_protocol_failure_is_not_cached() -> None:
    transport = Transport()
    transport.result = {}
    provider = HarnessToolProvider(HarnessMcpClient(transport))
    for _ in range(2):
        failure = provider.validate_study(model={}).failure
        assert failure is not None and failure.kind == "protocol"
    assert transport.count("tools/call") == 2


def test_concurrent_discovery_caches_one_catalog() -> None:
    transport = Transport()
    provider = HarnessToolProvider(HarnessMcpClient(transport))
    with ThreadPoolExecutor(max_workers=8) as executor:
        catalogs = list(executor.map(lambda _: provider.discover(), range(32)))
    assert all(catalog is catalogs[0] for catalog in catalogs)
    assert transport.count("tools/list") == 1


def test_unrelated_call_is_not_blocked_by_provider_validation_lock() -> None:
    started = Event()
    release = Event()

    class ConcurrentClient(HarnessMcpClient):
        def call_tool(
            self, name: str, arguments: Mapping[str, object] | None = None
        ) -> McpToolResult:
            if name == "validate_study":
                started.set()
                assert release.wait(timeout=5)
            submitted = None if arguments is None else arguments.get("job_id")
            return McpToolResult(
                tool=name,
                is_error=False,
                structured={} if submitted is None else {"job_id": submitted},
            )

    provider = HarnessToolProvider(ConcurrentClient(Transport()))
    with ThreadPoolExecutor(max_workers=2) as executor:
        validation = executor.submit(provider.validate_study, model={})
        try:
            assert started.wait(timeout=5)
            unrelated = executor.submit(
                provider.call_tool, "get_job_result", {"job_id": "1"}
            )
            assert unrelated.result(timeout=2).ok
        finally:
            release.set()
        assert validation.result(timeout=5).ok


def test_typed_job_arguments_use_one_job_id() -> None:
    transport = Transport()
    transport.result = {
        "isError": False,
        "structuredContent": {"job_id": "job-1", "state": "succeeded"},
        "content": [],
    }
    provider = HarnessToolProvider(HarnessMcpClient(transport))
    assert provider.get_job_result(GetJobResultArgs("job-1")).ok
    assert provider.list_artifacts(ListArtifactsArgs("job-1")).ok
    assert provider.read_artifact(ReadArtifactArgs("job-1", "metrics.json")).ok
    calls = [
        cast(Mapping[str, object], call["params"])["arguments"]
        for call in transport.calls
        if call["method"] == "tools/call"
    ]
    assert calls == [
        {"job_id": "job-1"},
        {"job_id": "job-1"},
        {"job_id": "job-1", "name": "metrics.json"},
    ]


def test_mismatched_returned_job_id_fails_closed() -> None:
    transport = Transport()
    transport.result = {
        "isError": False,
        "structuredContent": {"job_id": "job-other", "state": "succeeded"},
        "content": [],
    }
    provider = HarnessToolProvider(HarnessMcpClient(transport))
    result = provider.get_job_result(GetJobResultArgs("job-1"))
    assert not result.ok
    assert result.failure is not None
    assert result.failure.kind == "protocol"
    assert result.failure.execution_certainty == "unknown_outcome"
    assert result.structured["job_id"] == "job-other"


def test_missing_returned_job_id_fails_closed() -> None:
    transport = Transport()
    transport.result = {
        "isError": False,
        "structuredContent": {"state": "succeeded"},
        "content": [],
    }
    provider = HarnessToolProvider(HarnessMcpClient(transport))
    result = provider.get_job_result(GetJobResultArgs("job-1"))
    assert not result.ok
    assert result.failure is not None
    assert result.failure.kind == "protocol"
    assert result.failure.execution_certainty == "unknown_outcome"
    assert "did not acknowledge" in result.failure.message
