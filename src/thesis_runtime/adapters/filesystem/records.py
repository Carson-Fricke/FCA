"""Filesystem-backed chat persistence, used by restart-durability tests.

The chat service persists immutable record snapshots through the
:class:`~thesis_runtime.conversations.service.ChatPersistence` port.  The served application
persists chats in Cosmos (``thesis_runtime.adapters.cosmos.records``).  This adapter writes each
snapshot as one atomically-replaced JSON document under a workspace-scoped root,
so tests can exercise reload after a simulated restart.

Layout::

    <root>/<RecordType>/<sha256(workspace_id NUL record_id)>.json

The filename is content-independent (a stable hash of the record identity), so
records can be overwritten in place and never leak record ids that contain path
separators.  ``list`` filters every document in a record-type directory by its
stored ``workspace_id``; the local dataset is small enough that a directory scan
is the simplest correct index.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import cast

from thesis_runtime.contracts.codec import (
    _RECORD_TYPES,
    Record,
    RecordT,
    RecordType,
    _ensure_supported,
    _from_payload,
    _payload,
    _record_id,
    _type_from_name,
    ensure_ownership_unchanged,
)
from thesis_runtime.contracts.records import (
    ChatContractError,
    ChatMessage,
    ChatState,
    ChatThread,
    ConversationRecord,
    replace_record,
)
from thesis_runtime.storage.locks import conversation_lock


def _atomic_write(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` atomically via a same-directory temp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=".chat-", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class FileChatPersistence:
    """Durable JSON-document persistence under one workspace root.

    Satisfies the structural :class:`~thesis_runtime.conversations.service.ChatPersistence`
    protocol, so it drops into :class:`~thesis_runtime.conversations.service.ChatService` in place
    of :class:`~thesis_runtime.conversations.service.InMemoryChatPersistence`.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        """Return the durable root directory."""
        return self._root

    def _type_dir(self, record_type: RecordType) -> Path:
        _ensure_supported(record_type)
        return self._root / record_type.__name__

    def _path(self, record_type: RecordType, workspace_id: str, record_id: str) -> Path:
        digest = hashlib.sha256(f"{workspace_id}\x00{record_id}".encode()).hexdigest()
        return self._type_dir(record_type) / f"{digest}.json"

    def save(self, record: RecordT) -> RecordT:
        if isinstance(record, ConversationRecord):
            with conversation_lock(
                str(self._root.resolve()), record.workspace_id, record.chat_id
            ):
                root = self.get(ChatThread, record.workspace_id, record.chat_id)
                if root is not None and root.state is ChatState.DELETED:
                    raise ChatContractError("conversation has been deleted")
                ensure_ownership_unchanged(
                    self.get(type(record), record.workspace_id, _record_id(record)),
                    record,
                )
                return self._save(record)
        return self._save(record)

    def admit_message(self, message: ChatMessage) -> ChatMessage:
        from thesis_runtime.conversations.admission import (
            arrival_sequence,
            existing_admission,
            message_bookkeeping,
            position_message,
            require_admission_chat,
        )

        with conversation_lock(
            str(self._root.resolve()), message.workspace_id, message.chat_id
        ):
            root = require_admission_chat(
                self.get(ChatThread, message.workspace_id, message.chat_id)
            )
            previous = self.get(ChatMessage, message.workspace_id, message.message_id)
            if previous is not None:
                return existing_admission(previous, message)
            rows = self.list_conversation(
                ChatMessage,
                message.workspace_id,
                message.chat_id,
                conversation_id=message.conversation_id,
            )
            # The record itself retains the head across restart. This small
            # single-process test adapter has no distributed runnable queue.
            all_rows = self.list_conversation(
                ChatMessage, message.workspace_id, message.chat_id
            )
            arrival = (
                max(
                    (arrival_sequence(row) for row in all_rows),
                    default=0,
                )
                + 1
            )
            admitted = position_message(
                message,
                max((row.input_position for row in rows), default=0) + 1,
                arrival_sequence=arrival,
            )
            self.save(admitted)
            self._save(message_bookkeeping(root, admitted))
            return admitted

    def update_chat(
        self,
        workspace_id: str,
        chat_id: str,
        update: Callable[[ChatThread], ChatThread],
    ) -> ChatThread:
        from thesis_runtime.conversations.admission import require_live_chat

        with conversation_lock(str(self._root.resolve()), workspace_id, chat_id):
            root = require_live_chat(self.get(ChatThread, workspace_id, chat_id))
            return self._save(update(root))

    def read_message_window(
        self,
        workspace_id: str,
        chat_id: str,
        conversation_id: str,
        *,
        after_position: int | None,
        consumed_position: int,
        limit: int,
    ) -> tuple[ChatMessage, ...]:
        from thesis_runtime.conversations.admission import select_message_window

        return select_message_window(
            self.list_conversation(
                ChatMessage, workspace_id, chat_id, conversation_id=conversation_id
            ),
            after_position=after_position,
            consumed_position=consumed_position,
            limit=limit,
        )

    def _save(self, record: RecordT) -> RecordT:
        record_type = cast(RecordType, type(record))
        _ensure_supported(record_type)
        record_id = _record_id(record)
        path = self._path(record_type, record.workspace_id, record_id)
        document = json.dumps(
            {"record": _payload(record)},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        _atomic_write(path, document)
        return record

    def get(
        self, record_type: type[RecordT], workspace_id: str, record_id: str
    ) -> RecordT | None:
        typed = cast(RecordType, record_type)
        _ensure_supported(typed)
        path = self._path(typed, workspace_id, record_id)
        if not path.is_file():
            return None
        record = self._read(path)
        if record.workspace_id != workspace_id or _record_id(record) != record_id:
            return None
        return cast(RecordT, record)

    def list(
        self, record_type: type[RecordT], workspace_id: str
    ) -> tuple[RecordT, ...]:
        typed = cast(RecordType, record_type)
        _ensure_supported(typed)
        directory = self._type_dir(typed)
        if not directory.is_dir():
            return ()
        records = [self._read(path) for path in sorted(directory.glob("*.json"))]
        selected = [
            record
            for record in records
            if record.workspace_id == workspace_id
            and not (
                isinstance(record, ChatThread) and record.state is ChatState.DELETED
            )
        ]
        selected.sort(key=lambda record: (record.created_at, _record_id(record)))
        return cast(tuple[RecordT, ...], tuple(selected))

    def list_conversation(
        self,
        record_type: type[RecordT],
        workspace_id: str,
        root_conversation_id: str,
        *,
        conversation_id: str | None = None,
    ) -> tuple[RecordT, ...]:
        if not issubclass(record_type, ConversationRecord):
            raise TypeError("record type is not conversation-owned")
        return tuple(
            record
            for record in self.list(record_type, workspace_id)
            if isinstance(record, ConversationRecord)
            and record.root_conversation_id == root_conversation_id
            and (conversation_id is None or record.conversation_id == conversation_id)
        )

    def mark_conversation_deleted(
        self,
        workspace_id: str,
        chat_id: str,
        deleted_at: datetime,
    ) -> None:
        with conversation_lock(str(self._root.resolve()), workspace_id, chat_id):
            root = self.get(ChatThread, workspace_id, chat_id)
            if root is not None and root.state is not ChatState.DELETED:
                self._save(
                    replace_record(root, state=ChatState.DELETED, updated_at=deleted_at)
                )

    def list_deleted_conversations(self, workspace_id: str) -> tuple[ChatThread, ...]:
        directory = self._type_dir(ChatThread)
        if not directory.is_dir():
            return ()
        return tuple(
            record
            for path in directory.glob("*.json")
            if isinstance((record := self._read(path)), ChatThread)
            and record.workspace_id == workspace_id
            and record.state is ChatState.DELETED
        )

    def purge_conversation(
        self,
        workspace_id: str,
        chat_id: str,
        limit: int = 100,
    ) -> bool:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        with conversation_lock(str(self._root.resolve()), workspace_id, chat_id):
            root = self.get(ChatThread, workspace_id, chat_id)
            if root is None or root.state is not ChatState.DELETED:
                raise ChatContractError("purge requires a deleted root conversation")
            remaining = limit
            for record_type in _RECORD_TYPES:
                if record_type is ChatThread or not issubclass(
                    record_type, ConversationRecord
                ):
                    continue
                for record in self.list_conversation(
                    record_type, workspace_id, chat_id
                ):
                    if remaining == 0:
                        return False
                    self.delete(record_type, workspace_id, _record_id(record))
                    remaining -= 1
            return True

    def delete(
        self, record_type: RecordType, workspace_id: str, record_id: str
    ) -> None:
        if record_type is ChatThread:
            with conversation_lock(str(self._root.resolve()), workspace_id, record_id):
                root = self.get(ChatThread, workspace_id, record_id)
                if root is not None and root.state is ChatState.DELETED:
                    raise ChatContractError("a conversation tombstone must be retained")
                self._delete(record_type, workspace_id, record_id)
            return
        self._delete(record_type, workspace_id, record_id)

    def _delete(
        self, record_type: RecordType, workspace_id: str, record_id: str
    ) -> None:
        path = self._path(record_type, workspace_id, record_id)
        try:
            path.unlink()
        except FileNotFoundError:
            return

    def _read(self, path: Path) -> Record:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, Mapping) or "record" not in document:
            raise ValueError(f"malformed chat persistence document: {path.name}")
        payload = document["record"]
        if not isinstance(payload, Mapping):
            raise ValueError(f"malformed chat persistence record: {path.name}")
        record_type = _type_from_name(path.parent.name)
        return _from_payload(record_type, payload)


__all__ = ["FileChatPersistence"]
