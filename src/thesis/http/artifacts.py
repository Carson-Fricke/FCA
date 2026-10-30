"""Workspace artifact catalog over durable Git-backed workspace records.

The route projects strategies, studies, and indicators exactly as committed in
the workspace repository, plus a bounded UTF-8 reader behind the artifact file
viewer.  No volatile or in-memory store participates: the catalog exposes only
artifacts whose bytes are durable in Git.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Path, Request
from fastapi.concurrency import run_in_threadpool

from thesis.http.errors import require_service, service_error
from thesis.strategies.artifacts import WorkspaceStrategyArtifacts
from thesis.workspaces.local import WorkspaceNotFoundError
from thesis.workspaces.program_contracts import ProgramKind
from thesis.workspaces.programs import WorkspacePrograms

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

    def strategy_items(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        """Share one projection of published bundles and authored strategy programs."""
        bundles = (
            []
            if self.strategies is None
            else list(self.strategies.catalog(workspace_id))
        )
        programs = (
            []
            if self.programs is None
            else list(self.programs.catalog(workspace_id, "strategy"))
        )
        bundle_ids = {str(item["id"]) for item in bundles}
        return tuple(
            deepcopy(item)
            for item in [
                *bundles,
                *(item for item in programs if item["id"] not in bundle_ids),
            ]
        )

    def catalog(self, workspace_id: str) -> dict[str, object]:
        """Return one workspace's committed artifacts and their file manifests."""
        try:
            strategy_items = list(self.strategy_items(workspace_id))
            program_items: dict[ProgramKind, list[dict[str, object]]] = {
                "strategy": [],
                "study": [],
                "indicator": [],
            }
            if self.programs is not None:
                for kind in ("study", "indicator"):
                    program_items[kind] = [
                        deepcopy(item)
                        for item in self.programs.catalog(workspace_id, kind)
                    ]
        except WorkspaceNotFoundError:
            raise KeyError(workspace_id) from None
        indicator_items = program_items["indicator"]
        items = [*strategy_items, *program_items["study"], *indicator_items]
        if self.programs is not None:
            sketches = self.programs.sketches
            offset = 0
            while True:
                page = sketches.list(workspace_id, offset=offset, limit=200)
                for record in cast(list[dict[str, dict[str, object]]], page["items"]):
                    reference = record["reference"]
                    sketch = record["sketch"]
                    payload = (
                        json.dumps(
                            sketch,
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=False,
                        )
                        + "\n"
                    ).encode()
                    version = cast(str, reference["version"])
                    items.append(
                        {
                            "id": reference["sketchId"],
                            "kind": "sketch",
                            "title": sketch["title"],
                            "subtitle": "Evolving strategy sketch",
                            "description": sketch["premise"],
                            "status": "committed",
                            "version": version,
                            "artifact_path": f"sketches/{reference['sketchId']}",
                            "created_at": None,
                            "updated_at": None,
                            "git_commit": reference["gitCommit"],
                            "digests": {
                                "source_sha256": None,
                                "bundle_sha256": version,
                                "manifest_sha256": version,
                            },
                            "inputs": [],
                            "files": [
                                {
                                    "path": f"revisions/{version}.json",
                                    "sha256": version,
                                    "size_bytes": len(payload),
                                    "viewer": "json",
                                }
                            ],
                            "file_count": 1,
                            "size_bytes": len(payload),
                            "history": sketches.history(
                                workspace_id, cast(str, reference["sketchId"])
                            ),
                            "preview": {
                                "type": "sketch",
                                "reference": reference,
                                "sketch": sketch,
                            },
                        }
                    )
                next_offset = page["nextOffset"]
                if next_offset is None:
                    break
                offset = cast(int, next_offset)
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
        if separator and kind == "sketch" and self.programs is not None:
            match = re.fullmatch(r"revisions/([0-9a-f]{64})\.json", file_path)
            if match is None:
                raise KeyError(file_path)
            result = self.programs.sketches.read(workspace_id, name, version=match[1])
            text = (
                json.dumps(
                    result["sketch"],
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            )
            payload = text.encode()
            return {
                "path": file_path,
                "text": text[:MAX_ARTIFACT_FILE_BYTES],
                "truncated": len(payload) > MAX_ARTIFACT_FILE_BYTES,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
        if not separator or kind not in _ARTIFACT_KINDS or not name:
            raise KeyError(artifact_id)
        try:
            is_bundle = (
                kind == "strategy"
                and self.strategies is not None
                and any(
                    item["id"] == name for item in self.strategies.catalog(workspace_id)
                )
            )
            if is_bundle:
                assert self.strategies is not None
                payload = self.strategies.read_file(workspace_id, name, file_path)
            else:
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

    @router.get("/sketches/{sketch_id}")
    async def read_strategy_sketch(
        workspace_id: str,
        sketch_id: str,
        service: Annotated[
            ArtifactCatalogService, Depends(get_artifact_catalog_service)
        ],
        git_commit: str | None = None,
        version: str | None = None,
    ) -> dict[str, object]:
        if service.programs is None:
            raise service_error(KeyError(sketch_id), resource="sketch")
        try:
            return await run_in_threadpool(
                service.programs.sketches.read,
                workspace_id,
                sketch_id,
                git_commit=git_commit,
                version=version,
            )
        except (KeyError, ValueError) as error:
            raise service_error(error, resource="sketch") from error

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
