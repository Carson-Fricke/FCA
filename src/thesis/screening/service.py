"""Workspace screening service over the T219 engine.

The service is the seam between the HTTP screener router and the screening
engine: it resolves the screener context (universe + market data ports) for
one workspace, executes declarative definitions, and persists saved
definitions and results as workspace artifacts.  All wall-clock values come
from the injected clock; data access goes through the injected ports —
no network, no lake or harness internals in this module.

The service is workspace-scoped by construction: every method takes the
workspace id from the caller and keys its stores by it, mirroring the
residency binding rules.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import Any, Final, Protocol

from thesis.screening.artifacts import SavedStudy
from thesis.screening.engine import (
    ScreenerContext,
    ScreenerDefinition,
    ScreenerError,
    run_screener,
)


class ScreenerServiceError(ScreenerError):
    """Base class for screener service failures."""


class UnknownScreenerError(ScreenerServiceError, LookupError):
    """A saved definition or result id does not exist for the workspace."""


class ScreenerDataUnavailableError(ScreenerServiceError):
    """The screening data ports are not configured for this workspace."""


class ScreenerStore(Protocol):
    """Persistence boundary for screener documents, keyed by workspace."""

    def put(
        self,
        workspace_id: str,
        key: str,
        *,
        document: Mapping[str, Any],
    ) -> None: ...

    def get(self, workspace_id: str, key: str) -> Mapping[str, Any]: ...

    def list_prefix(
        self, workspace_id: str, prefix: str
    ) -> Sequence[Mapping[str, Any]]: ...

    def head_revision(self, workspace_id: str) -> str: ...


class ScreenerService:
    """Workspace-scoped facade over the screening engine and artifacts."""

    def __init__(
        self,
        *,
        data_context: Callable[[str, date], ScreenerContext] | None,
        store: ScreenerStore,
        clock: Callable[[], str],
    ) -> None:
        self._data_context = data_context
        self._store = store
        self._clock = clock

    def run(
        self,
        workspace_id: str,
        payload: Mapping[str, Any],
        *,
        as_of: date,
    ) -> Mapping[str, Any]:
        """Execute one screening run and persist the result as a study."""
        if self._data_context is None:
            raise ScreenerDataUnavailableError(
                "screening data ports are not configured for this workspace"
            )
        definition = _definition_from_payload(payload)
        context = self._data_context(workspace_id, as_of)
        result = run_screener(definition, context, as_of=as_of)
        study = SavedStudy(
            name=str(payload.get("name") or definition.name),
            definition=definition,
            result=result,
            git_revision=self._store.head_revision(workspace_id),
            saved_at=as_of,
        )
        result_id = f"scr-{study.content_sha256[:16]}"
        document: dict[str, Any] = {
            "workspace_id": workspace_id,
            "result_id": result_id,
            **study.canonical_document,
        }
        self._store.put(
            workspace_id,
            f"results/{result_id}.json",
            document=document,
        )
        return document

    def save_definition(
        self, workspace_id: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Persist a screener definition and return its stored document."""
        definition_id = _definition_id(payload)
        document: dict[str, Any] = {
            "workspace_id": workspace_id,
            "definition_id": definition_id,
            **dict(payload),
        }
        self._store.put(
            workspace_id,
            f"definitions/{definition_id}.json",
            document=document,
        )
        return document

    def list_definitions(self, workspace_id: str) -> Sequence[Mapping[str, Any]]:
        """List the workspace's saved screener definitions."""
        return self._store.list_prefix(workspace_id, "definitions/")

    def get_result(self, workspace_id: str, result_id: str) -> Mapping[str, Any]:
        """Return one stored screening result."""
        return self._store.get(workspace_id, f"results/{result_id}.json")

    def get_definition(self, workspace_id: str, screener_id: str) -> Mapping[str, Any]:
        """Return one saved screener definition."""
        return self._store.get(workspace_id, f"definitions/{screener_id}.json")


def _definition_from_payload(payload: Mapping[str, Any]) -> ScreenerDefinition:
    """Build a validated engine definition from the HTTP payload."""
    return ScreenerDefinition(
        name=str(payload["name"]),
        universe_revision=str(
            payload.get("universe_revision") or f"asof-{payload['as_of']}"
        ),
        filters=tuple(payload["filters"]),
        lookback_days=int(payload.get("lookback_days", 20)),
    )


def _definition_id(payload: Mapping[str, Any]) -> str:
    """Derive the stored definition id from the payload name."""
    name = str(payload.get("name", "screener"))
    slug = "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-")
    return slug or "screener"


_REVISION_UNSET: Final = "uncommitted"


def _fallback_head_revision(workspace_id: str) -> str:  # pragma: no cover
    return f"{_REVISION_UNSET}:{workspace_id}"
