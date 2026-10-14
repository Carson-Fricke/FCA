"""Consume one model stream; durable progress is separate from canonical input."""

import hashlib
import json
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import closing
from contextvars import copy_context
from dataclasses import replace
from datetime import UTC, datetime
from threading import Condition, Thread
from time import monotonic
from typing import Literal, Protocol

from thesis_runtime.contracts.errors import (
    ProviderRejectedError,
    ProviderUnreachableError,
)
from thesis_runtime.contracts.messages import MessageContent, SessionMessage
from thesis_runtime.contracts.output import OutputDelta
from thesis_runtime.contracts.records import (
    AgentInvocation,
    AgentInvocationKind,
    AgentInvocationState,
)
from thesis_runtime.conversations.timeline import TimelineWriter
from thesis_runtime.storage.ownership import OwnershipLost


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CompletionProvider(Protocol):
    def request_snapshot(
        self,
        *,
        messages: Sequence[SessionMessage],
        role: Literal["primary", "coding", "critique", "research"],
        tools: Sequence[Mapping[str, object]] | None = None,
        response_format: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]: ...

    def stream(
        self,
        *,
        messages: Sequence[SessionMessage],
        message_id: str,
        role: Literal["primary", "coding", "critique", "research"],
        tools: Sequence[Mapping[str, object]] | None = None,
        response_format: Mapping[str, object] | None = None,
    ) -> Generator[OutputDelta | SessionMessage, None, None]: ...


class InvocationPersistence(Protocol):
    """The model lifecycle needs no access to subagents or their transcripts."""

    def save_invocation(self, invocation: AgentInvocation) -> AgentInvocation: ...


class ModelSteered(Exception):
    """Generation stopped for new operator input, with its partial output retained."""

    def __init__(self, message: SessionMessage) -> None:
        super().__init__("Model generation interrupted by operator steering.")
        self.message = message


class ModelInvocation:
    """One stable output identity, bounded batching and explicit interruption.

    The canonical outgoing message is the completion record. Callers persist it
    before marking the invocation successful or acknowledging its input snapshot.
    Network iteration supplies backpressure: no unbounded producer queue exists.
    """

    def __init__(
        self,
        invocation: AgentInvocation,
        message_id: str,
        timeline: TimelineWriter | None,
        cancelled: Callable[[], bool],
        store: InvocationPersistence | None = None,
        clock: Callable[[], datetime] = _utc_now,
        steered: Callable[[], bool] | None = None,
    ) -> None:
        self.invocation = invocation
        self.message_id = message_id
        self.timeline = timeline
        self.cancelled = cancelled
        self.steered = steered
        self._partial: dict[str, list[str]] = {"reasoning": [], "text": []}
        self._offsets = {"reasoning": 0, "text": 0}
        self._pending: list[OutputDelta] = []
        self._size = 0
        self._has_flushed = False
        self._store = store
        self._clock = clock
        self.started_at = invocation.created_at
        self.duration_ms = 0
        self._condition = Condition()
        self._publisher_stopped = False
        self._publish_error: BaseException | None = None
        self.failure_committed = False

    def _publish(self) -> None:
        with self._condition:
            while not self._publisher_stopped:
                self._condition.wait_for(
                    lambda: self._pending or self._publisher_stopped
                )
                if self._publisher_stopped:
                    return
                self._condition.wait(0.1)
                if self._pending:
                    try:
                        self.flush()
                    except BaseException as error:
                        self._publish_error = error
                        return

    def execute(
        self,
        provider: CompletionProvider,
        working: Sequence[SessionMessage],
        role: Literal["primary", "coding", "critique", "research"],
        tools: Sequence[Mapping[str, object]] | None,
        store: InvocationPersistence | None,
    ) -> SessionMessage:
        """Audit the exact request before execution; retain it in every terminal update."""
        self._provider = provider
        self._store = store
        request_messages = tuple(
            {
                "message_id": message.message_id,
                "direction": message.direction,
                "position": message.position,
            }
            for message in working
        )
        provider_request = self._provider.request_snapshot(
            messages=working, role=role, tools=tools
        )
        if not isinstance(provider_request, Mapping):
            raise TypeError("provider request snapshot must be a mapping")
        request_json = json.dumps(
            dict(provider_request),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        provider_request = json.loads(request_json)
        input_hash = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        self._record_invocation(
            kind=AgentInvocationKind.MODEL,
            state=AgentInvocationState.STARTED,
            request_messages=request_messages,
            provider_request=provider_request,
            input_hash=input_hash,
        )
        try:
            return self.run(
                self._provider.stream(
                    messages=working, message_id=self.message_id, role=role, tools=tools
                )
            )
        except (OwnershipLost, ModelSteered):
            raise
        except Exception as error:
            self.interrupt(self.failure(error))
            raise

    @staticmethod
    def failure(error: Exception) -> dict[str, object]:
        failure: dict[str, object] = {
            "kind": "provider_rejected"
            if isinstance(error, ProviderRejectedError)
            else "provider_unreachable"
            if isinstance(error, ProviderUnreachableError)
            else "runtime_fault",
            "message": str(error) or type(error).__name__,
            "certainty": "observed_failure"
            if isinstance(error, ProviderRejectedError)
            else "unknown_outcome",
            "retry_safe": False,
        }
        if isinstance(error, ProviderRejectedError):
            failure["status"] = error.status
            if error.retry_after is not None:
                failure["retry_after"] = error.retry_after
        if isinstance(error, (ProviderRejectedError, ProviderUnreachableError)):
            failure["recoverable"] = error.recoverable
        return failure

    def _record_invocation(
        self,
        *,
        kind: AgentInvocationKind,
        state: AgentInvocationState,
        request_messages: tuple[Mapping[str, object], ...],
        provider_request: Mapping[str, object],
        input_hash: str,
    ) -> None:
        self.invocation = replace(
            self.invocation,
            kind=kind,
            state=state,
            request_messages=request_messages,
            provider_request=provider_request,
            input_hash=input_hash,
        )
        if self._store is not None:
            self._store.save_invocation(self.invocation)

    def commit(
        self,
        persist: Callable[[], None],
        *,
        failure: Mapping[str, object] | None = None,
    ) -> None:
        """The canonical message closes progress; only then may success be recorded."""
        try:
            persist()
        except OwnershipLost:
            raise
        except Exception as error:
            self.interrupt(self.failure(error))
            raise
        if failure is not None:
            self.interrupt(failure)
            return
        self.invocation = replace(
            self.invocation,
            state=AgentInvocationState.SUCCEEDED,
            updated_at=self._clock(),
        )
        if self._store is not None:
            self._store.save_invocation(self.invocation)

    def _write(self, state: str, **fields: object) -> None:
        if self.timeline is None:
            return
        invocation = self.invocation
        identity = (
            state if state != "delta" else f"{fields['channel']}:{fields['offset']}"
        )
        self.timeline.append(
            workspace_id=invocation.workspace_id,
            chat_id=invocation.chat_id,
            kind="output",
            item_id=f"ModelOutput:{invocation.invocation_id}:{identity}",
            thread_id=invocation.sub_agent_id or "root",
            execution_id=invocation.workflow_execution_id,
            payload={
                "invocationId": invocation.invocation_id,
                "messageId": self.message_id,
                "actorId": invocation.created_by,
                "state": state,
                **fields,
            },
        )

    def flush(self) -> None:
        with self._condition:
            if self._publish_error is not None:
                raise self._publish_error
            self._flush_locked()

    def _flush_locked(self) -> None:
        # Preserve channel order, coalescing adjacent fragments only.
        while self._pending:
            first = self._pending[0]
            text = first.text
            count = 1
            while (
                count < len(self._pending)
                and self._pending[count].channel == first.channel
            ):
                text += self._pending[count].text
                count += 1
            offset = self._offsets[first.channel]
            self._write("delta", channel=first.channel, offset=offset, text=text)
            del self._pending[:count]
            self._offsets[first.channel] += len(text.encode("utf-8"))
        self._size = 0
        self._has_flushed = True

    def interrupt(self, failure: Mapping[str, object]) -> None:
        # A failed publisher must not mask the initiating persistence failure
        # again when closing the invocation. Its buffer remains uncommitted.
        if self._publish_error is None:
            self.flush()
        self._write("interrupted", failure=dict(failure))
        self.invocation = replace(
            self.invocation,
            state=AgentInvocationState.FAILED,
            failure=dict(failure),
            updated_at=self._clock(),
        )
        if self._store is not None:
            self._store.save_invocation(self.invocation)
        self.failure_committed = True

    @property
    def has_partial_output(self) -> bool:
        return any("".join(parts).strip() for parts in self._partial.values())

    def partial_message(
        self,
        completion: SessionMessage | None = None,
        *,
        failure: Mapping[str, object] | None = None,
    ) -> SessionMessage:
        """Seal received output as evidence, never as executable tool requests."""
        reasoning = "".join(self._partial["reasoning"])
        text = "".join(self._partial["text"])
        if completion is not None:
            text = completion.content.text
            reasoning = str(
                (completion.content.result or {}).get("reasoning") or reasoning
            )
        return SessionMessage(
            self.message_id,
            "outgoing",
            MessageContent(
                text
                if text.strip()
                else (
                    "Model generation interrupted by provider failure."
                    if failure is not None
                    else "Model turn interrupted by new operator input."
                ),
                "model",
                result={
                    "reasoning": reasoning,
                    "interrupted": True,
                    **({"failure": dict(failure)} if failure is not None else {}),
                },
            ),
        )

    def run(
        self, stream: Generator[OutputDelta | SessionMessage, None, None]
    ) -> SessionMessage:
        self.started_at = self._clock()
        started = monotonic()
        self._write("started", startedAt=self.started_at.isoformat())
        # The publisher inherits the worker's ownership fence. It only flushes
        # the bounded buffer; the session remains the sole stream consumer.
        publisher = Thread(
            target=copy_context().run,
            args=(self._publish,),
            name="model-output",
            daemon=True,
        )
        publisher.start()
        try:
            return self._consume(stream)
        finally:
            with self._condition:
                self._publisher_stopped = True
                self._condition.notify_all()
            publisher.join()
            self.flush()
            self.duration_ms = int((monotonic() - started) * 1000)

    def _consume(
        self, stream: Generator[OutputDelta | SessionMessage, None, None]
    ) -> SessionMessage:
        completion: SessionMessage | None = None
        next_cancel_check = 0.0
        with closing(stream):
            for event in stream:
                check_input = monotonic() >= next_cancel_check
                if check_input:
                    if self.cancelled():
                        raise ProviderRejectedError("model invocation was cancelled")
                    next_cancel_check = monotonic() + 0.25
                if completion is not None:
                    raise ProviderRejectedError(
                        "provider emitted output after completion"
                    )
                if isinstance(event, SessionMessage):
                    if (
                        event.direction != "outgoing"
                        or event.message_id != self.message_id
                    ):
                        raise ProviderRejectedError(
                            "provider returned an invalid output identity"
                        )
                    if not event.content.text.strip() and not event.content.requests:
                        raise ProviderRejectedError(
                            "provider returned an empty completion without requests"
                        )
                    completion = event
                    if self.steered is not None and self.steered():
                        raise ModelSteered(self.partial_message(completion))
                    continue
                if not isinstance(event, OutputDelta):
                    raise ProviderRejectedError(
                        "provider emitted an invalid output fragment"
                    )
                self._partial[event.channel].append(event.text)
                for start in range(0, len(event.text), 4096):
                    fragment = OutputDelta(
                        event.channel, event.text[start : start + 4096]
                    )
                    with self._condition:
                        if self._publish_error is not None:
                            raise self._publish_error
                        was_empty = not self._pending
                        self._pending.append(fragment)
                        self._size += len(fragment.text)
                        if not self._has_flushed or self._size >= 4096:
                            self.flush()
                        elif was_empty:
                            self._condition.notify_all()
                if check_input and self.steered is not None and self.steered():
                    raise ModelSteered(self.partial_message())
        self.flush()
        if completion is None:
            raise ProviderRejectedError("provider stream ended without a completion")
        return completion
