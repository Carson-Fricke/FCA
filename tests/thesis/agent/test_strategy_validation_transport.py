from collections.abc import Mapping

from thesis.agent.harness_tools import HARNESS_TOOL_NAMES, HarnessToolProvider
from thesis.integrations.mcp import TOOL_VALIDATE_STRATEGY, HarnessMcpClient


class Transport:
    def __init__(self, *, failure: bool) -> None:
        self.failure = failure
        self.requests: list[Mapping[str, object]] = []

    def exchange(self, request: Mapping[str, object]) -> Mapping[str, object]:
        self.requests.append(request)
        return {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "isError": self.failure,
                "content": [{"type": "text", "text": "compiler result"}],
                "structuredContent": {
                    "valid": not self.failure,
                    "kind": "strategy",
                    "shell": "backtest",
                    "deterministicCompilerDiagnostic": self.failure,
                    "diagnostics": [{"message": "mismatched types"}],
                },
            },
        }

    def notify(self, request: Mapping[str, object]) -> None:
        pass

    def close(self) -> None:
        pass


def test_strategy_provider_preserves_typed_result_and_compiler_failure() -> None:
    assert TOOL_VALIDATE_STRATEGY in HARNESS_TOOL_NAMES
    model: dict[str, object] = {
        "source": "authored source",
        "harnessConfiguration": {"interval": "1d"},
    }
    for failure in (False, True):
        transport = Transport(failure=failure)
        provider = HarnessToolProvider(HarnessMcpClient(transport))
        result = provider.validate_strategy(model=model)
        assert result.ok is not failure
        assert result.structured["kind"] == "strategy"
        assert result.structured["shell"] == "backtest"
        assert transport.requests[0]["params"] == {
            "name": "validate_strategy",
            "arguments": {"model": model},
        }
        if failure:
            assert result.failure is not None
            assert result.failure.deterministic_compiler_diagnostic
            assert result.failure.payload["diagnostics"] == [
                {"message": "mismatched types"}
            ]
