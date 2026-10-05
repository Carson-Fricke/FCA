"""Public schemas are generated from the same types as live wire serializers."""

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pydantic import ValidationError

from thesis.agent.runtime.activity import (
    RUNTIME_ACTIVITY_ADAPTER,
    Activity,
    Delivery,
    ExternalInput,
    ModelOutput,
    RuntimeTool,
    activity_data,
    chat_message_schema,
    runtime_activity_schema,
)
from thesis.agent.runtime.datum import (
    Datum,
    DatumWire,
    ToolResultWire,
    datum_schema,
    tool_result_payload,
    tool_result_schema,
)
from thesis.agent.runtime.tree import (
    ChatSubAgentTreeWire,
    chat_sub_agent_tree_schema,
    project_chat_sub_agents,
)
from thesis.chat.contracts import (
    CompletionMode,
    SubAgentKind,
    SubAgentRecord,
    SubAgentStatus,
)


def test_tree_serialization_preserves_durable_authority_and_generated_schema() -> None:
    record = SubAgentRecord(
        workspace_id="workspace",
        created_at=datetime.now(UTC),
        created_by="operator",
        sub_agent_id="phase",
        chat_id="chat",
        role="analysis",
        parent_sub_agent_id="workflow",
        workflow_execution_id="execution",
        phase_attempt_id="attempt",
        phase="analysis",
        spec_version="2.3.0",
        completion_mode=CompletionMode.EXPLICIT_COMPLETION,
        phase_executor=True,
        returns_to_sub_agent_id="workflow",
        kind=SubAgentKind.ONE_SHOT,
        allowed_tools=("validate",),
        delegated_budget=7,
        result={"summary": "evidence"},
        result_ref="result",
        status=SubAgentStatus.COMPLETED,
    )
    wire = project_chat_sub_agents(
        [record], workspace_id="workspace", chat_id="chat"
    ).as_dict()
    validated = ChatSubAgentTreeWire.model_validate(wire)
    assert validated.model_dump(mode="json", by_alias=True) == wire
    node = cast(list[dict[str, Any]], wire["items"])[0]
    assert node["phaseAttemptId"] == "attempt"
    assert node["specVersion"] == "2.3.0"
    assert node["completionMode"] == "explicit_completion"
    assert node["phaseExecutor"] is True
    assert node["returnsToSubAgentId"] == "workflow"
    assert node["allowedTools"] == ["validate"] and node["delegatedBudget"] == 7
    assert node["result"] == {"summary": "evidence"} and node["resultRef"] == "result"
    assert "context" not in node
    schema = chat_sub_agent_tree_schema()
    fields = schema["$defs"]["SubAgentNodeWire"]["properties"]
    assert set(node) == set(fields)
    assert "spec_version" not in fields


@pytest.mark.parametrize(
    "activity",
    [
        ExternalInput(),
        Delivery("child", "delivery"),
        RuntimeTool("child", "finished", "call"),
        ModelOutput(
            "child",
            "invocation",
            "call",
            ({"id": "call", "function": {"name": "check", "arguments": "{}"}},),
        ),
    ],
)
def test_activity_wire_and_discriminator_share_runtime_types(
    activity: Activity,
) -> None:
    wire = activity_data(activity)
    decoded = RUNTIME_ACTIVITY_ADAPTER.validate_python(
        {**wire, "ownerActorId": "runtime"}
    )
    assert activity_data(decoded) == wire
    schema = runtime_activity_schema()
    assert schema["discriminator"]["propertyName"] == "activityKind"
    definition = schema["$defs"][type(activity).__name__]
    assert definition["additionalProperties"] is True
    assert set(wire) <= set(definition["properties"])
    message = chat_message_schema()
    assert (
        message["properties"]["data"]["anyOf"][0]["discriminator"]
        == schema["discriminator"]
    )
    assert type(activity).__name__ in message["$defs"]


def test_tool_and_datum_serializers_use_exported_wire_models() -> None:
    datum = Datum("evidence", structured={"value": 42}, text="Checked")
    wire = datum.as_dict()
    assert DatumWire.model_validate(wire).model_dump(mode="json", by_alias=True) == wire
    assert set(wire) == set(datum_schema()["properties"])
    result = tool_result_payload(
        call_id="call",
        ok=True,
        failure=None,
        structured={"value": 42},
        summary="Checked",
        text="Checked",
        tool="check",
        datum_type="evidence",
    )
    assert (
        ToolResultWire.model_validate(result).model_dump(mode="json", by_alias=True)
        == result
    )
    assert set(result) == set(tool_result_schema()["properties"])
    with pytest.raises(ValidationError):
        ToolResultWire.model_validate({**result, "ok": "not a boolean"})
