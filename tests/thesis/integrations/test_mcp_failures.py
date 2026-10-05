"""Named MCP request logging and safe, non-replaying transport failures."""

from __future__ import annotations

import logging
import ssl

import httpx
import pytest

from thesis.agent.harness_tools import HarnessToolProvider
from thesis.integrations.mcp import HarnessMcpClient, RemoteHttpMcpTransport


@pytest.mark.parametrize(
    "error, certainty",
    [
        (
            httpx.ConnectError(
                "[SSL: CERTIFICATE_VERIFY_FAILED] certificate failed secret"
            ),
            "not_started",
        ),
        (httpx.ReadTimeout("timed out"), "unknown_outcome"),
        (ssl.SSLError("CERTIFICATE_VERIFY_FAILED secret"), "unknown_outcome"),
        (RuntimeError("failed secret"), "unknown_outcome"),
    ],
)
def test_transport_error_preserves_details_redacts_token_and_never_replays(
    error: Exception, certainty: str, caplog: pytest.LogCaptureFixture
) -> None:
    calls = 0

    def fail(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise error

    transport = RemoteHttpMcpTransport(
        "https://harness.test",
        bearer_token="secret",
        transport=httpx.MockTransport(fail),
    )
    provider = HarnessToolProvider(HarnessMcpClient(transport))
    with caplog.at_level(logging.INFO, logger="thesis.integrations.mcp"):
        failure = provider.call_tool(
            "run_study", {"model": {"source": "private-source"}}
        ).failure
    assert failure is not None
    assert failure.execution_certainty == certainty
    assert type(error).__name__ in failure.message
    assert "secret" not in failure.message
    if isinstance(error, (ssl.SSLError, httpx.ConnectError)):
        assert "CERTIFICATE_VERIFY_FAILED" in failure.message
    assert calls == 1
    assert "method=tools/call tool=run_study" in caplog.text
    assert "private-source" not in caplog.text and "secret" not in caplog.text
    provider.close()


@pytest.mark.parametrize(
    "status, certainty",
    [(401, "not_started"), (403, "not_started"), (500, "unknown_outcome")],
)
def test_http_failure_certainty(status: int, certainty: str) -> None:
    transport = RemoteHttpMcpTransport(
        "https://harness.test",
        bearer_token="secret",
        transport=httpx.MockTransport(lambda _: httpx.Response(status)),
    )
    provider = HarnessToolProvider(HarnessMcpClient(transport))
    failure = provider.call_tool("run_study").failure
    assert failure is not None
    assert failure.execution_certainty == certainty
    assert failure.status_code == status
    provider.close()


def test_missing_auth_is_not_started_and_discovery_logging_names_list(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HARNESS_API_BEARER_TOKEN", raising=False)
    transport = RemoteHttpMcpTransport("https://harness.test")
    provider = HarnessToolProvider(HarnessMcpClient(transport))
    with caplog.at_level(logging.INFO, logger="thesis.integrations.mcp"):
        failure = provider.discover().failure
    assert failure is not None and failure.execution_certainty == "not_started"
    assert "method=tools/list tool=None" in caplog.text
    provider.close()
