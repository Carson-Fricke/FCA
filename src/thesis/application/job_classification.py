"""Identify execution records owned by the FCA harness."""

from thesis.integrations.mcp import (
    TOOL_LAUNCH_TRADER,
    TOOL_RUN_BACKTEST,
    TOOL_RUN_SCREENER,
    TOOL_RUN_STUDY,
)

#: Thesis operation name for the harness-backed falsification research model.
HARNESS_RESEARCH_MODEL_TOOL = "harness.research_model"

#: Operations whose execution state and results are owned by one harness job
#: record.  Every other tracking operation is non-harness: tracking owns its
#: attribution and outcome observation, but no harness report or status surface
#: may present it as a harness execution.  The names reuse the published
#: harness operation constants rather than duplicating string literals.
HARNESS_EXECUTION_TOOL_NAMES: frozenset[str] = frozenset(
    {
        TOOL_LAUNCH_TRADER,
        TOOL_RUN_BACKTEST,
        TOOL_RUN_SCREENER,
        TOOL_RUN_STUDY,
        HARNESS_RESEARCH_MODEL_TOOL,
    }
)


def is_harness_execution_tool(tool_name: str) -> bool:
    """Return whether one operation submits a harness-owned execution."""
    return tool_name in HARNESS_EXECUTION_TOOL_NAMES
