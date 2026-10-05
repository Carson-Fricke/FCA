"""Deterministic fixtures for the Thesis HTTP surface tests."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from thesis.chat import ChatService, WorkflowDefinition, WorkflowExecution
from thesis.http.application import create_app

NOW = datetime(2026, 1, 1, tzinfo=UTC)
WORKFLOW = WorkflowDefinition(
    workflow_id="strategy-development",
    version="1.0.0",
    title="Strategy development",
    phases=("understand", "implement", "evaluate"),
)
WORKSPACE = "workspace-a"
BEARER_TOKEN = "thesis-test-token"


async def _no_sleeper(seconds: float) -> None:
    return None


def _counting_id_factory() -> Callable[[str], str]:
    counters: dict[str, int] = {}

    def factory(prefix: str) -> str:
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}-{counters[prefix]:03d}"

    return factory


@pytest.fixture()
def service() -> ChatService:
    return ChatService(clock=lambda: NOW, id_factory=_counting_id_factory())


@pytest.fixture()
def launch(service: ChatService) -> Callable[[str], WorkflowExecution]:
    def factory(chat_id: str, workspace: str = WORKSPACE) -> WorkflowExecution:
        return service.launch_workflow(
            workspace,
            chat_id,
            workflow=WORKFLOW,
            actor_id="operator",
            body="Build and test the strategy.",
        )

    return factory


@pytest.fixture()
def execution(
    service: ChatService, launch: Callable[[str], WorkflowExecution]
) -> WorkflowExecution:
    chat = service.create_chat(WORKSPACE, title="Research", owner_actor_id="operator")
    return launch(chat.chat_id)


@pytest.fixture()
def make_client() -> Callable[..., TestClient]:
    def factory(
        chat_service: ChatService,
        *,
        stream_max_poll_cycles: int | None = 1,
    ) -> TestClient:
        app = create_app(
            bearer_token=BEARER_TOKEN,
            chat_service=chat_service,
            stream_sleeper=_no_sleeper,
            stream_max_poll_cycles=stream_max_poll_cycles,
        )
        return TestClient(app)

    return factory


@pytest.fixture()
def client(service: ChatService, make_client: Callable[..., TestClient]) -> TestClient:
    return make_client(service)
