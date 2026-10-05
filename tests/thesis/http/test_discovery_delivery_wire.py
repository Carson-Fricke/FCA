"""Discovery startup delivery must match the schema consumed by the live UI."""

from datetime import UTC, datetime

from thesis.agent.messages import SESSION_MESSAGE_ADAPTER, from_record
from thesis.agent.runtime.conversation import Conversations
from thesis.agent.runtime.datum import Datum, ToolResultWire
from thesis.agent.runtime.delivery import Anchor, DeliveryService
from thesis.chat import ChatService, MessageRole
from thesis.http.schema import export_schema


def test_discovery_seed_separates_datum_result_from_delivery_metadata() -> None:
    chat = ChatService(clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    thread = chat.create_chat("workspace", title="Discovery", owner_actor_id="operator")
    receipt = DeliveryService(chat).deliver(
        Anchor("workspace", thread.chat_id, "discovery-agent"),
        Datum("agent_prompt", structured={"phase": "discovery"}, text="Explore MSFT"),
        role=MessageRole.USER,
        data={"kind": "agent_prompt", "phase": "discovery", "attempt": 0},
        idempotency_key="discovery-seed",
        schedule=False,
    )
    message = receipt.message
    assert message.tool_result is not None
    result = ToolResultWire.model_validate(message.tool_result)
    assert result.call_id is None and result.tool is None
    assert result.datum_type == "agent_prompt"
    assert message.data is not None
    assert message.data["deliveryKey"] == "discovery-seed"
    assert "deliveryKey" not in message.tool_result
    assert "phase" not in message.tool_result
    assert from_record(message).direction == "incoming"
    assert from_record(message).content.source == "agent_prompt"
    SESSION_MESSAGE_ADAPTER.validate_python(message.data["sessionMessage"])


def test_schema_preserves_persisted_delivery_shape_without_opening_tool_results() -> (
    None
):
    components = export_schema()["components"]["schemas"]
    variants = components["ChatMessageWire"]["properties"]["tool_result"]["anyOf"]
    assert {"$ref": "#/components/schemas/DeliveredDatumWire"} in variants
    assert components["DeliveredDatumWire"]["additionalProperties"] is True
    assert "datumType" in components["DeliveredDatumWire"]["required"]
    assert components["ToolResultWire"]["additionalProperties"] is False
    assert "callId" in components["ToolResultWire"]["required"]


def test_canonical_metadata_schema_types_session_and_datums_without_requiring_them() -> (
    None
):
    components = export_schema()["components"]["schemas"]
    assert components["SessionMessageWire"]["properties"]["direction"]["enum"] == [
        "incoming",
        "outgoing",
        "instruction",
    ]
    for name in ("ExternalInput", "ModelOutput", "RuntimeTool", "Delivery"):
        assert components[name]["additionalProperties"] is True
    for name in (
        "ChatMessageWire",
        "CreateMessageResponseWire",
        "NodeInputReceiptWire",
    ):
        metadata = components[name]["properties"]["data"]["allOf"][1]
        assert "required" not in metadata
        assert metadata["properties"] == {
            "sessionMessage": {"$ref": "#/components/schemas/SessionMessageWire"},
            "datums": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/DatumWire"},
            },
        }


def test_multiple_datums_preserve_one_canonical_message_and_strict_main_result() -> (
    None
):
    from thesis.agent.messages import MessageContent, SessionMessage, ToolCorrelation
    from thesis.agent.runtime.datum import DatumWire

    chat = ChatService()
    thread = chat.create_chat("workspace", title="Datums", owner_actor_id="operator")
    conversations = Conversations(chat=chat)
    datums = (
        Datum("diagnostic", text="Repair"),
        Datum("artifact", structured={"id": "model"}),
    )
    correlation = ToolCorrelation("request-message", "call", "validate_study")
    canonical = conversations.deliver_session_message(
        Anchor("workspace", thread.chat_id),
        SessionMessage(
            "result-message",
            "incoming",
            MessageContent(
                "Checked",
                "tool_result",
                correlation,
                result={"ok": False, "structured": {"diagnostic": "Repair"}},
            ),
        ),
        datums=datums,
        schedule=False,
    )
    messages = chat.list_messages("workspace", thread.chat_id)
    assert len(messages) == 1
    message = messages[0]
    assert message.data is not None and message.tool_result is not None
    presented = message.data["datums"]
    assert isinstance(presented, list)
    assert presented == [datum.as_dict() for datum in datums]
    for datum in presented:
        DatumWire.model_validate(datum)
    main = ToolResultWire.model_validate(message.tool_result)
    assert main.call_id == "call" and main.tool == "validate_study"
    assert from_record(message) == canonical
    assert canonical.content.correlation == correlation
