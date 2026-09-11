"""Workspace catalog, snapshot overview, and the local data composition.

The workspace routes serve the catalog and the overview snapshot the UI uses
to hydrate a workspace page.  This module also owns
:func:`build_local_workspace_data`, the composition helper that builds the
local services (local persistence plus a :class:`LocalHarnessProcess` adapter
with an injected runner) for ``thesis serve`` and for tests.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from thesis.artifacts.importer import ArtifactImporter
from thesis.chat import ChatService, ChatState
from thesis.http.backtests import BacktestDataService, BacktestStore
from thesis.http.dashboards import (
    DashboardDataService,
    InMemoryWidgetStore,
    WidgetStore,
)
from thesis.http.deployments import (
    DeploymentDataService,
)
from thesis.http.deployments import (
    _UnavailableTransport as _UnavailableStatusTransport,
)
from thesis.http.screeners import (
    InMemoryScreenerStore,
    ScreenerDataService,
    ScreenerMarketPort,
)
from thesis.http.state import (
    InMemoryWorkspaceStateStore,
    WorkspaceStateService,
    WorkspaceStateStore,
)
from thesis.http.strategies import StrategyDataService
from thesis.integrations.harness_activation import (
    HarnessApiTransport,
    HarnessBoundary,
    HarnessProcessRunner,
    LocalHarnessProcess,
)
from thesis.integrations.runtime_status import RuntimeStatusClient
from thesis.screening.engine import MarketBar, ScreenerPortError, UniverseEntry


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if len(slug) < 3:
        slug = f"{slug}-ws" if slug else "workspace"
    return slug[:63].strip("-") or "workspace"


def _initials(name: str) -> str:
    words = [word for word in name.split() if word]
    if not words:
        return "WS"
    if len(words) == 1:
        return words[0][:2].upper()
    return "".join(word[0] for word in words[:2]).upper()


def _counting_id_factory() -> Callable[[str], str]:
    counters: dict[str, int] = {}

    def factory(prefix: str) -> str:
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}-{counters[prefix]:03d}"

    return factory


class WorkspaceStore(Protocol):
    """Persistence boundary for workspace summaries and activity items.

    The in-memory :class:`WorkspaceCatalogStore` and the filesystem-backed
    :class:`thesis.workspaces.catalog.LocalWorkspaceCatalogStore` both satisfy
    this port.
    """

    def list(self) -> tuple[dict[str, object], ...]: ...

    def get(self, workspace_id: str) -> dict[str, object] | None: ...

    def add(self, summary: dict[str, object]) -> dict[str, object]: ...

    def add_activity(self, item: dict[str, object]) -> dict[str, object]: ...

    def activity(self, workspace_id: str) -> tuple[dict[str, object], ...]: ...


class WorkspaceCatalogStore:
    """Local persistence for workspace summaries and activity items."""

    def __init__(self) -> None:
        self._workspaces: dict[str, dict[str, object]] = {}
        self._activity: dict[str, list[dict[str, object]]] = {}

    def list(self) -> tuple[dict[str, object], ...]:
        return tuple(dict(row) for row in self._workspaces.values())

    def get(self, workspace_id: str) -> dict[str, object] | None:
        row = self._workspaces.get(workspace_id)
        return dict(row) if row is not None else None

    def add(self, summary: dict[str, object]) -> dict[str, object]:
        workspace_id = str(summary["id"])
        self._workspaces[workspace_id] = dict(summary)
        return dict(summary)

    def add_activity(self, item: dict[str, object]) -> dict[str, object]:
        workspace_id = str(item["workspace_id"])
        self._activity.setdefault(workspace_id, []).insert(0, dict(item))
        return dict(item)

    def activity(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        return tuple(dict(item) for item in self._activity.get(workspace_id, ()))


class CreateWorkspaceRequest(BaseModel):
    """Request boundary for workspace creation."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=1_024)
    initials: str | None = Field(default=None, min_length=1, max_length=4)


_COUNT_KEYS = (
    "strategies",
    "chats",
    "backtests",
    "deployments",
    "dashboards",
    "screeners",
)


class WorkspaceDataService:
    """Catalog and overview reads over the local workspace records."""

    def __init__(
        self,
        store: WorkspaceStore | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[str], str] | None = None,
        count_providers: Mapping[str, Callable[[str], int]] | None = None,
        open_chats_provider: Callable[[str], list[dict[str, object]]] | None = None,
        health_provider: Callable[[str], str] | None = None,
    ) -> None:
        self._store = store if store is not None else WorkspaceCatalogStore()
        self._clock = clock if clock is not None else _utc_now
        self._id_factory = id_factory
        self._count_providers = count_providers or {}
        self._open_chats_provider = open_chats_provider
        self._health_provider = health_provider

    @property
    def store(self) -> WorkspaceStore:
        return self._store

    def list_workspaces(self) -> tuple[dict[str, object], ...]:
        return self._store.list()

    def create_workspace(
        self, name: str, description: str = "", initials: str | None = None
    ) -> dict[str, object]:
        workspace_id = self._unique_id(_slugify(name))
        summary: dict[str, object] = {
            "id": workspace_id,
            "name": name,
            "description": description,
            "initials": initials if initials is not None else _initials(name),
            "created_at": _iso_z(self._clock()),
        }
        return self._store.add(summary)

    def record_activity(
        self,
        workspace_id: str,
        *,
        title: str,
        description: str,
        activity_type: str,
        activity_id: str | None = None,
    ) -> dict[str, object]:
        item: dict[str, object] = {
            "id": activity_id or f"act-{len(self._store.activity(workspace_id)) + 1}",
            "workspace_id": workspace_id,
            "title": title,
            "description": description,
            "type": activity_type,
            "created_at": _iso_z(self._clock()),
        }
        return self._store.add_activity(item)

    def _unique_id(self, base: str) -> str:
        existing = {str(row.get("id")) for row in self._store.list()}
        if base not in existing:
            return base
        suffix = 2
        while f"{base}-{suffix}" in existing:
            suffix += 1
        return f"{base}-{suffix}"

    def snapshot(self, workspace_id: str) -> dict[str, object]:
        """Return the overview payload: workspace meta, activity, counts."""
        workspace = self._store.get(workspace_id)
        if workspace is None:
            raise KeyError(workspace_id)
        open_chats = (
            list(self._open_chats_provider(workspace_id))
            if self._open_chats_provider is not None
            else []
        )
        overview: dict[str, object] = {
            "workspace": workspace,
            "health": (
                self._health_provider(workspace_id)
                if self._health_provider is not None
                else "operational"
            ),
            "activity": list(self._store.activity(workspace_id)),
            "openChats": open_chats,
        }
        counts: dict[str, int] = {key: 0 for key in _COUNT_KEYS}
        counts["chats"] = len(open_chats)
        for key in _COUNT_KEYS:
            provider = self._count_providers.get(key)
            if provider is not None:
                counts[key] = provider(workspace_id)
        return {"overview": overview, "counts": counts}


def get_workspace_service(request: Request) -> WorkspaceDataService:
    """Resolve the service from app state so applications control persistence."""
    service = getattr(request.app.state, "workspace_service", None)
    if service is None:
        service = WorkspaceDataService()
        request.app.state.workspace_service = service
    if not isinstance(service, WorkspaceDataService):
        raise RuntimeError("app.state.workspace_service must be a WorkspaceDataService")
    return service


def create_workspaces_router() -> APIRouter:
    """Create the workspace catalog and snapshot router."""
    router = APIRouter(prefix="/v1", tags=["workspaces"])

    @router.get("/workspaces")
    async def list_workspaces(
        service: Annotated[WorkspaceDataService, Depends(get_workspace_service)],
    ) -> dict[str, object]:
        return {"items": list(service.list_workspaces())}

    @router.post("/workspaces", status_code=201)
    async def create_workspace(
        request: CreateWorkspaceRequest,
        service: Annotated[WorkspaceDataService, Depends(get_workspace_service)],
    ) -> dict[str, object]:
        return service.create_workspace(
            request.name, description=request.description, initials=request.initials
        )

    @router.get("/workspaces/{workspace_id}/snapshot")
    async def get_snapshot(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        service: Annotated[WorkspaceDataService, Depends(get_workspace_service)],
    ) -> dict[str, object]:
        try:
            return service.snapshot(workspace_id)
        except KeyError as error:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "workspace_not_found",
                    "message": f"workspace {workspace_id!r} does not exist",
                },
            ) from error

    return router


class _JsonFile:
    """One atomic JSON document under the local persistence root."""

    def __init__(self, path: pathlib.Path) -> None:
        self._path = path

    def read(self) -> dict[str, object] | None:
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                document = json.load(handle)
        except (FileNotFoundError, NotADirectoryError):
            return None
        return document if isinstance(document, dict) else None

    def write(self, document: Mapping[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(dict(document), handle)
        os.replace(temporary, self._path)


class _LocalWidgetStore:
    """Widget definitions persisted as one JSON document per workspace."""

    def __init__(self, path: pathlib.Path) -> None:
        self._file = _JsonFile(path)
        document = self._file.read()
        self._widgets: dict[str, list[dict[str, object]]] = {}
        raw = (document or {}).get("widgets", {})
        if isinstance(raw, dict):
            self._widgets = {
                str(key): [dict(item) for item in value]
                for key, value in raw.items()
                if isinstance(value, list)
            }

    def list(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        return tuple(dict(item) for item in self._widgets.get(workspace_id, ()))

    def add(self, workspace_id: str, widget: dict[str, object]) -> dict[str, object]:
        self._widgets.setdefault(workspace_id, []).append(dict(widget))
        self._flush()
        return dict(widget)

    def delete(self, workspace_id: str, widget_id: str) -> None:
        widgets = self._widgets.get(workspace_id, [])
        for index, widget in enumerate(widgets):
            if widget.get("id") == widget_id:
                del widgets[index]
                self._flush()
                return
        raise KeyError(widget_id)

    def _flush(self) -> None:
        self._file.write({"widgets": self._widgets})


class _LocalStateStore:
    """Workspace-state documents persisted under the local root."""

    def __init__(self, directory: pathlib.Path) -> None:
        self._directory = directory

    def load(self, workspace_id: str) -> dict[str, object] | None:
        return _JsonFile(self._directory / f"{workspace_id}.json").read()

    def save(self, workspace_id: str, document: dict[str, object]) -> None:
        _JsonFile(self._directory / f"{workspace_id}.json").write(document)


class _UnavailableMarketPort:
    """Fail-closed market port used when none is configured."""

    def universe(self, as_of: object) -> tuple[UniverseEntry, ...]:
        raise ScreenerPortError("no screener market port is configured")

    def market_data(self, symbol: str, window_end: object) -> tuple[MarketBar, ...]:
        raise ScreenerPortError("no screener market port is configured")

    def change_pct(self, symbol: str, as_of: object) -> float:
        raise ScreenerPortError("no screener market port is configured")


@dataclass(frozen=True, slots=True)
class WorkspaceDataBundle:
    """The composed local services injected into the application state."""

    workspaces: WorkspaceDataService
    backtests: BacktestDataService
    deployments: DeploymentDataService
    dashboards: DashboardDataService
    screeners: ScreenerDataService
    state: WorkspaceStateService
    strategies: StrategyDataService
    harness: HarnessBoundary | None = None


def _workspace_template_path(
    override: str | pathlib.Path | None,
) -> pathlib.Path:
    if override is not None:
        return pathlib.Path(override)
    repo_root = pathlib.Path(__file__).resolve().parents[3]
    return repo_root / "config" / "thesis" / "workspace.yaml"


def _local_catalog_store(
    workspace_root: pathlib.Path,
    template: str | pathlib.Path | None,
    clock: Callable[[], datetime],
) -> WorkspaceStore:
    """Build the filesystem-backed workspace catalog (lazy heavy imports)."""
    from thesis.workspaces.catalog import LocalWorkspaceCatalogStore
    from thesis.workspaces.local import LocalWorkspaceProvider
    from thesis.workspaces.manifest import load_workspace_manifest

    manifest = load_workspace_manifest(_workspace_template_path(template))
    provider = LocalWorkspaceProvider(workspace_root)
    return LocalWorkspaceCatalogStore(provider, manifest, clock=clock)


def build_local_workspace_data(
    *,
    workspace_root: str | pathlib.Path | None = None,
    workspace_template: str | pathlib.Path | None = None,
    harness_runner: HarnessProcessRunner | None = None,
    harness_transport: HarnessApiTransport | None = None,
    harness_base_url: str = "http://127.0.0.1:8777",
    harness_boundary: HarnessBoundary | None = None,
    chat_service: ChatService | None = None,
    market_port: ScreenerMarketPort | None = None,
    backtest_store: BacktestStore | None = None,
    artifact_importer: ArtifactImporter | None = None,
    clock: Callable[[], datetime] | None = None,
    id_factory: Callable[[str], str] | None = None,
) -> WorkspaceDataBundle:
    """Compose the local-mode services behind the workspace DATA routes.

    ``harness_runner`` builds a :class:`LocalHarnessProcess` with an injected
    process runner; without one the backtest launch surface fails closed.
    ``harness_transport`` builds the status client behind the portfolio and
    risk-decision seams; without one those surfaces fail closed with 503.
    ``backtest_store`` and ``artifact_importer`` inject the backtest run
    persistence boundary and the harness artifact importer; without them the
    backtest service keeps its own in-memory store and imports no artifacts.
    Widget definitions and workspace-state documents persist under
    ``workspace_root`` when one is supplied, else in memory.  When
    ``workspace_root`` is supplied, workspaces themselves persist as literal
    Git-backed directories under it; without one they live in memory.
    """
    resolved_clock = clock if clock is not None else _utc_now
    ids = id_factory if id_factory is not None else _counting_id_factory()
    harness = (
        harness_boundary
        if harness_boundary is not None
        else LocalHarnessProcess(harness_runner)
        if harness_runner is not None
        else None
    )
    status_client = RuntimeStatusClient(
        harness_base_url,
        harness_transport
        if harness_transport is not None
        else _UnavailableStatusTransport(),
    )

    widget_store: WidgetStore = (
        _LocalWidgetStore(pathlib.Path(workspace_root) / "dashboards.json")
        if workspace_root is not None
        else InMemoryWidgetStore()
    )
    state_store: WorkspaceStateStore = (
        _LocalStateStore(pathlib.Path(workspace_root) / "workspace-state")
        if workspace_root is not None
        else InMemoryWorkspaceStateStore()
    )
    screener_store = InMemoryScreenerStore()
    strategies = StrategyDataService(clock=resolved_clock, id_factory=ids)

    backtests = BacktestDataService(
        backtest_store,
        harness=harness,
        importer=artifact_importer,
        clock=resolved_clock,
        id_factory=ids,
    )
    deployments = DeploymentDataService(status_client=status_client)
    dashboards = DashboardDataService(
        widget_store, clock=resolved_clock, id_factory=ids
    )
    screeners = ScreenerDataService(
        store=screener_store,
        market=market_port if market_port is not None else _UnavailableMarketPort(),
        clock=resolved_clock,
        id_factory=ids,
    )
    chat = chat_service if chat_service is not None else ChatService()

    def open_chats(workspace_id: str) -> list[dict[str, object]]:
        return [
            thread.as_dict()
            for thread in chat.list_chats(workspace_id)
            if thread.state == ChatState.ACTIVE
        ]

    workspace_store: WorkspaceStore = WorkspaceCatalogStore()
    if workspace_root is not None:
        workspace_store = _local_catalog_store(
            pathlib.Path(workspace_root), workspace_template, resolved_clock
        )

    workspaces = WorkspaceDataService(
        store=workspace_store,
        clock=resolved_clock,
        id_factory=ids,
        count_providers={
            "backtests": lambda workspace_id: len(backtests.store.list(workspace_id)),
            "deployments": lambda workspace_id: len(
                deployments.store.list_deployments(workspace_id)
            ),
            "dashboards": lambda workspace_id: len(widget_store.list(workspace_id)),
            "screeners": lambda workspace_id: len(screener_store.list(workspace_id)),
            "chats": lambda workspace_id: len(open_chats(workspace_id)),
            "strategies": lambda workspace_id: len(strategies.store.list(workspace_id)),
        },
        open_chats_provider=open_chats,
    )
    return WorkspaceDataBundle(
        workspaces=workspaces,
        backtests=backtests,
        deployments=deployments,
        dashboards=dashboards,
        screeners=screeners,
        state=WorkspaceStateService(state_store),
        strategies=strategies,
        harness=harness,
    )


__all__ = [
    "CreateWorkspaceRequest",
    "WorkspaceCatalogStore",
    "WorkspaceDataBundle",
    "WorkspaceDataService",
    "build_local_workspace_data",
    "create_workspaces_router",
    "get_workspace_service",
]
