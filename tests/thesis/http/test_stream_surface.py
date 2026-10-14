"""The current HTTP contract exposes one unified timeline stream per prefix."""

from typing import Any


def test_unified_timeline_is_the_only_registered_stream(
    wire_schema: dict[str, Any],
) -> None:
    paths = wire_schema["paths"]
    for prefix in ("", "/api"):
        base = prefix + "/v1/workspaces/{workspace_id}"
        assert {
            path for path in paths if path.startswith(base) and path.endswith("/stream")
        } == {base + "/chats/{chat_id}/timeline/stream"}
        for suffix in (
            "/chats/{chat_id}/messages",
            "/chats/{chat_id}/timeline",
            "/executions/{execution_id}/events",
            "/chats/{chat_id}/sub_agents/{sub_agent_id}/events",
        ):
            assert "get" in paths[base + suffix]
