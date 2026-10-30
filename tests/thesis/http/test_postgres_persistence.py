"""Real PostgreSQL HTTP admission, replay, ownership and transaction behavior."""

from __future__ import annotations

import os
import secrets
import subprocess
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.sql import SQL, Identifier
from psycopg.types.json import Jsonb
from tests.thesis.http.test_stateless_ownership import Provider

from thesis.application.composition import build_serving_stack, build_serving_worker
from thesis.http.application import create_app
from thesis_runtime.adapters.postgres.stack import open_postgres_runtime
from thesis_runtime.adapters.postgres.store import PostgresStore, identity
from thesis_runtime.contracts.records import AgentInvocation, ChatMessage, MessageRole
from thesis_runtime.conversations import WorkflowDefinition
from thesis_runtime.execution.worker import Work
from thesis_runtime.storage.ownership import OwnershipLost, own
from thesis_runtime.workflows.execution_store import (
    ExecutionIdentity,
    ExecutionStoreConflict,
)


def test_navigation_and_history_are_bounded_and_details_retain_canonical_evidence(
    postgres: PostgresStore,
) -> None:
    client, persistence, _ = replica(postgres, Provider())
    try:
        chat = persistence.chat.create_chat(
            "workspace", title="Large history", owner_actor_id="operator"
        )
        turn = persistence.chat.send_message(
            "workspace",
            chat.chat_id,
            actor_id="operator",
            body="Inspect retained evidence",
        )
        receipt = persistence.chat.send_message(
            "workspace",
            chat.chat_id,
            actor_id="tool",
            role=MessageRole.TOOL,
            body="Read artifact",
            tool_result={
                "datumType": "read_artifact",
                "ok": True,
                "summary": "Read metrics",
                "failure": None,
                "structured": {
                    "job_id": "job-1",
                    "name": "metrics.json",
                    "content": "retained-evidence-" * 20_000,
                },
                "text": "retained-evidence-" * 20_000,
            },
        )
        # Arrange a large external log in one statement; the HTTP boundary must
        # select receipts before paging, even when raw activity dominates it.
        with postgres.pool.connection() as connection:
            connection.execute(
                "INSERT INTO thesis_documents(collection,workspace_id,id,body) SELECT 'catalog','workspace','fragment-'||n,"
                "jsonb_build_object('id','fragment-'||n,'workspaceId','workspace','chatId',%s::text,'kind','output','seq',n,"
                "'threadId','root','payload',jsonb_build_object('messageId',%s::text,'state','delta','text','fragment')) "
                "FROM generate_series(3,5002) n",
                (chat.chat_id, receipt.message_id),
            )
            connection.execute(
                "UPDATE thesis_documents SET body=jsonb_set(body,'{nextSeq}',%s) WHERE workspace_id='workspace' AND id=%s",
                (Jsonb(5002), "counter:" + chat.chat_id),
            )
        headers = {"Authorization": "Bearer token"}
        base = f"/v1/workspaces/workspace/chats/{chat.chat_id}"
        outline = client.get(base + "/outline", headers=headers)
        assert outline.status_code == 200, outline.text
        assert outline.json()["nextAfter"] is None
        assert [
            (entry["kind"], entry["messageId"]) for entry in outline.json()["items"]
        ] == [("turn", turn.message_id), ("artifact", receipt.message_id)]
        assert "retained-evidence-" not in outline.text
        page = client.get(base + "/timeline?thread=root", headers=headers)
        assert page.status_code == 200, page.text
        assert len(page.content) < 256 * 1024
        assert page.json()["after"] == 5002
        preview = next(
            item
            for item in page.json()["items"]
            if item["payload"].get("message_id") == receipt.message_id
        )
        assert preview["payload"]["detailsAvailable"] is True
        assert "sessionMessage" not in (preview["payload"]["data"] or {})
        detail = client.get(base + f"/timeline/items/{preview['seq']}", headers=headers)
        assert detail.status_code == 200, detail.text
        assert detail.json()["payload"] == receipt.as_dict()
        assert persistence.chat.get_message("workspace", receipt.message_id) == receipt
        assert (
            client.get(base + "/outline?after=5002", headers=headers).json()["items"]
            == []
        )
        assert (
            client.get(base + "/timeline/items/9000", headers=headers).status_code
            == 404
        )
        assert (
            client.get(
                base.replace(chat.chat_id, "missing")
                + f"/timeline/items/{preview['seq']}",
                headers=headers,
            ).status_code
            == 404
        )
        assert client.get(base + f"/timeline/items/{preview['seq']}").status_code == 401
    finally:
        persistence.timeline.commits.close()


@pytest.fixture(scope="session")
def postgres_database() -> Iterator[str]:
    """One disposable server/database; never connect tests to application data."""
    server = os.environ.get("THESIS_TEST_POSTGRES_DSN")
    container = None
    if not server:
        password = secrets.token_hex(16)
        container = "thesis-test-" + uuid4().hex[:12]
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                container,
                "-e",
                "POSTGRES_PASSWORD",
                "-p",
                "127.0.0.1::5432",
                "postgres:17",
            ],
            env={**os.environ, "POSTGRES_PASSWORD": password},
            check=True,
            capture_output=True,
        )
        address = subprocess.check_output(
            ["docker", "port", container, "5432/tcp"], text=True
        ).strip()
        server = make_conninfo(
            host="127.0.0.1",
            port=address.rsplit(":", 1)[1],
            user="postgres",
            password=password,
            dbname="postgres",
        )
    database = "test_thesis_" + uuid4().hex
    try:
        deadline = monotonic() + 20
        while True:
            try:
                with psycopg.connect(
                    server, autocommit=True, connect_timeout=1
                ) as connection:
                    connection.execute(
                        SQL("CREATE DATABASE {}").format(Identifier(database))
                    )
                break
            except psycopg.OperationalError:
                if monotonic() >= deadline:
                    raise
                sleep(0.1)
        yield make_conninfo(server, dbname=database)
    finally:
        with psycopg.connect(server, autocommit=True) as connection:
            connection.execute(
                SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    Identifier(database)
                )
            )
        if container:
            subprocess.run(
                ["docker", "stop", container], check=True, capture_output=True
            )


@pytest.fixture
def postgres(postgres_database: str) -> Iterator[PostgresStore]:
    store = PostgresStore(postgres_database)
    with store.pool.connection() as connection:
        connection.execute("TRUNCATE thesis_documents")
    yield store
    store.close()


def replica(store: PostgresStore, provider: Provider):
    persistence = open_postgres_runtime(store)
    stack = build_serving_stack(
        chat_service=persistence.chat,
        model_provider=cast(Any, provider),
        work_store=persistence.work,
    )
    worker = build_serving_worker(
        stack, persistence.work, persistence.ownership, persistence.executions
    )
    return (
        TestClient(
            create_app(
                bearer_token="token",
                conversations=stack.conversations,
                timeline=persistence.timeline,
            )
        ),
        persistence,
        worker,
    )


def test_postgres_acceptance_survives_replica_replacement_and_retains_exact_audit(
    postgres: PostgresStore,
) -> None:
    provider = Provider()
    client, first, _ = replica(postgres, provider)
    other, second, worker = replica(postgres, provider)
    _, third, rival = replica(postgres, provider)
    try:
        chat = first.chat.create_chat(
            "workspace", title="PostgreSQL", owner_actor_id="operator"
        )
        path = f"/v1/workspaces/workspace/chats/{chat.chat_id}/messages"
        body = {
            "actor_id": "operator",
            "body": "Continue",
            "message_id": "retained-input",
        }
        response = client.post(
            path, headers={"Authorization": "Bearer token"}, json=body
        )
        assert response.status_code == 200, response.text
        duplicate = other.post(
            path, headers={"Authorization": "Bearer token"}, json=body
        )
        assert duplicate.json()["input_position"] == response.json()["input_position"]
        assert len(second.work.ready()) == 1
        provider.release.clear()
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(worker.tick)
            assert provider.entered.wait(2)
            assert rival.tick() == 0
            provider.release.set()
            result.result(timeout=3)
        assert rival.tick() == 0
        replies = [
            message.body
            for message in second.chat.list_messages("workspace", chat.chat_id)
            if message.role is MessageRole.ASSISTANT
        ]
        assert replies == ["Durable reply"] and provider.calls == 1
        invocation = second.chat.persistence.list(AgentInvocation, "workspace")[0]
        assert invocation.provider_request and invocation.input_hash
        assert all(
            set(reference) == {"message_id", "direction", "position"}
            for reference in invocation.request_messages
        )
        assert (
            second.chat.read_consumed_input("workspace", chat.chat_id, "")
            == response.json()["input_position"]
        )
        page = other.get(
            path.removesuffix("/messages") + "/timeline?thread=root&limit=200",
            headers={"Authorization": "Bearer token"},
        )
        assert page.status_code == 200, page.text
        assert [item["kind"] for item in page.json()["items"]] == ["message", "message"]
        assert any(
            item["kind"] == "output"
            for item in second.timeline.read_after(
                workspace_id="workspace", chat_id=chat.chat_id
            )
        )
    finally:
        provider.release.set()
        for persistence in (first, second, third):
            persistence.timeline.commits.close()


def test_postgres_failed_admission_rolls_back_input_cursor_root_and_work(
    postgres: PostgresStore,
) -> None:
    client, persistence, _ = replica(postgres, Provider())
    client = TestClient(client.app, raise_server_exceptions=False)
    try:
        chat = persistence.chat.create_chat(
            "workspace", title="Atomic", owner_actor_id="operator"
        )
        with postgres.pool.connection() as connection:
            connection.execute(
                "CREATE FUNCTION reject_work() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.record_type='RuntimeWork' THEN RAISE EXCEPTION 'injected storage failure'; END IF; RETURN NEW; END $$"
            )
            # Generated columns are available in AFTER triggers.
            connection.execute(
                "CREATE TRIGGER reject_work AFTER INSERT ON thesis_documents FOR EACH ROW EXECUTE FUNCTION reject_work()"
            )
        path = f"/v1/workspaces/workspace/chats/{chat.chat_id}/messages"
        response = client.post(
            path,
            headers={"Authorization": "Bearer token"},
            json={
                "actor_id": "operator",
                "body": "Retain together",
                "message_id": "atomic-input",
            },
        )
        assert response.status_code == 500
        assert persistence.chat.list_messages("workspace", chat.chat_id) == ()
        assert (
            persistence.timeline.latest_sequence(
                workspace_id="workspace", chat_id=chat.chat_id
            )
            == 0
        )
        assert persistence.work.ready() == ()
        assert persistence.chat.get_chat("workspace", chat.chat_id) == chat
        with postgres.pool.connection() as connection:
            connection.execute(
                "DROP TRIGGER reject_work ON thesis_documents; DROP FUNCTION reject_work()"
            )
        response = client.post(
            path,
            headers={"Authorization": "Bearer token"},
            json={
                "actor_id": "operator",
                "body": "Retain together",
                "message_id": "atomic-input",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["input_position"] == 1
    finally:
        persistence.timeline.commits.close()


def test_postgres_stale_owner_cannot_publish_even_after_a_replacement_claim(
    postgres: PostgresStore,
) -> None:
    _, persistence, _ = replica(postgres, Provider())
    try:
        claim = persistence.ownership.acquire("workspace", "turn", 60)
        assert claim is not None
        with pytest.raises(OwnershipLost), own(persistence.ownership, claim):
            with postgres.pool.connection() as connection:
                connection.execute(
                    "UPDATE thesis_documents SET body=jsonb_set(body,'{expiresAt}','0') WHERE id=%s",
                    [identity("Ownership:", "turn")],
                )
            replacement = persistence.ownership.acquire("workspace", "turn", 60)
            assert replacement and replacement.fence > claim.fence
            persistence.work.enqueue(
                Work("workspace", "stale", "turn", "conversation", {})
            )
        with pytest.raises(KeyError):
            persistence.work.get("workspace", "stale")
        assert persistence.chat.persistence.list(ChatMessage, "workspace") == ()
    finally:
        persistence.timeline.commits.close()


def test_open_output_prefix_does_not_skip_interleaved_history(
    postgres: PostgresStore,
) -> None:
    client, persistence, _ = replica(postgres, Provider())
    try:
        chat = persistence.chat.create_chat(
            "workspace", title="Open output", owner_actor_id="operator"
        )
        common = {
            "invocationId": "open",
            "messageId": "message-open",
            "actorId": "model",
        }
        persistence.timeline.append(
            workspace_id="workspace",
            chat_id=chat.chat_id,
            thread_id="root",
            kind="output",
            item_id="output-start",
            payload={
                **common,
                "state": "started",
                "startedAt": chat.created_at.isoformat(),
            },
        )
        persistence.timeline.append(
            workspace_id="workspace",
            chat_id=chat.chat_id,
            thread_id="root",
            kind="output",
            item_id="output-first",
            payload={
                **common,
                "state": "delta",
                "channel": "text",
                "offset": 0,
                "text": "First ",
            },
        )
        middle = persistence.chat.send_message(
            "workspace", chat.chat_id, actor_id="operator", body="Interleaved evidence"
        )
        persistence.timeline.append(
            workspace_id="workspace",
            chat_id=chat.chat_id,
            thread_id="root",
            kind="output",
            item_id="output-next",
            payload={
                **common,
                "state": "delta",
                "channel": "text",
                "offset": 6,
                "text": "next",
            },
        )
        path = f"/v1/workspaces/workspace/chats/{chat.chat_id}/timeline?thread=root&limit=1"
        response = client.get(path, headers={"Authorization": "Bearer token"})
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["nextBefore"] == 4
        assert [item["seq"] for item in page["items"]] == [4]
        assert [item["seq"] for item in page["outputPrefixes"]] == [1, 2]
        earlier = client.get(
            path + "&before=4", headers={"Authorization": "Bearer token"}
        )
        assert earlier.json()["items"][0]["payload"]["message_id"] == middle.message_id
    finally:
        persistence.timeline.commits.close()


def test_workflow_metadata_and_revision_conflicts_do_not_require_transcripts(
    postgres: PostgresStore,
) -> None:
    client, persistence, _ = replica(postgres, Provider())
    try:
        chat = persistence.chat.create_chat(
            "workspace", title="Metadata", owner_actor_id="operator"
        )
        execution = persistence.chat.launch_workflow(
            "workspace",
            chat.chat_id,
            actor_id="operator",
            body="Start",
            workflow=WorkflowDefinition(
                workflow_id="test-flow",
                version="1.0.0",
                title="Test",
                phases=("proposal",),
            ),
        )
        identity = ExecutionIdentity(
            execution.execution_id,
            "workspace",
            chat.chat_id,
            "test-flow",
            "1.0",
            "operator",
        )
        initial = persistence.executions.create(
            identity, launch_request={"evidence": "x" * 300_000}
        )
        changed = persistence.executions.put_phase_result(
            execution.execution_id,
            "proposal",
            {"result": "retained"},
            expected_revision=initial.revision,
        )
        with pytest.raises(ExecutionStoreConflict):
            persistence.executions.put_phase_result(
                execution.execution_id,
                "proposal",
                {"result": "stale"},
                expected_revision=initial.revision,
            )
        assert persistence.executions.get(execution.execution_id) == changed
        assert tuple(persistence.executions.identities()) == (identity,)
        response = client.get(
            f"/v1/workspaces/workspace/chats/{chat.chat_id}/executions",
            headers={"Authorization": "Bearer token"},
        )
        assert response.status_code == 200, response.text
        assert [item["execution_id"] for item in response.json()["items"]] == [
            execution.execution_id
        ]
        assert response.json()["after"] == persistence.timeline.latest_sequence(
            workspace_id="workspace", chat_id=chat.chat_id
        )
        assert "retained" not in response.text and "evidence" not in response.text
    finally:
        persistence.timeline.commits.close()
