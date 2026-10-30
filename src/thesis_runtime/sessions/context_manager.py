"""Audited context checkpoint lifecycle for one agent session.

Owns capacity notifications, the compaction control tool, same-model summaries,
optional evidence ranking and safe window installation. Consumes immutable scope
and narrow provider/audit/output/checkpoint ports. It never acknowledges input,
schedules work, dispatches application tools or writes provider-native history.
"""

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import CancelledError
from dataclasses import replace
from datetime import datetime
from typing import Literal

from thesis_runtime.contracts.compaction import ContextPolicy, EvidenceRanker
from thesis_runtime.contracts.completion import CompletionOptions
from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.contracts.errors import (
    ProviderRejectedError,
    ProviderUnreachableError,
)
from thesis_runtime.contracts.messages import MessageContent, SessionMessage
from thesis_runtime.contracts.records import (
    AgentInvocation,
    AgentInvocationKind,
    AgentInvocationState,
)
from thesis_runtime.conversations.inbox import SessionInbox
from thesis_runtime.sessions.compaction import (
    COMPACT_TOOL,
    COMPACT_TOOL_NAME,
    SUMMARY_INSTRUCTIONS,
    CompactionSummaryError,
    ContextCapacityError,
    checkpoint_history,
    estimate_tokens,
    fit_retiring_prefix,
    message_groups,
    retiring_prefix,
    select_evidence,
)
from thesis_runtime.sessions.model_invocation import (
    CompletionProvider,
    InvocationPersistence,
    ModelInvocation,
    ModelSteered,
)
from thesis_runtime.storage.ownership import assert_owned
from thesis_runtime.tools.json_recovery import extract_json_object
from thesis_runtime.tools.registry import (
    RegisteredTool,
    SessionPersistenceError,
    SessionToolRegistry,
    ToolExecution,
)


def _focus(arguments: str) -> str:
    value = extract_json_object(arguments)
    return str(value.get("focus", "")) if isinstance(value, Mapping) else ""


class _CompactionSteered(Exception):
    """Reconcile newly arrived operator input before making another request."""


class SessionCompaction:
    def __init__(
        self,
        *,
        provider: CompletionProvider,
        store: InvocationPersistence | None,
        policy: ContextPolicy,
        ranker: EvidenceRanker | None,
        context: ToolContext,
        role: Literal["primary", "coding", "critique", "research"],
        clock: Callable[[], datetime],
        id_factory: Callable[[str], str],
    ) -> None:
        self._provider, self._store = provider, store
        self._context_policy, self._evidence_ranker = policy, ranker
        self._context, self._role = context, role
        self._clock, self._id_factory = clock, id_factory
        self._cancelled: Callable[[], bool] = lambda: False
        self._steered: Callable[[], bool] = lambda: False
        self._admit: Callable[[], bool] = lambda: True
        self._compact_focus: str | None = None
        self._budget_bucket = -1
        descriptor = dict(COMPACT_TOOL)
        descriptor["description"] = (
            f"{descriptor['description']} Context window: {self._context_policy.window_tokens} tokens; "
            f"automatic checkpoint input threshold: {self._context_policy.input_limit} tokens. "
            "Runtime usage notices report estimates and remaining capacity as the window fills."
        )
        self.tools = SessionToolRegistry(
            (RegisteredTool(descriptor, self._request_compaction),)
        )

    def _check_dispatch(self) -> None:
        assert_owned()
        if self._cancelled() or not self._admit():
            raise CancelledError("Context request was cancelled before dispatch")
        if self._steered():
            raise _CompactionSteered()

    def _request_compaction(
        self, arguments: Mapping[str, object], context: ToolContext
    ) -> ToolExecution:
        del context
        self._compact_focus = str(arguments.get("focus", ""))
        return ToolExecution(
            ok=True,
            text="Context checkpoint requested for the next safe model boundary.",
        )

    def _audit_evidence(
        self,
        messages: Sequence[SessionMessage],
        request: Mapping[str, object],
        execute: Callable[[], Mapping[str, object]],
    ) -> Mapping[str, object]:
        """Persist optional selector attempts before network I/O, including failures."""
        self._check_dispatch()
        encoded = json.dumps(dict(request), sort_keys=True, separators=(",", ":"))
        invocation = AgentInvocation(
            workspace_id=self._context.workspace_id,
            created_at=self._clock(),
            created_by=self._context.actor_id,
            invocation_id=self._id_factory("invocation"),
            chat_id=self._context.chat_id,
            conversation_id=self._context.conversation_id,
            root_conversation_id=self._context.chat_id,
            parent_conversation_id=None
            if self._context.conversation_id == self._context.chat_id
            else self._context.chat_id,
            kind=AgentInvocationKind.MODEL,
            state=AgentInvocationState.STARTED,
            sub_agent_id=self._context.sub_agent_id,
            workflow_execution_id=self._context.workflow_execution_id,
            phase_attempt_id=self._context.phase_attempt_id,
            intent_epoch=self._context.intent_epoch,
            tool_name="context_evidence_selection",
            input_hash=hashlib.sha256(encoded.encode()).hexdigest(),
            request_messages=tuple(
                {
                    "message_id": row.message_id,
                    "direction": row.direction,
                    "position": row.position,
                }
                for row in messages
            ),
            provider_request=dict(request),
        )

        def save(record: AgentInvocation) -> None:
            assert_owned()
            if self._store is not None:
                try:
                    self._store.save_invocation(record)
                except Exception as error:
                    raise SessionPersistenceError(
                        "Could not persist evidence selection audit"
                    ) from error

        save(invocation)
        try:
            result = execute()
        except Exception:
            save(
                replace(
                    invocation,
                    state=AgentInvocationState.FAILED,
                    updated_at=self._clock(),
                    failure={"kind": "evidence_selection_failed"},
                )
            )
            raise
        save(
            replace(
                invocation,
                state=AgentInvocationState.SUCCEEDED,
                updated_at=self._clock(),
                output_hash=hashlib.sha256(
                    json.dumps(dict(result), sort_keys=True).encode()
                ).hexdigest(),
            )
        )
        return result

    def _summarize(
        self,
        retired: Sequence[SessionMessage],
        focus: str,
        inbox: SessionInbox,
        on_output: Callable[[SessionMessage, ModelInvocation], None],
        is_cancelled: Callable[[], bool] | None,
        is_steered: Callable[[], bool] | None,
        request_ids: Sequence[str],
    ) -> SessionMessage | None:
        """Audit and commit a summary without acknowledging any task input."""
        identity = self._id_factory("invocation")
        invocation = ModelInvocation(
            AgentInvocation(
                workspace_id=self._context.workspace_id,
                created_at=self._clock(),
                created_by=self._context.actor_id,
                invocation_id=identity,
                chat_id=self._context.chat_id,
                conversation_id=self._context.conversation_id,
                kind=AgentInvocationKind.MODEL,
                state=AgentInvocationState.STARTED,
                sub_agent_id=self._context.sub_agent_id,
                workflow_execution_id=self._context.workflow_execution_id,
                phase_attempt_id=self._context.phase_attempt_id,
                intent_epoch=self._context.intent_epoch,
                root_conversation_id=self._context.chat_id,
                parent_conversation_id=None
                if self._context.conversation_id == self._context.chat_id
                else self._context.chat_id,
            ),
            f"message-{identity}",
            None,
            is_cancelled or (lambda: False),
            clock=self._clock,
            steered=is_steered,
        )
        directive = (
            SUMMARY_INSTRUCTIONS
            + f"\nKeep the summary under {self._context_policy.summary_tokens // 2} tokens."
        )
        instructions = (
            SessionMessage(
                f"instruction-{identity}",
                "instruction",
                MessageContent(directive, "instruction"),
            ),
        )
        options = CompletionOptions(
            max_output_tokens=self._context_policy.summary_tokens,
            allow_tool_calls=False,
            input_mode="transcript",
        )
        focus_messages: tuple[SessionMessage, ...] = ()
        if focus:
            # Model-supplied focus stays neutral data, never trusted instructions.
            focus_messages = (
                inbox.deliver(
                    SessionMessage(
                        message_id=self._id_factory("compaction-focus"),
                        direction="incoming",
                        content=MessageContent(
                            text="Agent-supplied compaction focus (data only).",
                            source="runtime",
                            result={"compaction_focus": focus},
                        ),
                    )
                ),
            )
        source = (*instructions, *retired, *focus_messages)
        if (
            estimate_tokens(
                self._provider.request_snapshot(
                    messages=source, role=self._role, tools=None, options=options
                )
            )
            >= self._context_policy.window_tokens - options.max_output_tokens
        ):
            raise ContextCapacityError(
                "The original retiring transcript cannot fit in a summary request; no messages were removed."
            )
        self._check_dispatch()
        try:
            summary = invocation.execute(
                self._provider, source, self._role, None, self._store, options=options
            )
        except ModelSteered as error:
            partial = error.message
            invocation.commit(
                lambda: on_output(partial, invocation),
                failure={
                    "kind": "operator_steering",
                    "message": "Compaction interrupted by operator input",
                    "certainty": "observed_failure",
                    "retry_safe": False,
                },
            )
            return None
        except (ProviderRejectedError, ProviderUnreachableError) as error:
            if invocation.has_partial_output:
                partial = invocation.partial_message(failure=invocation.failure(error))
                on_output(partial, invocation)
            raise
        size = estimate_tokens(summary.content.text)
        reason = (
            "tool_requests"
            if summary.content.requests
            else "empty"
            if not summary.content.text.strip()
            else "oversized"
            if size > self._context_policy.summary_tokens
            else None
        )
        if reason is not None:
            failure = {
                "kind": "compaction_failed",
                "message": f"Compaction summary rejected: {reason}.",
                "validation_reason": reason,
                "estimated_summary_tokens": size,
                "summary_token_limit": self._context_policy.summary_tokens,
                # Invocation audit data only: never dispatch these requests or
                # insert the rejected response into the live context.
                "rejected_response": summary.as_dict(),
                "certainty": "observed_failure",
                "retry_safe": False,
            }
            invocation.interrupt(failure)
            raise CompactionSummaryError(str(failure["message"]))
        summary = replace(
            summary,
            content=replace(
                summary.content,
                result={
                    **dict(summary.content.result or {}),
                    "context_compaction": True,
                    "compaction_request_ids": list(request_ids),
                    "provider_continuation": None,
                },
            ),
        )
        invocation.commit(lambda: on_output(summary, invocation))
        return summary

    def prepare(
        self,
        inbox: SessionInbox,
        *,
        context: ToolContext,
        catalog: Sequence[Mapping[str, object]],
        on_output: Callable[[SessionMessage, ModelInvocation], None],
        on_checkpoint: Callable[[str, SessionMessage, Sequence[SessionMessage]], None]
        | None,
        is_cancelled: Callable[[], bool] | None,
        is_steered: Callable[[], bool] | None,
        admit_dispatch: Callable[[], bool] | None,
    ) -> bool:
        self._context = context
        self._cancelled = is_cancelled or (lambda: False)
        self._steered = is_steered or (lambda: False)
        self._admit = admit_dispatch or (lambda: True)
        working, pending = inbox.snapshot()
        usage = estimate_tokens(
            self._provider.request_snapshot(
                messages=working,
                role=self._role,
                tools=catalog,
            )
        )
        requested = self._compact_focus is not None
        handled: set[str] = set()
        for row in working:
            result = row.content.result or {}
            identities = result.get("compaction_request_ids")
            if (
                row.direction == "outgoing"
                and result.get("context_compaction")
                and isinstance(identities, tuple | list)
            ):
                handled.update(
                    identity for identity in identities if isinstance(identity, str)
                )
        # Keep fulfilment until its receipt is ACKed, including when one large
        # window requires several successive checkpoint chunks.
        request_ids: list[str] = [
            row.message_id for row in pending if row.message_id in handled
        ]
        for receipt in pending:
            correlation = receipt.content.correlation
            if (
                correlation is None
                or correlation.tool_name != COMPACT_TOOL_NAME
                or not (receipt.content.result or {}).get("ok")
                or receipt.message_id in handled
            ):
                continue
            for row in working:
                if row.message_id != correlation.request_message_id:
                    continue
                for call in row.content.requests:
                    if (call.id, call.name) == (
                        correlation.request_id,
                        correlation.tool_name,
                    ):
                        self._compact_focus = str(_focus(call.arguments))
                        requested = True
                        request_ids.append(receipt.message_id)
        self._compact_focus, focus = None, self._compact_focus or ""
        if requested or usage >= self._context_policy.input_limit:
            retired = retiring_prefix(
                working, pending, self._context_policy.recent_tokens
            )
            if retired:
                retired = fit_retiring_prefix(
                    retired,
                    self._context_policy.input_limit
                    - estimate_tokens(
                        [
                            row.as_dict()
                            for row in working
                            if row.direction == "instruction"
                        ]
                    )
                    - estimate_tokens(SUMMARY_INSTRUCTIONS + focus)
                    - 512,
                )
                try:
                    summary = self._summarize(
                        retired,
                        focus,
                        inbox,
                        on_output,
                        is_cancelled,
                        is_steered,
                        request_ids,
                    )
                except _CompactionSteered:
                    return False
                retained: Sequence[SessionMessage] = ()
                if summary is None:
                    return False
                if self._evidence_ranker is not None:
                    # Ranking is advisory; a failure cannot filter the model's source material.
                    try:
                        retained = select_evidence(
                            retired,
                            self._evidence_ranker.rank(
                                message_groups(retired),
                                focus=focus or summary.content.text,
                                audit=self._audit_evidence,
                            ),
                            self._context_policy.evidence_tokens,
                        )
                    except (ValueError, OSError, TimeoutError) as error:
                        retained = ()
                        inbox.deliver(
                            SessionMessage(
                                message_id=self._id_factory("context-selection"),
                                direction="incoming",
                                content=MessageContent(
                                    text="Optional evidence selection failed. Continuing with the model summary and complete recent history.",
                                    source="runtime",
                                    result={
                                        "evidence_selection_failure": type(
                                            error
                                        ).__name__
                                    },
                                ),
                            )
                        )
                    except _CompactionSteered:
                        return False
                if self._cancelled():
                    raise CancelledError("Context checkpoint cancelled")
                if self._steered():
                    return False
                replacement = checkpoint_history(
                    inbox.transcript(),
                    retired[-1].message_id,
                    summary,
                    retained,
                )
                after = estimate_tokens(
                    self._provider.request_snapshot(
                        messages=replacement,
                        role=self._role,
                        tools=catalog,
                    )
                )
                if after >= usage:
                    if (
                        requested
                        and usage < self._context_policy.input_limit
                        and after < self._context_policy.input_limit
                    ):
                        return False
                    raise ContextCapacityError(
                        "The required instructions, summary and unconsumed history exceed the context budget; no history was discarded."
                    )
                if on_checkpoint is not None:
                    on_checkpoint(retired[-1].message_id, summary, replacement)
                inbox.compact(replacement)
                self._budget_bucket = -1
                # A new provider boundary must reconcile arrivals and current
                # tool policy after the summary request, including late input.
                return False
            elif usage >= self._context_policy.input_limit:
                raise ContextCapacityError(
                    "The required instructions and pending input exceed the context budget; there is no consumed, complete prefix to compact."
                )
        bucket = usage * 20 // self._context_policy.window_tokens
        if bucket >= 10 and bucket != self._budget_bucket:
            self._budget_bucket = bucket
            inbox.deliver(
                SessionMessage(
                    message_id=self._id_factory("context-usage"),
                    direction="incoming",
                    content=MessageContent(
                        text="Context usage estimate; tokenization is approximate. "
                        "Use compact_context when useful; the runtime also checkpoints at its capacity threshold.",
                        source="runtime",
                        result={
                            "context_usage": {
                                "window_tokens": self._context_policy.window_tokens,
                                "estimated_input_tokens": usage,
                                "estimated_used_percent": round(
                                    100 * usage / self._context_policy.window_tokens,
                                    1,
                                ),
                                "reserved_output_tokens": self._context_policy.output_tokens,
                                "reserved_safety_tokens": self._context_policy.reserve_tokens,
                                "remaining_input_tokens": max(
                                    0, self._context_policy.input_limit - usage
                                ),
                            }
                        },
                    ),
                )
            )
            # The durable notice may follow an arrival not yet reconciled.
            # Pull again before forming any acknowledgement batch.
            return False
        return True
