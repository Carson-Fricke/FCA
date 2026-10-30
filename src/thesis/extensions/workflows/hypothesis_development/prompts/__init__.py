"""Prompt builders for the hypothesis-development phases.

The package re-exports the prompt surface so callers can keep importing from
``prompts``.
"""

from __future__ import annotations

from .discovery import _discovery_restart_section, _discovery_turn_prompt
from .model_creation import (
    _model_creation_instructions,
    _model_creation_revision_section,
)
from .verdict import _verdict_instructions

__all__ = [
    "_discovery_restart_section",
    "_discovery_turn_prompt",
    "_model_creation_instructions",
    "_model_creation_revision_section",
    "_verdict_instructions",
]
