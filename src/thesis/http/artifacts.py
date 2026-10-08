"""Workspace artifact catalog over durable Git-backed workspace records.

The route projects strategies, studies, and indicators exactly as committed in
the workspace repository, plus a bounded UTF-8 reader behind the artifact file
viewer.  No volatile or in-memory store participates: the catalog exposes only
artifacts whose bytes are durable in Git.
"""

from __future__ import annotations

import hashlib
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from fastapi.concurrency import run_in_threadpool

from thesis.http.errors import require_service, service_error
from thesis.strategies.artifacts import WorkspaceStrategyArtifacts
from thesis.workspaces.local import WorkspaceNotFoundError
from thesis.workspaces.programs import ProgramKind, WorkspacePrograms

MAX_ARTIFACT_FILE_BYTES = 262_144
_ARTIFACT_KINDS: tuple[ProgramKind, ...] = ("strategy", "study", "indicator")
_KIND_ORDER = {"strategy": 0, "study": 1, "indicator": 2}


class ArtifactCatalogService:
    """Aggregate durable strategy, study, and indicator artifacts."""

    def __init__(
        self,
        strategies: WorkspaceStrategyArtifacts | None = None,
        programs: WorkspacePrograms | None = None,
    ) -> None:
        self.strategies = strategies
        self.programs = programs

    def catalog(self, workspace_id: str) -> dict[str, object]:
        """Return one workspace's committed artifacts and their file manifests."""
        try:
            strategy_items = (
                []
                if self.strategies is None
                else [dict(item) for item in self.strategies.catalog(workspace_id)]
            )
            program_items: dict[ProgramKind, list[dict[str, object]]] = {
                "strategy": [],
                "study": [],
                "indicator": [],
            }
            if self.programs is not None:
                for kind in _ARTIFACT_KINDS:
                    program_items[kind] = [
                        dict(item) for item in self.programs.catalog(workspace_id, kind)
                    ]
        except WorkspaceNotFoundError:
            raise KeyError(workspace_id) from None
        bundle_ids = {str(item["id"]) for item in strategy_items}
        # A bundle-manifest strategy and a program-layout strategy can share an
        # ID; the verified bundle projection wins and the program entry is
        # dropped rather than duplicated.
        strategy_items.extend(
            item for item in program_items["strategy"] if item["id"] not in bundle_ids
        )
        indicator_items = program_items["indicator"]
        items = [*strategy_items, *program_items["study"], *indicator_items]
        _link_indicator_usage(indicator_items, items)
        for item in items:
            item["id"] = f"{item['kind']}:{item['id']}"
        items.sort(
            key=lambda item: (
                _KIND_ORDER.get(str(item["kind"]), len(_KIND_ORDER)),
                str(item["title"]),
            )
        )
        return {
            "workspace_id": workspace_id,
            "items": items,
            "sources": {
                "strategies": _source_state(
                    self.strategies is not None or self.programs is not None,
                    strategy_items,
                ),
                "studies": _source_state(
                    self.programs is not None, program_items["study"]
                ),
                "indicators": _source_state(self.programs is not None, indicator_items),
            },
        }

    def file(
        self, workspace_id: str, artifact_id: str, file_path: str
    ) -> dict[str, object]:
        """Read one bounded UTF-8 file from a committed artifact."""
        kind, separator, name = artifact_id.partition(":")
        if not separator or kind not in _ARTIFACT_KINDS or not name:
            raise KeyError(artifact_id)
        try:
            payload: bytes | None = None
            if kind == "strategy" and self.strategies is not None:
                try:
                    payload = self.strategies.read_file(workspace_id, name, file_path)
                except KeyError:
                    payload = None
            if payload is None:
                if self.programs is None:
                    raise KeyError(name)
                payload = self.programs.file_content(
                    workspace_id, kind, name, file_path
                )
        except WorkspaceNotFoundError:
            raise KeyError(workspace_id) from None
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("artifact file is not UTF-8 text") from error
        truncated = len(payload) > MAX_ARTIFACT_FILE_BYTES
        return {
            "path": file_path,
            "text": text[:MAX_ARTIFACT_FILE_BYTES],
            "truncated": truncated,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        }


def _link_indicator_usage(
    indicator_items: list[dict[str, object]], items: list[dict[str, object]]
) -> None:
    users: dict[str, list[dict[str, object]]] = {}
    for item in items:
        preview = item.get("preview")
        if not isinstance(preview, dict):
            continue
        dependencies = preview.get("indicator_ids")
        if not isinstance(dependencies, list):
            continue
        reference = {
            "id": f"{item.get('kind')}:{item.get('id')}",
            "kind": item.get("kind"),
            "title": item.get("title"),
        }
        for dependency in dependencies:
            if isinstance(dependency, str):
                users.setdefault(dependency, []).append(reference)
    for indicator in indicator_items:
        preview = indicator.get("preview")
        if isinstance(preview, dict):
            preview["used_by"] = users.get(str(indicator.get("id")), [])


def _source_state(available: bool, items: list[dict[str, object]]) -> str:
    if not available:
        return "unavailable"
    return "ok" if items else "empty"


def get_artifact_catalog_service(request: Request) -> ArtifactCatalogService:
    """Resolve the catalog from app state so applications control composition."""
    return require_service(request, "artifact_catalog_service", ArtifactCatalogService)


def create_artifact_router() -> APIRouter:
    """Create the workspace-artifacts router; services resolve from app state."""
    router = APIRouter(
        prefix="/v1/workspaces/{workspace_id}", tags=["workspace-artifacts"]
    )

    @router.get("/artifacts")
    async def list_workspace_artifacts(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        service: Annotated[
            ArtifactCatalogService, Depends(get_artifact_catalog_service)
        ],
    ) -> dict[str, object]:
        try:
            return await run_in_threadpool(service.catalog, workspace_id)
        except KeyError as error:
            raise service_error(error, resource="workspace") from error
        except ValueError as error:
            raise service_error(error) from error

    @router.get("/artifacts/{artifact_id}/files/{file_path:path}")
    async def read_workspace_artifact_file(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        artifact_id: Annotated[str, Path(min_length=1, max_length=256)],
        file_path: Annotated[str, Path(min_length=1, max_length=512)],
        service: Annotated[
            ArtifactCatalogService, Depends(get_artifact_catalog_service)
        ],
    ) -> dict[str, object]:
        try:
            return await run_in_threadpool(
                service.file, workspace_id, artifact_id, file_path
            )
        except KeyError as error:
            raise service_error(error, resource="artifact") from error
        except ValueError as error:
            raise service_error(error) from error

    return router


__all__ = [
    "ArtifactCatalogService",
    "MAX_ARTIFACT_FILE_BYTES",
    "create_artifact_router",
    "get_artifact_catalog_service",
]
