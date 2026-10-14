"""Job metadata stays compact while the authoritative harness input remains intact."""

import json

import pytest
from tests.thesis.agent.test_harness_tools import Transport

from thesis.agent.harness_tools import HarnessToolProvider
from thesis.extensions.tools.registry import build_session_registry
from thesis.integrations.mcp import HarnessMcpClient
from thesis_runtime.contracts.context import ToolContext


@pytest.mark.parametrize("name", ["get_job_status", "get_job_result"])
def test_session_job_read_returns_execution_reference_instead_of_source(name):
    transport = Transport()
    transport.catalog = [{"name": name, "inputSchema": {"type": "object"}}]
    accepted = {
        "job_id": "job-one",
        "strategy_instances": [
            {
                "instance_id": "study-one",
                "bundle_hash": "bundle",
                "parameters": {"source": "immutable study source\n" * 4000},
            }
        ],
    }
    transport.result = {
        "isError": False,
        "structuredContent": {
            "job_id": "job-one",
            "state": "succeeded",
            "metrics": {"sessions": 145},
            "input_digest": "digest",
            "accepted_input": accepted,
        },
        "content": [{"type": "text", "text": json.dumps(accepted)}],
    }
    provider = HarnessToolProvider(HarnessMcpClient(transport))
    context = ToolContext("workspace-one", "chat-one", "chat-one", "operator")
    registry = build_session_registry(context=context, harness_tools=provider)
    response = registry.dispatch(name, {"job_id": "job-one"}, context=context)
    assert response.ok
    assert response.structured == {
        "job_id": "job-one",
        "state": "succeeded",
        "metrics": {"sessions": 145},
        "input_digest": "digest",
        "accepted_input_reference": {
            "job_id": "job-one",
            "input_digest": "digest",
            "programs": [{"program_id": "study-one", "bundle_hash": "bundle"}],
        },
    }
    assert response.text == ""
    assert (
        provider.call_tool(name, {"job_id": "job-one"}).structured["accepted_input"]
        == accepted
    )
    provider.close()
