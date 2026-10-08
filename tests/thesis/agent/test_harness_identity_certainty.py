from collections.abc import Mapping

import pytest

from thesis.agent.harness_tools import HarnessToolProvider
from thesis.integrations.mcp import HarnessMcpClient


class Transport:
    def __init__(self, certainty: object = "unknown_outcome") -> None:
        self.certainty = certainty
        self.generation = 0
        self.requests: list[Mapping[str, object]] = []

    def exchange(self, request: Mapping[str, object]) -> Mapping[str, object]:
        self.requests.append(request)
        if request["method"] == "initialize":
            self.generation += 1
            result = {
                "serverInfo": {
                    "validationIdentity": f"compiler-{self.generation}",
                    "sdkIdentity": "sdk-hash",
                }
            }
        else:
            result = {
                "isError": True,
                "content": [{"type": "text", "text": "actual queue failure"}],
                "structuredContent": {
                    "executionCertainty": self.certainty,
                    "serverError": {"message": "actual queue failure"},
                },
            }
        return {"jsonrpc": "2.0", "id": request["id"], "result": result}

    def notify(self, request: Mapping[str, object]) -> None:
        pass

    def close(self) -> None:
        pass


def test_validation_identity_refreshes_serving_metadata_not_cached_handshake() -> None:
    transport = Transport()
    client = HarnessMcpClient(transport)
    provider = HarnessToolProvider(client)
    client.initialize()
    assert client.server_info["serverInfo"] == {
        "validationIdentity": "compiler-1",
        "sdkIdentity": "sdk-hash",
    }
    assert provider.validation_identity() == "compiler-2"
    assert provider.validation_identity() == "compiler-3"
    assert provider.current_server_info()["serverInfo"] == {
        "validationIdentity": "compiler-4",
        "sdkIdentity": "sdk-hash",
    }
    provider.read_sdk_reference(topic="consumer", start_line=1, end_line=5)
    assert transport.requests[-1]["params"] == {
        "name": "read_sdk_reference",
        "arguments": {"topic": "consumer", "start_line": 1, "end_line": 5},
    }


@pytest.mark.parametrize(
    "certainty", ["unknown_outcome", "not_started", "observed_failure", "invalid"]
)
def test_structured_certainty_survives_provider_and_tool_exception(
    certainty: str,
) -> None:
    expected = "unknown_outcome" if certainty == "invalid" else certainty
    provider = HarnessToolProvider(HarnessMcpClient(Transport(certainty)))
    failure = provider.call_tool("run_backtest", {}).failure
    assert failure is not None
    assert failure.execution_certainty == expected
    assert failure.payload["serverError"] == {"message": "actual queue failure"}
