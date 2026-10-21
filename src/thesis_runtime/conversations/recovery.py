"""Continuation timing, messages and supervisor selection; no writes or scheduling."""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping, Sequence

from thesis_runtime.contracts.messages import (
    MessageContent,
    SessionMessage,
    from_record,
)
from thesis_runtime.contracts.records import (
    RECORD_ANCHOR_ROLES,
    ChatMessage,
    MessageRole,
)
from thesis_runtime.conversations.delivery import Anchor
from thesis_runtime.subagents.store import SubAgentStore


def _continuation(row: ChatMessage) -> Mapping[str, object] | None:
    if row.role is not MessageRole.SYSTEM or row.created_by != "thesis-runtime":
        return None
    value = (from_record(row).content.result or {}).get("continuation")
    return value if isinstance(value, Mapping) else None


def continuation_delay(history: Sequence[ChatMessage], now: float) -> float | None:
    """Honor the latest trusted continuation unless operator input superseded it."""
    for row in reversed(history):
        previous = _continuation(row)
        if previous is not None:
            resume_at = previous.get("resume_at", 0)
            if isinstance(resume_at, int | float) and math.isfinite(resume_at):
                return max(0.0, resume_at - now)
            return None
        if row.role is MessageRole.USER:
            break
    return None


def continuation_message(
    history: Sequence[ChatMessage], failure: Mapping[str, object] | None, *, now: float
) -> tuple[SessionMessage, int]:
    """Build backed-off runnable input from retained outcomes, without a lifetime cap."""
    attempt = 1
    for row in reversed(history):
        message = from_record(row)
        result = message.content.result or {}
        previous = _continuation(row)
        if previous is not None:
            value = previous.get("attempt")
            attempt = value + 1 if isinstance(value, int) and value > 0 else 1
            break
        datums = result.get("datums", ())
        if not isinstance(datums, list | tuple):
            datums = ()
        if message.content.source == "user" or any(
            isinstance(datum, Mapping) and datum.get("ok") is True for datum in datums
        ):
            break
        if (
            message.direction == "outgoing"
            and not message.content.requests
            and not result.get("interrupted")
        ):
            break
    delay = 0.0 if failure is None else min(300.0, 30.0 * 2 ** min(attempt - 1, 4))
    requested = (failure or {}).get("retry_after")
    if isinstance(requested, int | float) and math.isfinite(requested):
        delay = max(delay, requested)
    detail = (failure or {}).get(
        "message", "Execution slice finished with work remaining"
    )
    body = (
        f"Automatically continuing this task: {detail}. "
        "Resume from retained results and pending job IDs; reconcile uncertain mutations before any new submission. "
        "Repair rejected arguments from their exact violations. If an approach repeatedly fails, change it, "
        "use smaller executable steps, or return a scoped technical blocker to your supervisor. "
        "Routine recovery requires no operator response."
    )
    return SessionMessage(
        f"message-{uuid.uuid4().hex}",
        "incoming",
        MessageContent(
            body,
            "runtime",
            result={
                "continuation": {"attempt": attempt, "resume_at": now + delay},
                **({"failure": dict(failure)} if failure is not None else {}),
            },
        ),
    ), attempt


def supervisor_anchor(store: SubAgentStore, anchor: Anchor) -> Anchor | None:
    """Skip record-only containers; suppress malformed cyclic return chains."""
    if anchor.sub_agent_id is None:
        return None
    child = store.get_sub_agent(anchor.workspace_id, anchor.sub_agent_id)
    target = None if child is None else child.returns_to_sub_agent_id
    visited = {anchor.sub_agent_id}
    while target:
        if target in visited:
            return None
        visited.add(target)
        parent = store.get_sub_agent(anchor.workspace_id, target)
        if parent is None or parent.role not in RECORD_ANCHOR_ROLES:
            break
        target = parent.parent_sub_agent_id
    return Anchor(anchor.workspace_id, anchor.chat_id, target)


def recovery_notice(anchor: Anchor, failure: Mapping[str, object]) -> SessionMessage:
    """Expose explicit technical evidence, never the child's private transcript."""
    return SessionMessage(
        f"recovery-notice-{uuid.uuid4().hex}",
        "incoming",
        MessageContent(
            f"Child {anchor.sub_agent_id} encountered a technical interruption and is automatically continuing. "
            "Inspect retained outcomes and redirect if useful; do not duplicate pending submissions.",
            "runtime",
            result={
                "childRecovery": {
                    "sub_agent_id": anchor.sub_agent_id,
                    "failure": dict(failure),
                }
            },
        ),
    )
