"""Pure ordering rules protecting pending input and overlapping tool groups."""

from thesis_runtime.contracts.messages import (
    MessageContent,
    SessionMessage,
    ToolCorrelation,
    ToolRequest,
)
from thesis_runtime.sessions.compaction import retiring_prefix, select_evidence


def test_retiring_prefix_can_cross_calls_but_never_retires_pending_inputs():
    initial = SessionMessage(
        "initial", "incoming", MessageContent("assignment", "user")
    )
    first = SessionMessage(
        "first",
        "outgoing",
        MessageContent("", "agent", requests=(ToolRequest("one", "work", "{}"),)),
    )
    second = SessionMessage(
        "second",
        "outgoing",
        MessageContent("", "agent", requests=(ToolRequest("two", "work", "{}"),)),
    )
    first_result = SessionMessage(
        "first-result",
        "incoming",
        MessageContent(
            "done", "tool", correlation=ToolCorrelation("first", "one", "work")
        ),
    )
    pending = SessionMessage("pending", "incoming", MessageContent("new input", "user"))
    second_result = SessionMessage(
        "second-result",
        "incoming",
        MessageContent(
            "done", "tool", correlation=ToolCorrelation("second", "two", "work")
        ),
    )
    assert retiring_prefix(
        (initial, first, second, first_result, pending, second_result), (pending,), 0
    ) == (initial, first, second, first_result)
    assert retiring_prefix((initial, first), (), 0) == (initial, first)


def test_verbatim_evidence_budget_retains_whole_groups_in_arrival_order():
    call = SessionMessage(
        "call",
        "outgoing",
        MessageContent("", "agent", requests=(ToolRequest("one", "work", "{}"),)),
    )
    interleaved = SessionMessage(
        "user", "incoming", MessageContent("correction", "user")
    )
    result = SessionMessage(
        "result",
        "incoming",
        MessageContent(
            "evidence", "tool", correlation=ToolCorrelation("call", "one", "work")
        ),
    )
    independent = SessionMessage(
        "other", "outgoing", MessageContent("progress", "agent")
    )
    assert select_evidence(
        (call, interleaved, result, independent), (1, 0, 0), 1_000
    ) == (call, interleaved, result, independent)
    assert select_evidence((call, interleaved, result), (0,), 1) == ()
