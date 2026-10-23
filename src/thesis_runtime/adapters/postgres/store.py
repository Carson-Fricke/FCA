"""Indexed JSON records and short, fenced PostgreSQL transactions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, LiteralString, cast

from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from thesis_runtime.storage.ownership import OwnershipLost, held_claims

SCHEMA = """
CREATE TABLE IF NOT EXISTS thesis_documents (
 collection text NOT NULL,
 workspace_id text NOT NULL,
 id text NOT NULL,
 body jsonb NOT NULL,
 revision bigint NOT NULL DEFAULT 1,
 record_type text GENERATED ALWAYS AS (body->>'recordType') STORED,
 chat_id text GENERATED ALWAYS AS (body->>'chatId') STORED,
 conversation_id text GENERATED ALWAYS AS (body->>'conversationId') STORED,
 root_conversation_id text GENERATED ALWAYS AS (body->>'rootConversationId') STORED,
 thread_id text GENERATED ALWAYS AS (body->>'threadId') STORED,
 execution_id text GENERATED ALWAYS AS (body->>'executionId') STORED,
 kind text GENERATED ALWAYS AS (body->>'kind') STORED,
 seq bigint GENERATED ALWAYS AS ((body->>'seq')::bigint) STORED,
 input_position bigint GENERATED ALWAYS AS ((body->>'inputPosition')::bigint) STORED,
 state text GENERATED ALWAYS AS (COALESCE(body->>'state',body#>>'{payload,state}')) STORED,
 resource text GENERATED ALWAYS AS (body->>'resource') STORED,
 created_at text GENERATED ALWAYS AS (COALESCE(body->>'createdAt',body->>'ts','')) STORED,
 PRIMARY KEY (collection, workspace_id, id)
);
CREATE INDEX IF NOT EXISTS thesis_record_scope ON thesis_documents
 (collection,workspace_id,record_type,created_at,id);
CREATE INDEX IF NOT EXISTS thesis_global_record ON thesis_documents
 (collection,record_type,id);
CREATE INDEX IF NOT EXISTS thesis_timeline ON thesis_documents
 (workspace_id,chat_id,seq) WHERE seq IS NOT NULL;
CREATE INDEX IF NOT EXISTS thesis_thread_timeline ON thesis_documents
 (workspace_id,chat_id,thread_id,seq) WHERE seq IS NOT NULL;
CREATE INDEX IF NOT EXISTS thesis_inbox ON thesis_documents
 (workspace_id,root_conversation_id,conversation_id,input_position)
 WHERE record_type='ChatMessage';
CREATE INDEX IF NOT EXISTS thesis_work ON thesis_documents
 (state,created_at,workspace_id,id) WHERE record_type='RuntimeWork';
CREATE INDEX IF NOT EXISTS thesis_execution_events ON thesis_documents
 (execution_id,kind,seq) WHERE execution_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS thesis_conversation_records ON thesis_documents
 (workspace_id,root_conversation_id,record_type,conversation_id);
CREATE INDEX IF NOT EXISTS thesis_output_message ON thesis_documents
 (workspace_id,chat_id,(body#>>'{payload,messageId}'),seq) WHERE kind='output';
CREATE INDEX IF NOT EXISTS thesis_ownership_resource ON thesis_documents
 (collection,workspace_id,resource) WHERE record_type='Ownership';
"""


class DocumentConflict(Exception):
    """A create identity or compare-and-swap revision already changed."""


def identity(prefix: str, value: str) -> str:
    return prefix + hashlib.sha256(value.encode()).hexdigest()


class PostgresStore:
    """One shared pool; native SQL, with no Cosmos query or SDK emulation."""

    def __init__(self, dsn: str, *, initialize: bool = True) -> None:
        self.pool: ConnectionPool = ConnectionPool(
            dsn,
            min_size=1,
            max_size=16,
            timeout=10,
            kwargs={"autocommit": True, "options": "-c statement_timeout=10000"},
            open=True,
        )
        self.pool.wait(timeout=15)
        if initialize:
            with self.pool.connection() as connection:
                connection.execute(SCHEMA)

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def transaction(
        self, workspace_id: str, *, chat_id: str | None = None, fenced: bool = True
    ) -> Iterator[Connection]:
        with self.pool.connection() as connection, connection.transaction():
            # Serialize only short writes within the ownership partition. All
            # callers acquire this same lock before root/counter/lease rows.
            lock = int.from_bytes(
                hashlib.sha256(workspace_id.encode()).digest()[:8], signed=True
            )
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (lock,))
            if fenced:
                for held in held_claims():
                    owner = held.store
                    if (
                        held.lost.is_set()
                        or getattr(owner, "store", None) is not self
                        or held.claim.workspace_id != workspace_id
                    ):
                        raise OwnershipLost(
                            "durable mutation crossed its ownership partition"
                        )
                    lease = self.read(
                        workspace_id,
                        identity("Ownership:", held.claim.resource),
                        connection=connection,
                    )
                    row = connection.execute(
                        "SELECT extract(epoch FROM clock_timestamp())"
                    ).fetchone()
                    assert row is not None
                    if (
                        lease is None
                        or lease["token"] != held.claim.token
                        or lease["fence"] != held.claim.fence
                        or lease["expiresAt"] <= float(row[0])
                    ):
                        raise OwnershipLost("stale ownership: " + held.claim.resource)
            if chat_id:
                root = self.read(
                    workspace_id, "ChatThread:" + chat_id, connection=connection
                )
                if root is not None and root["payload"]["state"] == "deleted":
                    from thesis_runtime.contracts.records import ChatContractError

                    raise ChatContractError("conversation has been deleted")
            yield connection

    def read(
        self,
        workspace_id: str,
        item_id: str,
        *,
        collection: str = "catalog",
        connection: Connection | None = None,
    ) -> dict[str, Any] | None:
        if connection is None:
            with self.pool.connection() as current:
                return self.read(
                    workspace_id, item_id, collection=collection, connection=current
                )
        row = connection.execute(
            "SELECT body,revision FROM thesis_documents WHERE collection=%s AND workspace_id=%s AND id=%s",
            (collection, workspace_id, item_id),
        ).fetchone()
        return None if row is None else {**row[0], "_etag": str(row[1])}

    def query(
        self,
        where: str,
        parameters: Sequence[object] = (),
        *,
        columns: str = "body",
        order: str = "",
        limit: int | None = None,
        collection: str = "catalog",
        connection: Connection | None = None,
    ) -> list[Any]:
        # SQL fragments are adapter-owned constants; untrusted values are
        # always bound parameters. Every query retains its collection scope.
        sql = (
            f"SELECT {columns} FROM thesis_documents WHERE collection=%s AND ({where})"
        )
        args = [collection, *parameters]
        if order:
            sql += " ORDER BY " + order
        if limit is not None:
            sql += " LIMIT %s"
            args.append(limit)
        if connection is None:
            with self.pool.connection() as current:
                return self.query(
                    where,
                    parameters,
                    columns=columns,
                    order=order,
                    limit=limit,
                    collection=collection,
                    connection=current,
                )
        return [
            row[0] if columns == "body" else row
            for row in connection.execute(cast(LiteralString, sql), args)
        ]

    def put(
        self,
        connection: Connection,
        document: Mapping[str, Any],
        *,
        collection: str = "catalog",
        create: bool = False,
        expected: str | None = None,
    ) -> None:
        body = {
            key: value for key, value in document.items() if not key.startswith("_")
        }
        args: list[object] = [collection, body["workspaceId"], body["id"], Jsonb(body)]
        sql = "INSERT INTO thesis_documents (collection,workspace_id,id,body) VALUES (%s,%s,%s,%s)"
        if expected is not None:
            sql = "UPDATE thesis_documents SET body=%s,revision=revision+1 WHERE collection=%s AND workspace_id=%s AND id=%s AND revision=%s"
            args = [
                Jsonb(body),
                collection,
                body["workspaceId"],
                body["id"],
                int(expected),
            ]
        elif create:
            sql += " ON CONFLICT DO NOTHING"
        else:
            sql += " ON CONFLICT (collection,workspace_id,id) DO UPDATE SET body=excluded.body,revision=thesis_documents.revision+1"
        if connection.execute(cast(LiteralString, sql), args).rowcount != 1:
            raise DocumentConflict(str(body["id"]))
        connection.execute(
            "SELECT pg_notify('thesis_commits',%s)",
            (
                json.dumps(
                    {
                        "workspace": body["workspaceId"],
                        "chat": body.get("chatId"),
                        "work": body.get("recordType") == "RuntimeWork",
                    }
                ),
            ),
        )

    def delete(
        self,
        connection: Connection,
        workspace_id: str,
        item_id: str,
        *,
        collection: str = "catalog",
    ) -> None:
        connection.execute(
            "DELETE FROM thesis_documents WHERE collection=%s AND workspace_id=%s AND id=%s",
            (collection, workspace_id, item_id),
        )
