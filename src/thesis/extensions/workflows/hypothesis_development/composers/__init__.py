"""JSON compose helpers that turn contract payloads into typed drafts.

The package re-exports the shared extraction helper so callers can keep
importing from ``composers``.
"""

from __future__ import annotations

from ._shared import _json_document

__all__ = [
    "_json_document",
]
