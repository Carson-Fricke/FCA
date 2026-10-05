"""Canonical result projection preserves all datum cards and timing metadata."""

from datetime import UTC, datetime

import pytest

from thesis.agent.messages import MessageContent, SessionMessage, ToolCorrelation
from thesis.agent.runtime.datum import Datum
from thesis.agent.runtime.session import SessionTurn, tool_chunks


@pytest.mark.parametrize("typed", [False, True])
def test_tool_metadata_is_carried_by_every_result(typed: bool) -> None:
    started_at = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
    turn = SessionTurn(
        message=SessionMessage(
            "result",
            "incoming",
            MessageContent(
                text="Fallback",
                source="tool",
                correlation=ToolCorrelation("output", "call-1", "check"),
                result={"ok": True, "text": "Fallback", "structured": {"valid": True}},
            ),
        ),
        invocation_id="invocation-1",
        started_at=started_at,
        duration_ms=0,
        arguments={"symbols": ["BTC"]},
        datums=(Datum("first", text="First"), Datum("second", text="Second"))
        if typed
        else (),
    )
    chunks = list(tool_chunks(turn))
    assert len(chunks) == (2 if typed else 1)
    for chunk in chunks:
        assert chunk.tool == "check"
        assert chunk.call_id == "call-1"
        assert chunk.data == {
            "startedAt": started_at.isoformat(),
            "durationMs": 0,
            "arguments": {"symbols": ["BTC"]},
            "invocationId": "invocation-1",
        }
        assert "durationMs" not in chunk.structured
    if typed:
        assert [chunk.text for chunk in chunks] == ["First", "Second"]
    else:
        assert chunks[0].structured == {"valid": True}


def test_structured_only_datums_project_every_card() -> None:
    turn = SessionTurn(
        message=SessionMessage(
            "result",
            "incoming",
            MessageContent(
                text="",
                source="tool",
                correlation=ToolCorrelation("output", "call-2", "read_sdk_reference"),
                result={"ok": True, "datums": []},
            ),
        ),
        datums=(
            Datum("read_sdk_reference", structured={"source": "export_study_program!"}),
            Datum("read_artifact", structured={"name": "metrics.json"}),
        ),
    )
    chunks = list(tool_chunks(turn))
    assert [chunk.structured for chunk in chunks] == [
        {"source": "export_study_program!"},
        {"name": "metrics.json"},
    ]
    assert [chunk.text for chunk in chunks] == ["", ""]
    assert [chunk.datum_type for chunk in chunks] == [
        "read_sdk_reference",
        "read_artifact",
    ]
