from collections.abc import Mapping

import pytest

from thesis.agent.harness_tools import HarnessToolProvider
from thesis.integrations.mcp import HarnessMcpClient


class Transport:
    def __init__(self, code: int) -> None:
        self.code = code

    def exchange(self, request: Mapping[str, object]) -> Mapping[str, object]:
        return {
            "jsonrpc": "2.0",
            "id": request["id"],
            "error": {"code": self.code, "message": "actual server rejection"},
        }

    def notify(self, request: Mapping[str, object]) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.mark.parametrize(
    "code, certainty", [(-32602, "not_started"), (-32603, "unknown_outcome")]
)
def test_remote_protocol_error_retains_server_details_and_outcome_certainty(
    code: int, certainty: str
) -> None:
    failure = (
        HarnessToolProvider(HarnessMcpClient(Transport(code)))
        .call_tool("run_backtest", {})
        .failure
    )
    assert failure is not None
    assert failure.execution_certainty == certainty
    assert "actual server rejection" in failure.message
    assert failure.payload["serverError"] == {
        "code": code,
        "message": "actual server rejection",
    }
