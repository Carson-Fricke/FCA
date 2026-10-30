"""Deliver harness terminal events to their retained conversation recipient.

The harness owns execution state. This consumer neither observes running jobs nor
submits work: it joins immutable submission attribution and uses the canonical
conversation inbox, whose durable identity makes notification retries harmless.
"""

from collections.abc import Mapping
from typing import cast

from thesis.persistence.submissions import SubmissionStore
from thesis_runtime.contracts.datum import Datum
from thesis_runtime.contracts.messages import (
    MessageContent,
    SessionMessage,
    from_record,
)
from thesis_runtime.contracts.records import (
    ChatMessage,
    RecordNotFound,
    WorkflowExecutionState,
)
from thesis_runtime.conversations.conversation import Conversations
from thesis_runtime.conversations.delivery import Anchor


def deliver_job_completion(
    submissions: SubmissionStore,
    conversations: Conversations,
    result: Mapping[str, object],
) -> None:
    """Accept one authenticated terminal event; unknown/non-chat jobs are ignored."""
    job_id = cast(str, result["job_id"])
    if result.get("state") not in {"succeeded", "failed", "cancelled"}:
        raise ValueError("completion requires terminal job evidence")
    submission = submissions.find(job_id)
    if submission is None:
        return
    inputs = cast(Mapping[str, object], submission["inputs"])
    recipient = inputs.get("recipient")
    if not isinstance(recipient, Mapping):
        return
    workspace_id = cast(str, submission["workspace_id"])
    if recipient.get("workspaceId") != workspace_id:
        raise ValueError("completion recipient has a different workspace")
    chat_id = cast(str, recipient["chatId"])
    try:
        conversations.chat.get_chat(workspace_id, chat_id)
    except RecordNotFound:
        return
    conversation_id = cast(str, recipient["conversationId"])
    anchor = Anchor(
        workspace_id, chat_id, None if conversation_id == chat_id else conversation_id
    )
    key = f"job-completion:{job_id}"
    previous = conversations.chat.persistence.get(ChatMessage, workspace_id, key)
    if previous is not None:
        prior = from_record(previous).content.result or {}
        datums = prior.get("datums", ())
        if (
            not isinstance(datums, (list, tuple))
            or len(datums) != 1
            or datums[0].get("structured") != dict(result)
        ):
            raise ValueError("terminal job completion conflicts with retained evidence")
        return
    schedule = True
    execution_id = recipient.get("workflowExecutionId")
    if isinstance(execution_id, str):
        execution = conversations.chat.get_execution(workspace_id, execution_id)
        schedule = execution.state in {
            WorkflowExecutionState.QUEUED,
            WorkflowExecutionState.RUNNING,
        } and execution.intent_epoch == recipient.get("intentEpoch")
        tracked = conversations.chat.find_tool_job(workspace_id, job_id)
        if tracked is not None and tracked.result != dict(result):
            conversations.chat.record_tool_result(
                workspace_id,
                job_id,
                result=result,
                success=result["state"] == "succeeded",
                error_message=cast(str | None, result.get("error_message")),
                intent_epoch=cast(int, recipient["intentEpoch"]),
            )
    datum = Datum(
        "job_completion", ok=result["state"] == "succeeded", structured=dict(result)
    )
    conversations.deliver_session_message(
        anchor,
        SessionMessage(
            key,
            "incoming",
            MessageContent(
                f"Job {job_id} {result['state']}.",
                "job_completion",
                result={
                    "ok": datum.ok,
                    "completedJobs": [job_id],
                    "datums": [datum.as_dict()],
                },
            ),
        ),
        datums=(datum,),
        idempotency_key=key,
        actor_id="harness",
        execution_id=cast(str | None, execution_id),
        schedule=schedule,
    )
