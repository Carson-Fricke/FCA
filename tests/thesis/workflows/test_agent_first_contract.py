"""The agent-first inversion's minimal contract surface.

Agents own their turns: the engine never turns one. These tests pin the two
halves of that contract that the deleted engine tests used to cover — the
engine-turn symbols are gone, and the general workflow object exposes the
agent-driven entry points and the phase-completion repair channel.
"""

from __future__ import annotations

import importlib

import pytest

from thesis.agent.runtime.control import (
    CONTROL_TOOL_NAMES,
    CONTROL_TOOL_SPECS,
    TOOL_REQUEST_OPERATOR_INPUT,
    TOOL_REQUEST_WORKFLOW_INPUT,
    PhaseCompletionRejected,
)
from thesis.agent.runtime.control_tools import CONTROL_CHAT_TOOL_NAMES
from thesis.workflows.core.workflow import Workflow
from thesis.workflows.runtime import WorkflowRuntime


def test_engine_turn_symbols_are_removed() -> None:
    with pytest.raises(ImportError):
        importlib.import_module("thesis.workflows.core.runner")
    assert not hasattr(Workflow, "advance_turn")
    assert not hasattr(Workflow, "run")
    assert not hasattr(WorkflowRuntime, "execute_hypothesis_development")


def test_general_object_exposes_prepare_and_completion() -> None:
    assert callable(getattr(Workflow, "prepare_phase", None))
    assert callable(getattr(Workflow, "apply_phase_completion", None))


def test_wrapped_input_tool_is_declared_for_workflow_nodes_only() -> None:
    declared = {spec.name for spec in CONTROL_TOOL_SPECS}
    assert TOOL_REQUEST_WORKFLOW_INPUT in CONTROL_TOOL_NAMES
    assert TOOL_REQUEST_WORKFLOW_INPUT in declared
    assert TOOL_REQUEST_OPERATOR_INPUT in declared
    assert TOOL_REQUEST_WORKFLOW_INPUT not in CONTROL_CHAT_TOOL_NAMES
    assert TOOL_REQUEST_OPERATOR_INPUT in CONTROL_CHAT_TOOL_NAMES


def test_phase_completion_rejection_is_a_repair_signal() -> None:
    assert issubclass(PhaseCompletionRejected, RuntimeError)
