"""The workflow-launch handle carries a real execution state.

The card's status must never derive from the synthetic ``requested`` phase
marker: the handle reports ``running`` until the engine advances it.
"""

from types import SimpleNamespace
from typing import cast

from thesis.chat.responder import DevelopmentRuntime
from thesis.chat.service import ChatService
from thesis.chat.workflow_launcher import RuntimeWorkflowLauncher
from thesis.workflows.core.spec import WorkflowContext


class _Workflow:
    def launch(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        return SimpleNamespace(execution_id="execution-1")

    def prepare_phase(self, request: object) -> None:
        del request


class _Runtime:
    def build_hypothesis_development(self, *, chat: object) -> _Workflow:
        del chat
        return _Workflow()


def test_launch_handle_reports_running_and_keeps_the_phase_marker() -> None:
    launcher = RuntimeWorkflowLauncher(
        cast(DevelopmentRuntime, _Runtime()), ChatService(), detach=lambda _: None
    )
    handle = launcher.execute(
        "hypothesis-development",
        WorkflowContext(
            workspace_id="workspace",
            chat_id="chat-1",
            actor_id="operator",
            trigger_message_id="message-1",
        ),
        ideas="SPY momentum persists.",
    )
    assert handle["executionId"] == "execution-1"
    assert handle["state"] == "running"
