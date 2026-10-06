"""Durable workspace strategy authoring and published-manifest projection."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from thesis.persistence.git import (
    GitCoordinator,
    GitPublicationConflict,
    GitPublicationError,
)
from thesis.persistence.journal import (
    ArtifactRef,
    CommandJournal,
    content_hash,
)
from thesis.strategies.contracts import StrategyDesign
from thesis.strategies.validation import (
    StrategySandboxValidator,
    StructuralStrategyValidator,
    validate_strategy_bundle,
)
from thesis.workspaces.repository import WorkspaceRepositoryProvider

_VersionHistory = list[tuple[str, dict[str, object], str]]
_VerifiedVersions = tuple[tuple[str, dict[str, object], str], ...]


class StrategyArtifactError(ValueError):
    """A strategy artifact or completion receipt is invalid or unpublished."""


class WorkspaceStrategyArtifacts:
    """Author immutable versions through journal acceptance and fenced publication."""

    def __init__(
        self,
        provider: WorkspaceRepositoryProvider,
        validator: StrategySandboxValidator | None = None,
    ) -> None:
        self.provider = provider
        self.validator = (
            validator if validator is not None else StructuralStrategyValidator()
        )
        self._versions_cache: dict[str, tuple[str | None, _VerifiedVersions]] = {}

    def author(
        self,
        workspace_id: str,
        execution_id: str,
        strategy: Mapping[str, object],
        name: str,
        symbol: str,
        description: str = "",
    ) -> dict[str, object]:
        """Accept, commit, publish, and return a verifiable completion receipt.

        Scope comes exclusively from caller IDs. Retrying accepted content recovers
        its original commit and retries provider publication before recording success.
        """
        _scope(execution_id)
        for label, value, limit in (
            ("name", name, 256),
            ("symbol", symbol, 32),
            ("description", description, 1024),
        ):
            if (
                not isinstance(value, str)
                or len(value) > limit
                or (label != "description" and not value.strip())
            ):
                raise StrategyArtifactError(f"invalid {label}")
        design = StrategyDesign.from_mapping(strategy)
        bundle = design.bundle
        # This mandatory structural check is not a compilation sandbox. A composed
        # validator may add real compile/run checks through the existing seam.
        validate_strategy_bundle(bundle, StructuralStrategyValidator())
        decision = validate_strategy_bundle(bundle, self.validator)
        slug = re.sub(r"[^a-z0-9]+", "-", bundle.strategy_id.lower()).strip("-")
        if not slug:
            raise StrategyArtifactError("strategyId must produce a safe slug")
        if slug in {"con", "prn", "aux", "nul"} or re.fullmatch(
            r"(?:com|lpt)[1-9]", slug
        ):
            slug += "-strategy"
        if bundle.version.endswith(".") or len(bundle.version) > 128:
            raise StrategyArtifactError("version must be a safe directory name")
        artifact_path = f"strategies/{slug}/{bundle.version}"
        idempotency_id = "strategy-" + content_hash(artifact_path)
        publication_id = f"pub-{content_hash(idempotency_id)[:48]}"
        source_hash = bundle.source[0].sha256
        manifest: dict[str, object] = {
            "schema": "fca-workspace-strategy-v1",
            "publicationId": publication_id,
            "artifactPath": artifact_path,
            "workspaceId": workspace_id,
            "executionId": execution_id,
            "strategyId": bundle.strategy_id,
            "version": bundle.version,
            "parentVersion": design.parent_version,
            "entrypoint": "src/lib.rs",
            "bundleSha256": bundle.bundle_sha256,
            "sourceSha256": source_hash,
            "bundle": bundle.canonical_document,
            "exitCriteria": list(design.exit_criteria),
            "name": name,
            "symbol": symbol,
            "description": description,
            "status": "draft",
            "validation": {
                "accepted": True,
                "diagnostics": list(decision.diagnostics),
                "validationId": decision.validation_id,
            },
        }
        files = {
            f"{artifact_path}/{file.path}": file.content
            for file in (*bundle.source, *bundle.tests)
        }
        files[f"{artifact_path}/manifest.json"] = _json_bytes(manifest)
        files[f"{artifact_path}/.gitattributes"] = b"* -text\n"
        root = self.provider.open(workspace_id).root
        git = GitCoordinator(root)
        guard = GitCoordinator(root, lock_name="strategy-authoring.lock")
        with guard.publication_lock():
            if design.parent_version is not None:
                if design.parent_version == bundle.version:
                    raise StrategyArtifactError(
                        "parentVersion must not reference itself"
                    )
                parent = self._verified(
                    root,
                    git,
                    f"strategies/{slug}/{design.parent_version}/manifest.json",
                )
                if (
                    parent is None
                    or parent[0]["strategyId"] != bundle.strategy_id
                    or parent[0]["workspaceId"] != workspace_id
                ):
                    raise StrategyArtifactError(
                        "parentVersion must be published in the same strategy lineage"
                    )
            journal = CommandJournal(_confined(root, ".git/thesis-strategies.sqlite3"))
            journal.verify_integrity()
            # Validate every destination before accepting a command or writing bytes.
            for relative, payload in files.items():
                path = _confined(root, relative)
                if path.exists() and (
                    not path.is_file() or path.read_bytes() != payload
                ):
                    raise GitPublicationConflict(
                        "immutable strategy version already differs"
                    )
            state: dict[str, object] = {
                "artifactPath": artifact_path,
                "manifest": manifest,
                "files": {
                    path: base64.b64encode(payload).decode("ascii")
                    for path, payload in files.items()
                },
            }
            entry = journal.accept(
                "author_strategy",
                {
                    "workspaceId": workspace_id,
                    "executionId": execution_id,
                    "bundleSha256": bundle.bundle_sha256,
                },
                state,
                workflow_hash=content_hash("workspace-strategy-authoring-v1"),
                model_hash=bundle.bundle_sha256,
                artifacts=(
                    ArtifactRef.from_bytes(path, payload)
                    for path, payload in files.items()
                ),
                idempotency_id=idempotency_id,
            )
            try:
                commit = git.find_artifact_publication(publication_id, files)
                if commit is None:
                    commit = git.publish(
                        entry, expected_head=git.head(), artifacts=files
                    ).commit_hash
                else:
                    git.recover_artifacts(commit, files)
                self.provider.publish(workspace_id)
                # This immutable marker attests that the artifact commit was accepted
                # by the provider. It is created only after that publish succeeds.
                marker_path = f"{artifact_path}/publication.json"
                marker = _json_bytes(
                    {
                        "schema": "fca-strategy-publication-v1",
                        "publicationId": publication_id,
                        "gitCommit": commit,
                        "manifestSha256": content_hash(
                            files[f"{artifact_path}/manifest.json"]
                        ),
                    }
                )
                marker_entry = journal.accept(
                    "publish_strategy_receipt",
                    {"publicationId": publication_id},
                    {"artifactPath": artifact_path, "gitCommit": commit},
                    workflow_hash=content_hash("workspace-strategy-authoring-v1"),
                    model_hash=bundle.bundle_sha256,
                    artifacts=[ArtifactRef.from_bytes(marker_path, marker)],
                    idempotency_id="strategy-receipt-" + content_hash(artifact_path),
                )
                marker_files = {marker_path: marker}
                marker_commit = git.find_artifact_publication(
                    marker_entry.publication_id, marker_files
                )
                if marker_commit is None:
                    marker_commit = git.publish(
                        marker_entry, expected_head=git.head(), artifacts=marker_files
                    ).commit_hash
                else:
                    git.recover_artifacts(marker_commit, marker_files)
                self.provider.publish(workspace_id)
                journal.record_published(
                    marker_entry.publication_id,
                    marker_commit,
                    git_head_before=git.parent(marker_commit),
                )
                journal.record_published(
                    entry.publication_id,
                    commit,
                    git_head_before=git.parent(commit),
                )
            except Exception as error:
                journal.release(entry.publication_id, error)
                raise
            return self._receipt(manifest, commit)

    def verify_completion(
        self,
        workspace_id: str,
        execution_id: str,
        receipt: Mapping[str, object],
    ) -> dict[str, object]:
        """Verify caller scope, full receipt, durable publication and artifact hashes."""
        _scope(execution_id)
        root = self.provider.open(workspace_id).root
        git = GitCoordinator(root)
        artifact_path = receipt.get("artifactPath")
        if not isinstance(artifact_path, str):
            raise StrategyArtifactError("completion receipt requires artifactPath")
        verified = self._verified(root, git, f"{artifact_path}/manifest.json")
        if verified is None:
            raise StrategyArtifactError(
                "strategy publication is missing or hash verification failed"
            )
        manifest, commit = verified
        if (
            manifest.get("workspaceId") != workspace_id
            or manifest.get("executionId") != execution_id
        ):
            raise StrategyArtifactError(
                "completion receipt scope does not match caller"
            )
        expected = self._receipt(manifest, commit)
        if dict(receipt) != expected:
            raise StrategyArtifactError(
                "completion receipt does not match published strategy"
            )
        return expected

    def list(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        """Project only durable, published and hash-verified manifests for the UI."""
        root, git, _head, verified_versions = self._verified_versions(workspace_id)
        groups: dict[str, dict[str, object]] = {}
        order = (
            {commit: index for index, commit in enumerate(git.commit_order())}
            if verified_versions
            else {}
        )
        for created_at, manifest, _commit in sorted(
            verified_versions, key=lambda item: order[item[2]]
        ):
            if manifest["workspaceId"] != workspace_id:
                continue
            strategy_id = cast(str, manifest["strategyId"])
            previous = groups.get(strategy_id)
            versions = (
                []
                if previous is None
                else cast(list[dict[str, object]], previous["versions"])
            )
            versions.append(
                {
                    "version": manifest["version"],
                    "created_at": created_at,
                    "parent_version": manifest["parentVersion"],
                    "note": "Authored",
                }
            )
            source_path = f"{manifest['artifactPath']}/{manifest['entrypoint']}"
            groups[strategy_id] = {
                "id": strategy_id,
                "workspace_id": workspace_id,
                "name": manifest["name"],
                "symbol": manifest["symbol"],
                "description": manifest["description"],
                "status": manifest["status"],
                "pnl": 0.0,
                "return_pct": 0.0,
                "win_rate": 0.0,
                "capital": 0.0,
                "code": _confined(root, source_path).read_text(encoding="utf-8"),
                "versions": versions,
                "created_at": created_at
                if previous is None
                else previous["created_at"],
            }
        return tuple(groups[key] for key in sorted(groups))

    def catalog(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        """Project verified manifests with latest-version files and history."""
        _root, git, _head, verified_versions = self._verified_versions(workspace_id)
        order = (
            {commit: index for index, commit in enumerate(git.commit_order())}
            if verified_versions
            else {}
        )
        groups: dict[str, list[tuple[str, dict[str, object], str]]] = {}
        for created_at, manifest, commit in sorted(
            verified_versions, key=lambda item: order[item[2]]
        ):
            if manifest["workspaceId"] != workspace_id:
                continue
            strategy_id = cast(str, manifest["strategyId"])
            groups.setdefault(strategy_id, []).append((created_at, manifest, commit))
        return tuple(
            self._catalog_entry(workspace_id, strategy_id, groups[strategy_id])
            for strategy_id in sorted(groups)
        )

    def read_file(
        self, workspace_id: str, strategy_id: str, relative_path: str
    ) -> bytes:
        """Read one latest-version file from a strategy's published artifact."""
        root, git, head, verified_versions = self._verified_versions(workspace_id)
        if head is None:
            raise StrategyArtifactError("workspace has no committed strategies")
        order = {commit: index for index, commit in enumerate(git.commit_order())}
        selected: tuple[dict[str, object], str] | None = None
        for _created_at, manifest, commit in verified_versions:
            if (
                manifest["workspaceId"] != workspace_id
                or manifest["strategyId"] != strategy_id
            ):
                continue
            if selected is None or order[commit] > order[selected[1]]:
                selected = (manifest, commit)
        if selected is None:
            raise KeyError(strategy_id)
        manifest, _commit = selected
        allowed = {"manifest.json", "publication.json"} | _bundle_paths(
            manifest["bundle"]
        )
        if relative_path not in allowed:
            raise KeyError(relative_path)
        return _confined(
            root, f"{manifest['artifactPath']}/{relative_path}"
        ).read_bytes()

    def _verified_versions(
        self, workspace_id: str
    ) -> tuple[Path, GitCoordinator, str | None, _VerifiedVersions]:
        """Return HEAD-keyed verified manifests, cached until the next commit."""
        root = self.provider.open(workspace_id).root
        git = GitCoordinator(root)
        head = git.head()
        cached = self._versions_cache.get(workspace_id)
        if cached is not None and cached[0] == head:
            return root, git, head, cached[1]
        verified_versions: list[tuple[str, dict[str, object], str]] = []
        for path in git.tracked_paths("strategies"):
            if re.fullmatch(r"strategies/[^/]+/[^/]+/manifest\.json", path) is None:
                continue
            verified = self._verified(root, git, path)
            if verified is not None:
                manifest, commit = verified
                verified_versions.append((git.commit_time(commit), manifest, commit))
        result = tuple(verified_versions)
        if len(self._versions_cache) > 16:
            self._versions_cache.clear()
        self._versions_cache[workspace_id] = (head, result)
        return root, git, head, result

    def _catalog_entry(
        self,
        workspace_id: str,
        strategy_id: str,
        versions: _VersionHistory,
    ) -> dict[str, object]:
        _created_at, manifest, commit = versions[-1]
        bundle = cast(dict[str, object], manifest["bundle"])
        manifest_bytes = _json_bytes(manifest)
        files: list[dict[str, object]] = []
        for collection_name in ("source", "tests"):
            collection = bundle.get(collection_name)
            if not isinstance(collection, list):
                continue
            for file in collection:
                if not isinstance(file, Mapping):
                    continue
                payload = base64.b64decode(cast(str, file["content"]), validate=True)
                files.append(
                    _file_entry(
                        cast(str, file["path"]), cast(str, file["sha256"]), len(payload)
                    )
                )
        files.append(
            _file_entry(
                "manifest.json", content_hash(manifest_bytes), len(manifest_bytes)
            )
        )
        marker = _json_bytes(
            {
                "schema": "fca-strategy-publication-v1",
                "publicationId": manifest["publicationId"],
                "gitCommit": commit,
                "manifestSha256": content_hash(manifest_bytes),
            }
        )
        files.append(_file_entry("publication.json", content_hash(marker), len(marker)))
        files.sort(key=lambda item: cast(str, item["path"]))
        limitations_value = bundle.get("limitations")
        limitations: list[str] = (
            [item for item in limitations_value if isinstance(item, str)]
            if isinstance(limitations_value, list)
            else []
        )
        history = [
            {
                "label": f"v{version_manifest['version']}",
                "created_at": version_created_at,
                "note": (
                    f"from v{version_manifest['parentVersion']}"
                    if version_manifest["parentVersion"]
                    else "root"
                ),
            }
            for version_created_at, version_manifest, _version_commit in versions
        ]
        return {
            "id": strategy_id,
            "workspace_id": workspace_id,
            "kind": "strategy",
            "title": manifest["name"],
            "subtitle": f"{strategy_id} · {manifest['symbol']}",
            "description": manifest["description"],
            "status": manifest["status"],
            "version": manifest["version"],
            "artifact_path": manifest["artifactPath"],
            "created_at": versions[0][0],
            "updated_at": versions[-1][0],
            "git_commit": commit,
            "digests": {
                "source_sha256": manifest["sourceSha256"],
                "bundle_sha256": manifest["bundleSha256"],
                "manifest_sha256": content_hash(manifest_bytes),
            },
            "inputs": _strategy_inputs(bundle.get("harnessConfiguration")),
            "files": files,
            "file_count": len(files),
            "size_bytes": sum(cast(int, item["size_bytes"]) for item in files),
            "history": history,
            "preview": {
                "type": "strategy",
                "entrypoint": manifest["entrypoint"],
                "symbol": manifest["symbol"],
                "exit_criteria": _exit_criteria(manifest.get("exitCriteria")),
                "limitations": limitations,
                "signals": None,
            },
        }

    @staticmethod
    def _verified(
        root: Path, git: GitCoordinator, manifest_path: str
    ) -> tuple[dict[str, object], str] | None:
        try:
            head = git.head()
            if head is None:
                return None
            raw_manifest = git.read_file(head, manifest_path)
            manifest = json.loads(raw_manifest)
            artifact_path = manifest["artifactPath"]
            if (
                manifest["schema"] != "fca-workspace-strategy-v1"
                or re.fullmatch(r"strategies/[^/]+/[^/]+", artifact_path) is None
                or manifest_path != f"{artifact_path}/manifest.json"
                or _json_bytes(manifest) != raw_manifest
                or manifest["entrypoint"] != "src/lib.rs"
            ):
                return None
            bundle = manifest["bundle"]
            canonical = json.dumps(
                bundle,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("utf-8")
            if (
                hashlib.sha256(
                    b"fca:thesis-strategy-bundle:v1\0" + canonical
                ).hexdigest()
                != manifest["bundleSha256"]
            ):
                return None
            files = {manifest_path: raw_manifest}
            files[f"{artifact_path}/.gitattributes"] = b"* -text\n"
            for file in (*bundle["source"], *bundle["tests"]):
                payload = base64.b64decode(file["content"], validate=True)
                if content_hash(payload) != file["sha256"]:
                    return None
                files[f"{artifact_path}/{file['path']}"] = payload
            if (
                content_hash(files[f"{artifact_path}/src/lib.rs"])
                != manifest["sourceSha256"]
            ):
                return None
            commit = git.find_artifact_publication(manifest["publicationId"], files)
            if commit is None:
                return None
            marker_path = f"{artifact_path}/publication.json"
            marker_bytes = git.read_file(head, marker_path)
            expected_marker = {
                "schema": "fca-strategy-publication-v1",
                "publicationId": manifest["publicationId"],
                "gitCommit": commit,
                "manifestSha256": content_hash(raw_manifest),
            }
            marker_id = "strategy-receipt-" + content_hash(artifact_path)
            if (
                marker_bytes != _json_bytes(expected_marker)
                or git.find_artifact_publication(
                    f"pub-{content_hash(marker_id)[:48]}", {marker_path: marker_bytes}
                )
                is None
            ):
                return None
            files[marker_path] = marker_bytes
            if not all(
                git.file_matches(head, path, payload)
                and _confined(root, path).read_bytes() == payload
                for path, payload in files.items()
            ):
                return None
            return manifest, commit
        except (OSError, ValueError, KeyError, TypeError, GitPublicationError):
            return None

    @staticmethod
    def _receipt(manifest: Mapping[str, object], commit: str) -> dict[str, object]:
        return {
            "workspaceId": manifest["workspaceId"],
            "executionId": manifest["executionId"],
            "strategyId": manifest["strategyId"],
            "version": manifest["version"],
            "bundleSha256": manifest["bundleSha256"],
            "sourceSha256": manifest["sourceSha256"],
            "manifestSha256": content_hash(_json_bytes(manifest)),
            "artifactPath": manifest["artifactPath"],
            "status": "draft",
            "validation": manifest["validation"],
            "publicationId": manifest["publicationId"],
            "gitCommit": commit,
        }


def _scope(value: str) -> None:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}", value) is None
    ):
        raise StrategyArtifactError("execution_id must be a safe identifier")


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _file_entry(path: str, sha256: str, size_bytes: int) -> dict[str, object]:
    return {
        "path": path,
        "sha256": sha256,
        "size_bytes": size_bytes,
        "viewer": _viewer_for(path),
    }


def _viewer_for(path: str) -> str:
    suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if suffix == "json":
        return "json"
    if suffix in {"md", "markdown"}:
        return "markdown"
    if suffix in {"rs", "toml", "yaml", "yml", "lock"}:
        return "code"
    return "text"


def _bundle_paths(bundle: object) -> set[str]:
    if not isinstance(bundle, Mapping):
        return set()
    paths: set[str] = set()
    for collection_name in ("source", "tests"):
        collection = bundle.get(collection_name)
        if not isinstance(collection, list):
            continue
        for file in collection:
            if isinstance(file, Mapping) and isinstance(file.get("path"), str):
                paths.add(file["path"])
    return paths


def _strategy_inputs(configuration: object) -> list[dict[str, object]]:
    if not isinstance(configuration, Mapping):
        return []
    subscriptions: object = configuration.get("subscriptions")
    if subscriptions is None:
        data = configuration.get("data")
        subscriptions = data.get("subscriptions") if isinstance(data, Mapping) else None
    if not isinstance(subscriptions, list):
        return []
    inputs: list[dict[str, object]] = []
    for item in subscriptions:
        if not isinstance(item, Mapping):
            continue
        data_type = item.get("data_type")
        symbols = item.get("symbols")
        interval = item.get("interval")
        inputs.append(
            {
                "data_type": data_type if isinstance(data_type, str) else "unknown",
                "symbols": (
                    [symbol for symbol in symbols if isinstance(symbol, str)]
                    if isinstance(symbols, list)
                    else []
                ),
                "interval": interval if isinstance(interval, str) else None,
            }
        )
    return inputs


def _exit_criteria(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    criteria: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        criteria.append(
            {
                "falsifier_id": item.get("falsifierId"),
                "observable": item.get("observable"),
                "condition": item.get("condition"),
                "threshold": item.get("threshold"),
                "description": item.get("description"),
            }
        )
    return criteria


def _confined(root: Path, relative: str) -> Path:
    current = root
    parts = relative.split("/")
    if any(part in {"", ".", ".."} or "\\" in part or ":" in part for part in parts):
        raise StrategyArtifactError("artifact path must be confined")
    for part in parts:
        if current.exists() and not current.is_dir():
            raise StrategyArtifactError("artifact parent path must be a directory")
        if current.is_dir() and any(
            child.name.casefold() == part.casefold() and child.name != part
            for child in current.iterdir()
        ):
            raise StrategyArtifactError("artifact path has a case collision")
        current = current / part
        if current.is_symlink():
            raise StrategyArtifactError("artifact path must not contain symlinks")
    if not current.resolve().is_relative_to(root.resolve()):
        raise StrategyArtifactError("artifact path escapes workspace")
    return current


__all__ = ["StrategyArtifactError", "WorkspaceStrategyArtifacts"]
