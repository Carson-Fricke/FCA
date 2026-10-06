"""Committed, workspace-scoped Rust programs and exact-input validation receipts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import uuid4

from thesis.integrations.mcp import HarnessMcpError, McpToolResult
from thesis.persistence.git import GitCoordinator, GitPublicationError
from thesis.research.models import (
    MARKET_SUBSCRIPTIONS_SCHEMA,
    ResearchModelError,
    validate_market_configuration,
    validate_market_subscriptions,
)
from thesis.workspaces.local import WorkspaceRepositoryError
from thesis.workspaces.repository import WorkspaceRepositoryProvider

ProgramKind = Literal["study", "strategy", "indicator"]
_DIRECTORIES = {"study": "studies", "strategy": "strategies", "indicator": "indicators"}
_RESERVED_IDS = {"con", "prn", "aux", "nul"} | {
    f"{prefix}{i}" for prefix in ("com", "lpt") for i in range(1, 10)
}
PROGRAM_ID_PATTERN = r"^[a-z][a-z0-9_-]{0,63}$"
MAX_READ_CHARS = 16000
MAX_LIST_RESULTS = 200
_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
PROGRAM_CONFIGURATION_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["start", "end", "subscriptions"],
    "properties": {
        "start": {"type": "string", "pattern": _DATE_PATTERN},
        "end": {"type": "string", "pattern": _DATE_PATTERN},
        "subscriptions": MARKET_SUBSCRIPTIONS_SCHEMA,
    },
}

STUDY_TEMPLATE = """use harness_strategy_sdk::{export_study_program, BarFrame, MarketConsumer,
    MetricPublisher, ObservationContext, StudyProgram, Subscription};
use harness_strategy_sdk::serde_json::Value;
#[derive(Default)]
struct AuthoredStudy { frames: u64 }
impl MarketConsumer for AuthoredStudy {
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription::Bars { symbols: vec!["SPY".into()], interval: "1min".into() }]
    }
    fn on_bar(&mut self, _: &BarFrame, _: &mut ObservationContext) { self.frames += 1; }
}
impl StudyProgram for AuthoredStudy {
    fn new(_: Value) -> Self { Self::default() }
    fn on_stop(&mut self, metrics: &mut MetricPublisher) {
        let _ = metrics.publish("study_frames", self.frames as f64);
    }
}
export_study_program!(AuthoredStudy);
"""
STRATEGY_TEMPLATE = """use harness_strategy_sdk::{export_strategy_program, MarketConsumer,
    OrderSink, StrategyProgram, Subscription};
use harness_strategy_sdk::serde_json::Value;
struct AuthoredStrategy;
impl MarketConsumer for AuthoredStrategy {
    type Context = OrderSink;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription::Bars { symbols: vec!["SPY".into()], interval: "1min".into() }]
    }
}
impl StrategyProgram for AuthoredStrategy {
    fn new(_: Value) -> Self { Self }
}
export_strategy_program!(AuthoredStrategy);
"""
INDICATOR_TEMPLATE = """use harness_strategy_sdk::{BarFrame, Decimal, Indicator, MarketConsumer,
    ObservationContext, Subscription};
#[derive(Default)]
pub struct AuthoredIndicator { last: Option<Decimal> }
impl MarketConsumer for AuthoredIndicator {
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription::Bars { symbols: vec!["SPY".into()], interval: "1min".into() }]
    }
    fn on_bar(&mut self, frame: &BarFrame, _: &mut ObservationContext) {
        self.last = frame.bars_by_symbol.get("SPY").map(|bar| bar.close);
    }
}
impl Indicator for AuthoredIndicator {
    fn value(&self) -> Option<Decimal> { self.last }
}
"""
PROGRAM_TEMPLATES = {
    "study": STUDY_TEMPLATE,
    "strategy": STRATEGY_TEMPLATE,
    "indicator": INDICATOR_TEMPLATE,
}
_SDK_TOPICS = {
    "consumer": "strategy-sdk/src/consumer.rs",
    "program": "strategy-sdk/src/program.rs",
    "indicator_api": "strategy-sdk/src/indicator.rs",
    "events": "strategy-sdk/src/events.rs",
    "exports": "strategy-sdk/src/export.rs",
    "decimal": "num/src/decimal.rs",
}


class ProgramArtifactError(ValueError):
    """Invalid identity, stale edit/validation, or an unavailable program capability."""


class ProgramPublicationError(ProgramArtifactError):
    """A local commit exists but provider publication needs explicit reconciliation."""

    def __init__(self, receipt: Mapping[str, object]) -> None:
        super().__init__(
            "Program is committed locally; publication or checkout materialization failed. Inspect the pinned receipt. Use the matching publish tool only to retry repository publication; it never repairs checkout/index state. Ask the operator to reconcile checkout failures; do not re-author or replay execution."
        )
        self.receipt = dict(receipt)


class ProgramHarnessResult(Protocol):
    """Common result surface of the MCP client and HarnessToolProvider."""

    @property
    def is_error(self) -> bool: ...

    @property
    def structured(self) -> Mapping[str, object]: ...

    @property
    def text(self) -> str: ...


class ProgramHarness(Protocol):
    """Inject the existing MCP client or provider, without another transport."""

    def call_tool(
        self, name: str, arguments: Mapping[str, object] | None = None
    ) -> ProgramHarnessResult: ...

    def current_server_info(self) -> Mapping[str, object]: ...

    def read_sdk_reference(self, **arguments: object) -> ProgramHarnessResult: ...


def _bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    except (TypeError, ValueError) as error:
        raise ProgramArtifactError("program values must be JSON-shaped") from error


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


_HistoryEntries = list[dict[str, object]]


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


def _program_inputs(configuration: object) -> list[dict[str, object]]:
    if not isinstance(configuration, Mapping):
        return []
    subscriptions = configuration.get("subscriptions")
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


def _program_preview(
    kind: ProgramKind, manifest: Mapping[str, object], configuration: object
) -> dict[str, object]:
    if kind == "study":
        start = None
        end = None
        if isinstance(configuration, Mapping):
            raw_start = configuration.get("start")
            raw_end = configuration.get("end")
            start = raw_start if isinstance(raw_start, str) else None
            end = raw_end if isinstance(raw_end, str) else None
        dependencies = manifest.get("indicatorIds")
        return {
            "type": "study",
            "window": {"start": start, "end": end},
            "indicator_ids": (
                [item for item in dependencies if isinstance(item, str)]
                if isinstance(dependencies, list)
                else []
            ),
            "metrics": [],
        }
    if kind == "indicator":
        return {"type": "indicator", "value_series": None, "used_by": []}
    return {
        "type": "strategy",
        "entrypoint": "program.rs",
        "symbol": "",
        "exit_criteria": [],
        "limitations": [],
        "signals": None,
    }


class WorkspacePrograms:
    """Own author/update/read/validate/run by kind and ID, never caller paths.

    ``version`` is a content identity, not HEAD. Indicators are imported as Rust
    modules named by their IDs; manifests declare all transitive dependencies.
    Harness compilation is authoritative: no structural-only acceptance fallback.
    """

    def __init__(
        self,
        repositories: WorkspaceRepositoryProvider,
        harness: ProgramHarness | None = None,
        *,
        sdk_root: Path | None = None,
    ) -> None:
        self.repositories = repositories
        self.harness = harness
        self.sdk_root = (
            sdk_root or Path(__file__).resolve().parents[2] / "harness" / "strategy-sdk"
        )
        self._catalog_cache: dict[
            tuple[str, str], tuple[str, tuple[dict[str, object], ...]]
        ] = {}

    def _path(
        self,
        workspace_id: str,
        kind: str,
        program_id: str,
        root: Path | None = None,
    ) -> str:
        if (
            kind not in _DIRECTORIES
            or re.fullmatch(PROGRAM_ID_PATTERN, program_id) is None
            or program_id in _RESERVED_IDS
            or (
                kind == "indicator"
                and ("-" in program_id or program_id in {"self", "super", "crate"})
            )
        ):
            raise ProgramArtifactError(
                "kind and program_id must be safe program identifiers"
            )
        relative = f"{_DIRECTORIES[kind]}/{program_id}"
        if root is None:
            root = self.repositories.open(workspace_id).root
            self.repositories.resolve_path(workspace_id, relative)
        current = root
        for part in relative.split("/"):
            if current.is_dir() and any(
                child.name.casefold() == part.casefold() and child.name != part
                for child in current.iterdir()
            ):
                raise ProgramArtifactError("program path has a case collision")
            current = current / part
            if current.is_symlink():
                raise ProgramArtifactError("program paths must not contain symlinks")
        return relative

    @staticmethod
    def _valid_program_id(kind: str, program_id: str) -> bool:
        if (
            re.fullmatch(PROGRAM_ID_PATTERN, program_id) is None
            or program_id in _RESERVED_IDS
        ):
            return False
        return not (
            kind == "indicator"
            and ("-" in program_id or program_id in {"self", "super", "crate"})
        )

    def author(
        self,
        workspace_id: str,
        kind: ProgramKind,
        program_id: str,
        source: str | None = None,
        *,
        configuration: Mapping[str, object] | None = None,
        indicator_ids: Sequence[str] = (),
        indicator_type: str = "AuthoredIndicator",
        template: str | None = None,
    ) -> dict[str, object]:
        relative = self._path(workspace_id, kind, program_id)
        root = self.repositories.open(workspace_id).root
        git = GitCoordinator(root)
        with git.publication_lock():
            head = git.head()
            if any(
                path.startswith(relative + "/")
                for path in git.tracked_paths(_DIRECTORIES[kind])
            ):
                raise ProgramArtifactError(
                    "program already exists; use a targeted update"
                )
            manifest = {
                "schema": "fca-workspace-program-v1",
                "kind": kind,
                "id": program_id,
                "configuration": dict(configuration or {}),
                "indicatorIds": list(indicator_ids),
                "indicatorType": indicator_type,
            }
            self._check_manifest(workspace_id, manifest)
            if source is not None and template is not None:
                raise ProgramArtifactError("provide source or template, not both")
            if source is None:
                source = self.template_source(
                    kind, manifest["configuration"], template=template or "basic"
                )
            payload = self._source_bytes(source)
            files = {
                f"{relative}/program.rs": payload,
                f"{relative}/manifest.json": _bytes(manifest),
            }
            commit = self._commit(workspace_id, git, head, files, "author " + kind)
            return self._receipt(workspace_id, kind, program_id, commit)

    def update(
        self,
        workspace_id: str,
        kind: ProgramKind,
        program_id: str,
        *,
        expected_version: str,
        expected: str | None = None,
        replacement: str | None = None,
        configuration: Mapping[str, object] | None = None,
        indicator_ids: Sequence[str] | None = None,
    ) -> dict[str, object]:
        relative = self._path(workspace_id, kind, program_id)
        git = GitCoordinator(self.repositories.open(workspace_id).root)
        with git.publication_lock():
            head = self._head(git)
            snapshot = self._snapshot(workspace_id, kind, program_id, head)
            if snapshot["version"] != expected_version:
                raise ProgramArtifactError(
                    "stale program version; read before updating"
                )
            source = cast(str, snapshot["source"])
            if (expected is None) != (replacement is None):
                raise ProgramArtifactError(
                    "expected and replacement must be provided together"
                )
            updated = source
            if expected is not None:
                if not expected or source.count(expected) != 1:
                    raise ProgramArtifactError(
                        "expected content must match exactly once"
                    )
                updated = source.replace(expected, cast(str, replacement), 1)
            manifest = cast(dict[str, object], snapshot["manifest"])
            if configuration is not None:
                manifest["configuration"] = dict(configuration)
            if indicator_ids is not None:
                manifest["indicatorIds"] = list(indicator_ids)
            self._check_manifest(workspace_id, manifest)
            files = {
                f"{relative}/program.rs": self._source_bytes(updated),
                f"{relative}/manifest.json": _bytes(manifest),
            }
            if all(
                git.file_matches(head, path, payload) for path, payload in files.items()
            ):
                raise ProgramArtifactError("update must modify the artifact")
            commit = self._commit(workspace_id, git, head, files, "update " + kind)
            return self._receipt(workspace_id, kind, program_id, commit)

    def load(
        self,
        workspace_id: str,
        kind: ProgramKind,
        program_id: str,
        commit: str | None = None,
    ) -> dict[str, object]:
        """Load one reachable pinned snapshot for trusted workflow composition.

        Unlike agent-facing ranged reads, source includes indicator modules for
        studies/strategies. Every dependency is read from the same commit, never
        from the working tree or a newer dependency revision.
        """
        git = GitCoordinator(self.repositories.open(workspace_id).root)
        selected = self._head(git) if commit is None else commit
        if (
            not isinstance(selected, str)
            or re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", selected) is None
        ):
            raise ProgramArtifactError("commit must be a full Git object ID")
        try:
            reachable = git.is_ancestor(selected)
        except GitPublicationError as error:
            raise ProgramArtifactError(
                "commit is not reachable in the selected workspace"
            ) from error
        if not reachable:
            raise ProgramArtifactError(
                "commit is not reachable in the selected workspace"
            )
        snapshot = self._snapshot(workspace_id, kind, program_id, selected)
        manifest = cast(dict[str, object], snapshot["manifest"])
        dependencies = cast(dict[str, str], snapshot["dependencies"])
        files = cast(dict[str, str], snapshot["files"])
        validation_path = (
            f"{self._path(workspace_id, kind, program_id)}/validation.json"
        )
        validation = None
        # Check the selected tree, not latest HEAD. Absence is expected on the
        # pre-validation source commit; malformed committed receipts are errors.
        try:
            raw_validation = git.read_file(selected, validation_path)
        except GitPublicationError:
            raw_validation = None
        if raw_validation is not None:
            try:
                validation = json.loads(raw_validation)
            except ValueError as error:
                raise ProgramArtifactError(
                    "stored program validation is malformed"
                ) from error
            if not isinstance(validation, dict):
                raise ProgramArtifactError(
                    "stored program validation must be an object"
                )
        serving = self._matched_sdk()
        return {
            **self._receipt(workspace_id, kind, program_id, selected),
            "source": self._assembled_source(snapshot, kind)
            if kind != "indicator"
            else snapshot["source"],
            "configuration": manifest["configuration"],
            "manifest": manifest,
            "validation": validation,
            "sdkIdentity": serving["sdkIdentity"],
            "validationIdentity": serving.get("validationIdentity"),
            "dependencies": [
                {
                    "programId": name,
                    "kind": "indicator",
                    "gitCommit": selected,
                    "artifactPath": f"indicators/{name}",
                    "sourceSha256": files[f"indicators/{name}/program.rs"],
                    "manifestSha256": files[f"indicators/{name}/manifest.json"],
                }
                for name in sorted(dependencies)
            ],
        }

    def verify_completion(
        self,
        workspace_id: str,
        kind: ProgramKind,
        program_id: str,
        receipt: Mapping[str, object],
    ) -> dict[str, object]:
        """Verify a compact receipt against its pinned, reachable content snapshot.

        Scope is caller-owned. A verified historical snapshot need not be latest;
        completion policy decides whether a newer version must be accepted.
        """
        commit = receipt.get("gitCommit")
        if not isinstance(commit, str):
            raise ProgramArtifactError("completion receipt requires gitCommit")
        loaded = self.load(workspace_id, kind, program_id, commit)
        expected = self._receipt(workspace_id, kind, program_id, commit)
        if any(receipt.get(key) != value for key, value in expected.items()):
            raise ProgramArtifactError(
                "completion receipt does not match the pinned program"
            )
        if "sdkIdentity" in receipt and receipt["sdkIdentity"] != loaded["sdkIdentity"]:
            raise ProgramArtifactError("completion SDK identity is stale")
        if "validationIdentity" in receipt and (
            loaded["validationIdentity"] is None
            or receipt["validationIdentity"] != loaded["validationIdentity"]
        ):
            raise ProgramArtifactError(
                "completion compiler identity is unavailable or stale"
            )
        return loaded

    def publish(
        self,
        workspace_id: str,
        kind: ProgramKind,
        program_id: str,
        commit: str | None = None,
    ) -> dict[str, object]:
        """Explicitly retry repository publication after a known local commit."""
        git = GitCoordinator(self.repositories.open(workspace_id).root)
        with git.publication_lock():
            selected = self._head(git) if commit is None else commit
            if (
                not isinstance(selected, str)
                or re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", selected) is None
            ):
                raise ProgramArtifactError("commit must be a full Git object ID")
            if not git.is_ancestor(selected):
                raise ProgramArtifactError(
                    "commit is not reachable in the selected workspace"
                )
            receipt = self._receipt(workspace_id, kind, program_id, selected)
            try:
                # Parent bytes are not evidence of an interrupted publication:
                # an operator may intentionally stage a revert. Never repair here.
                self.repositories.publish(workspace_id)
            except Exception as error:
                raise ProgramPublicationError(receipt) from error
        return receipt

    def read(
        self,
        workspace_id: str,
        kind: ProgramKind,
        program_id: str,
        *,
        start_line: int = 1,
        end_line: int | None = None,
        max_chars: int = MAX_READ_CHARS,
    ) -> dict[str, object]:
        git = GitCoordinator(self.repositories.open(workspace_id).root)
        head = self._head(git)
        snapshot = self._snapshot(workspace_id, kind, program_id, head)
        result = self._receipt(workspace_id, kind, program_id, head)
        result.update(
            self._range(cast(str, snapshot["source"]), start_line, end_line, max_chars)
        )
        result["configuration"] = cast(dict[str, object], snapshot["manifest"])[
            "configuration"
        ]
        return result

    def list(
        self,
        workspace_id: str,
        kind: ProgramKind,
        *,
        prefix: str | None = None,
        limit: int = 50,
    ) -> dict[str, object]:
        """Enumerate committed IDs at HEAD without caller paths or source.

        Each entry is receipt-shaped (identity and content hashes from the same
         guarded snapshot as ``read``) plus a compact configuration summary, so
        provenance is visible before a read or an author attempt. Unsafe,
        malformed, or unreadable entries are skipped; an empty workspace lists
        nothing rather than failing.
        """
        if kind not in _DIRECTORIES:
            raise ProgramArtifactError("kind must be study, strategy, or indicator")
        if prefix is not None and (
            not isinstance(prefix, str)
            or re.fullmatch(r"[a-z0-9_-]{1,64}", prefix) is None
        ):
            raise ProgramArtifactError(
                "prefix must be a lowercase program identifier fragment"
            )
        if type(limit) is not int or not 1 <= limit <= MAX_LIST_RESULTS:
            raise ProgramArtifactError(
                f"limit must be an integer from 1 to {MAX_LIST_RESULTS}"
            )
        git = GitCoordinator(self.repositories.open(workspace_id).root)
        head = git.head()
        response: dict[str, object] = {
            "workspaceId": workspace_id,
            "kind": kind,
            "gitCommit": head,
            "programs": [],
            "count": 0,
        }
        if head is None:
            return response
        directory = _DIRECTORIES[kind]
        discovered: dict[str, set[str]] = {}
        for path in git.tracked_paths(directory):
            rest = path.removeprefix(directory + "/")
            parts = rest.split("/")
            if len(parts) != 2 or parts[1] not in ("program.rs", "manifest.json"):
                continue
            program_id, filename = parts
            if not self._valid_program_id(kind, program_id):
                continue
            if prefix is not None and not program_id.startswith(prefix):
                continue
            discovered.setdefault(program_id, set()).add(filename)
        programs: list[dict[str, object]] = []
        for program_id in sorted(discovered)[:limit]:
            if discovered[program_id] != {"program.rs", "manifest.json"}:
                continue
            try:
                snapshot = self._snapshot(workspace_id, kind, program_id, head)
            except (ProgramArtifactError, WorkspaceRepositoryError):
                continue
            entry = self._receipt_entry(kind, program_id, head, snapshot)
            programs.append(entry)
        response["programs"] = programs
        response["count"] = len(programs)
        return response

    def catalog(
        self, workspace_id: str, kind: ProgramKind
    ) -> tuple[dict[str, object], ...]:
        """Project committed programs with configuration, files and revisions.

        The projection is cached per workspace, kind, and HEAD: repeated page
        loads avoid re-reading every program while any new commit invalidates
        the entry. Discovery does not call ``list`` first, so each program is
        snapshotted exactly once per request.
        """
        if kind not in _DIRECTORIES:
            raise ProgramArtifactError("kind must be study, strategy, or indicator")
        git = GitCoordinator(self.repositories.open(workspace_id).root)
        commit = git.head()
        if commit is None:
            return ()
        cache_key = (workspace_id, kind)
        cached = self._catalog_cache.get(cache_key)
        if cached is not None and cached[0] == commit:
            return cached[1]
        directory = _DIRECTORIES[kind]
        discovered: dict[str, set[str]] = {}
        for path in git.tracked_paths(directory):
            rest = path.removeprefix(directory + "/")
            parts = rest.split("/")
            if len(parts) != 2 or parts[1] not in ("program.rs", "manifest.json"):
                continue
            program_id, filename = parts
            if not self._valid_program_id(kind, program_id):
                continue
            discovered.setdefault(program_id, set()).add(filename)
        complete = {
            program_id: files
            for program_id, files in discovered.items()
            if files == {"program.rs", "manifest.json"}
        }
        # Filter on manifests first: legacy or malformed programs are rejected
        # from one batched read without reading their sources.
        manifest_paths = {
            program_id: f"{directory}/{program_id}/manifest.json"
            for program_id in complete
        }
        manifests: dict[str, bytes] = {}
        try:
            manifests.update(git.read_blobs(commit, sorted(manifest_paths.values())))
        except GitPublicationError:
            for path in sorted(manifest_paths.values()):
                try:
                    manifests.update(git.read_blobs(commit, [path]))
                except GitPublicationError:
                    continue
        candidates: list[str] = []
        for program_id, path in manifest_paths.items():
            manifest_bytes = manifests.get(path)
            if manifest_bytes is None:
                continue
            try:
                manifest = json.loads(manifest_bytes)
            except ValueError:
                continue
            if (
                not isinstance(manifest, dict)
                or manifest.get("kind") != kind
                or manifest.get("id") != program_id
            ):
                continue
            try:
                self._check_configuration(manifest.get("configuration"))
            except ProgramArtifactError:
                continue
            declared = manifest.get("indicatorIds")
            if not isinstance(declared, list) or any(
                not isinstance(item, str) for item in declared
            ):
                continue
            candidates.append(program_id)
        entries: list[dict[str, object]] = []
        selected = sorted(candidates)[:MAX_LIST_RESULTS]
        preloaded = dict(manifests)
        if selected:
            try:
                preloaded.update(
                    git.read_blobs(
                        commit,
                        [
                            f"{directory}/{program_id}/program.rs"
                            for program_id in selected
                        ],
                    )
                )
            except GitPublicationError:
                # Individual snapshots fall back to their own verified reads.
                pass
        revisions = git.path_revisions_many(
            [f"{directory}/{program_id}" for program_id in selected]
        )
        for program_id in selected:
            try:
                snapshot = self._snapshot(
                    workspace_id, kind, program_id, commit, git, preloaded
                )
            except (ProgramArtifactError, WorkspaceRepositoryError):
                continue
            entries.append(
                self._catalog_entry(
                    workspace_id,
                    kind,
                    program_id,
                    commit,
                    snapshot,
                    git,
                    self._history_entries(revisions.get(f"{directory}/{program_id}")),
                )
            )
        result = tuple(entries)
        if len(self._catalog_cache) > 16:
            self._catalog_cache.clear()
        self._catalog_cache[cache_key] = (commit, result)
        return result

    def file_content(
        self,
        workspace_id: str,
        kind: ProgramKind,
        program_id: str,
        relative_path: str,
    ) -> bytes:
        """Read one committed program file by its declared name."""
        if relative_path not in ("program.rs", "manifest.json"):
            raise KeyError(relative_path)
        git = GitCoordinator(self.repositories.open(workspace_id).root)
        head = self._head(git)
        relative = self._path(workspace_id, kind, program_id)
        if f"{relative}/{relative_path}" not in git.tracked_paths(relative):
            raise KeyError(relative_path)
        return git.read_file(head, f"{relative}/{relative_path}")

    def _catalog_entry(
        self,
        workspace_id: str,
        kind: ProgramKind,
        program_id: str,
        commit: str,
        snapshot: Mapping[str, object],
        git: GitCoordinator,
        history: _HistoryEntries | None = None,
    ) -> dict[str, object]:
        manifest = cast(dict[str, object], snapshot["manifest"])
        relative = f"{_DIRECTORIES[kind]}/{program_id}"
        sizes = cast(dict[str, int], snapshot["sizes"])
        files = sorted(
            (
                _file_entry(
                    path.removeprefix(relative + "/"),
                    sha256,
                    sizes.get(path, 0),
                )
                for path, sha256 in cast(dict[str, str], snapshot["files"]).items()
                if path.startswith(relative + "/")
            ),
            key=lambda item: cast(str, item["path"]),
        )
        configuration = manifest.get("configuration")
        revisions = (
            history if history is not None else self._revision_history(git, relative)
        )[:20]
        return {
            "id": program_id,
            "workspace_id": workspace_id,
            "kind": kind,
            "title": program_id,
            "subtitle": relative,
            "description": "",
            "status": "committed",
            "version": snapshot["version"],
            "artifact_path": relative,
            "created_at": revisions[-1]["created_at"] if revisions else None,
            "updated_at": revisions[0]["created_at"] if revisions else None,
            "git_commit": commit,
            "digests": {
                "source_sha256": _hash(cast(str, snapshot["source"]).encode()),
                "bundle_sha256": snapshot["version"],
                "manifest_sha256": _hash(_bytes(manifest)),
            },
            "inputs": _program_inputs(configuration),
            "files": files,
            "file_count": len(files),
            "size_bytes": sum(cast(int, item["size_bytes"]) for item in files),
            "history": revisions,
            "preview": _program_preview(kind, manifest, configuration),
        }

    def _revision_history(self, git: GitCoordinator, relative: str) -> _HistoryEntries:
        return self._history_entries(git.path_revisions(relative))

    @staticmethod
    def _history_entries(
        revisions: Sequence[tuple[str, str]] | None,
    ) -> _HistoryEntries:
        return [
            {
                "label": commit[:10],
                "created_at": committed_at,
                "note": "revision",
            }
            for commit, committed_at in (revisions or ())
        ]

    def validate(
        self, workspace_id: str, kind: ProgramKind, program_id: str
    ) -> dict[str, object]:
        git = GitCoordinator(self.repositories.open(workspace_id).root)
        head = self._head(git)
        snapshot = self._snapshot(workspace_id, kind, program_id, head)
        source = self._assembled_source(snapshot, kind)
        serving = self._matched_sdk()
        sdk_identity = serving["sdkIdentity"]
        manifest = cast(dict[str, object], snapshot["manifest"])
        result = self._call(
            "validate_strategy" if kind == "strategy" else "validate_study",
            {
                "model": {
                    "source": source,
                    "harnessConfiguration": manifest["configuration"],
                }
            },
        )
        receipt = self._receipt(workspace_id, kind, program_id, head)
        validation_identity = result.structured.get("validationIdentity")
        if not isinstance(validation_identity, str) or not validation_identity:
            validation_identity = serving.get("validationIdentity")
        current_serving = self._matched_sdk()
        if current_serving["sdkIdentity"] != sdk_identity:
            raise ProgramArtifactError("serving SDK changed during validation")
        if validation_identity != current_serving.get("validationIdentity"):
            raise ProgramArtifactError("serving compiler changed during validation")
        if validation_identity is None and not result.is_error:
            result = McpToolResult(
                tool=result.tool,
                is_error=True,
                structured={
                    "valid": False,
                    "error": "Harness validation identity is unavailable; validate again.",
                },
            )
        receipt.update(
            {
                "accepted": not result.is_error
                and result.structured.get("valid") is True
                and validation_identity is not None,
                "sdkIdentity": sdk_identity,
                **self._outcome(result, validation=True),
                "validationIdentity": validation_identity,
            }
        )
        relative = self._path(workspace_id, kind, program_id)
        with git.publication_lock():
            current = self._head(git)
            if self._sdk_identity() != sdk_identity:
                raise ProgramArtifactError(
                    "SDK changed during validation; validate again"
                )
            if (
                self._snapshot(workspace_id, kind, program_id, current)["version"]
                != snapshot["version"]
            ):
                raise ProgramArtifactError(
                    "program changed during validation; validate again"
                )
            self._commit(
                workspace_id,
                git,
                current,
                {f"{relative}/validation.json": _bytes(receipt)},
                "validate " + kind,
            )
        return receipt

    def run(
        self, workspace_id: str, kind: ProgramKind, program_id: str
    ) -> dict[str, object]:
        if kind == "indicator":
            raise ProgramArtifactError(
                "indicators execute only as owned study/strategy dependencies"
            )
        git = GitCoordinator(self.repositories.open(workspace_id).root)
        head = self._head(git)
        snapshot = self._snapshot(workspace_id, kind, program_id, head)
        relative = self._path(workspace_id, kind, program_id)
        try:
            validation = json.loads(git.read_file(head, f"{relative}/validation.json"))
        except (GitPublicationError, ValueError) as error:
            raise ProgramArtifactError(
                "program requires validation before running"
            ) from error
        serving = self._matched_sdk()
        if not isinstance(validation, dict) or (
            validation.get("accepted") is not True
            or validation.get("version") != snapshot["version"]
            or validation.get("sdkIdentity") != serving["sdkIdentity"]
        ):
            raise ProgramArtifactError(
                "validation is rejected or stale; validate before running"
            )
        identity = serving.get("validationIdentity")
        if identity is None or validation.get("validationIdentity") != identity:
            raise ProgramArtifactError(
                "harness validation identity is unavailable or changed; validate before running"
            )
        manifest = cast(dict[str, object], snapshot["manifest"])
        configuration = dict(cast(dict[str, object], manifest["configuration"]))
        source = self._assembled_source(snapshot, kind)
        parameters = {"source": source}
        data = dict(cast(dict[str, object], configuration.get("data", {})))
        subscriptions = configuration.pop("subscriptions", None)
        if subscriptions is None:
            subscriptions = data.pop("subscriptions", None)
        configuration.pop("requiredSymbols", None)
        configuration.pop("required_symbols", None)
        configuration.pop("symbols", None)
        if not isinstance(subscriptions, list) or not subscriptions:
            raise ProgramArtifactError(
                "program configuration must declare subscriptions"
            )
        symbols = sorted(
            {
                symbol
                for item in cast(list[dict[str, object]], subscriptions)
                for symbol in cast(list[str], item["symbols"])
            }
        )
        data["subscriptions"] = subscriptions
        data["symbols"] = symbols
        for key in ("requiredSymbols", "required_symbols", "interval", "bar_interval"):
            data.pop(key, None)
        configuration["data"] = data
        scope = _hash(_bytes([workspace_id, kind, program_id, snapshot["version"]]))[
            :16
        ]
        run_id = f"{kind}-{program_id}-{scope}-{uuid4().hex}"
        configuration["run_id"] = run_id
        configuration.update(
            {
                "shell": "study" if kind == "study" else "backtest",
                "strategy_instances": [
                    {
                        "instance_id": program_id,
                        "implementation": "jit",
                        "bundle_hash": _hash(source.encode()),
                        "capital_fraction": "1.0",
                        "parameters": parameters,
                        "symbols": symbols,
                    }
                ],
            }
        )
        receipt = self._receipt(workspace_id, kind, program_id, head)
        tool = "run_study" if kind == "study" else "run_backtest"
        try:
            result = self._call(tool, {"config": configuration})
        except HarnessMcpError as error:
            result = McpToolResult(
                tool=tool,
                is_error=True,
                structured={
                    "error": "Harness program request failed.",
                    "executionCertainty": getattr(
                        error, "execution_certainty", "unknown_outcome"
                    ),
                },
            )
        receipt.update(
            {
                "ok": not result.is_error,
                "submittedRunId": run_id,
                "result": {
                    "run_id": run_id,
                    **{
                        key: result.structured[key]
                        for key in (
                            "job_id",
                            "run_id",
                            "study_id",
                            "state",
                            "status",
                            "metrics",
                            "artifacts",
                        )
                        if key in result.structured
                    },
                },
                **self._outcome(result),
            }
        )
        return receipt

    def _serving_sdk(self) -> dict[str, object]:
        """Read fresh SDK/compiler metadata through the public harness ports."""
        if self.harness is None:
            raise ProgramArtifactError("harness is not configured")
        try:
            read_info = getattr(self.harness, "current_server_info", None)
            metadata = read_info() if callable(read_info) else {}
            if isinstance(metadata, Mapping):
                metadata = metadata.get("serverInfo", metadata)
            if not isinstance(metadata, Mapping) or "sdkIdentity" not in metadata:
                result = self.harness.read_sdk_reference()
                if result.is_error:
                    raise ProgramArtifactError("serving SDK metadata is unavailable")
                metadata = result.structured
        except (HarnessMcpError, OSError, AttributeError) as error:
            raise ProgramArtifactError("serving SDK metadata is unavailable") from error
        if (
            not isinstance(metadata, Mapping)
            or not isinstance(metadata.get("sdkIdentity"), str)
            or re.fullmatch(r"[0-9a-f]{64}", cast(str, metadata["sdkIdentity"])) is None
            or not isinstance(metadata.get("sdkVersion"), str)
            or type(metadata.get("sdkAbiVersion")) is not int
        ):
            raise ProgramArtifactError("serving SDK metadata is invalid")
        result = {
            key: metadata[key]
            for key in (
                "sdkIdentity",
                "sdkVersion",
                "sdkAbiVersion",
                "validationIdentity",
            )
            if key in metadata
        }
        compiler = result.get("validationIdentity")
        if not isinstance(compiler, str) or not compiler:
            result["validationIdentity"] = None
        return result

    def _matched_sdk(self) -> dict[str, object]:
        """Fence local guidance and execution against the actual serving SDK."""
        identity = self._sdk_identity()
        version, abi = self._sdk_metadata(self.sdk_root)
        if self.harness is None:
            return {
                "sdkIdentity": identity,
                "sdkVersion": version,
                "sdkAbiVersion": abi,
            }
        serving = self._serving_sdk()
        if (
            serving["sdkIdentity"],
            serving["sdkVersion"],
            serving["sdkAbiVersion"],
        ) != (identity, version, abi):
            raise ProgramArtifactError("local and serving SDK identities do not match")
        return serving

    def read_sdk_reference(
        self,
        topic: str,
        *,
        start_line: int = 1,
        end_line: int | None = None,
        max_chars: int = MAX_READ_CHARS,
    ) -> dict[str, object]:
        if topic in PROGRAM_TEMPLATES:
            metadata = self._matched_sdk()
            text = PROGRAM_TEMPLATES[topic]
        elif topic in _SDK_TOPICS:
            if self.harness is not None:
                metadata = self._serving_sdk()
                # Validate bounds locally, but preserve the serving source range
                # rather than substituting packaged reference text.
                if (
                    type(start_line) is not int
                    or start_line < 1
                    or type(max_chars) is not int
                    or not 1 <= max_chars <= MAX_READ_CHARS
                ):
                    raise ProgramArtifactError("invalid SDK read range")
                if end_line is not None and (
                    type(end_line) is not int
                    or end_line < start_line
                    or end_line - start_line >= 400
                ):
                    raise ProgramArtifactError("invalid SDK read range")
                arguments: dict[str, object] = {
                    "topic": topic,
                    "start_line": start_line,
                    "max_chars": max_chars,
                }
                if end_line is not None:
                    arguments["end_line"] = end_line
                try:
                    result = self.harness.read_sdk_reference(**arguments)
                except (HarnessMcpError, OSError) as error:
                    raise ProgramArtifactError(
                        "serving SDK reference is unavailable"
                    ) from error
                document = result.structured
                if result.is_error or any(
                    document.get(key) != metadata[key]
                    for key in ("sdkIdentity", "sdkVersion", "sdkAbiVersion")
                ):
                    raise ProgramArtifactError(
                        "serving SDK reference is unavailable or changed"
                    )
                content = document.get("content")
                if not isinstance(content, str):
                    raise ProgramArtifactError("serving SDK reference is invalid")
                return {
                    **metadata,
                    "topic": topic,
                    "source": content[:max_chars],
                    **{
                        key: document[key]
                        for key in ("startLine", "endLine", "totalLines", "truncated")
                        if key in document
                    },
                    "truncated": len(content) > max_chars
                    or document.get("truncated") is True,
                    "totalChars": len(content),
                }
            metadata = self._matched_sdk()
            text = (self.sdk_root.parent / _SDK_TOPICS[topic]).read_text(
                encoding="utf-8"
            )
        else:
            raise ProgramArtifactError("unknown SDK reference topic")
        return {
            "topic": topic,
            **metadata,
            **self._range(text, start_line, end_line, max_chars),
        }

    def template_source(
        self, kind: ProgramKind, configuration: object, *, template: str = "basic"
    ) -> str:
        """Render the version-matched basic consumer for the exact declared inputs."""
        self._matched_sdk()
        if kind not in PROGRAM_TEMPLATES or template != "basic":
            raise ProgramArtifactError("unknown program template; use basic")
        self._check_configuration(configuration)
        subscriptions = cast(
            list[dict[str, object]],
            cast(Mapping[str, object], configuration)["subscriptions"],
        )
        declarations = []
        for item in subscriptions:
            variant = {"bars": "Bars", "quote": "Quote", "trade": "Trade"}[
                cast(str, item["data_type"])
            ]
            symbols = ", ".join(
                json.dumps(symbol) + ".into()"
                for symbol in cast(list[str], item["symbols"])
            )
            interval = (
                f", interval: {json.dumps(item['interval'])}.into()"
                if variant == "Bars"
                else ""
            )
            declarations.append(
                f"Subscription::{variant} {{ symbols: vec![{symbols}]{interval} }}"
            )
        source = PROGRAM_TEMPLATES[kind].replace(
            'vec![Subscription::Bars { symbols: vec!["SPY".into()], interval: "1min".into() }]',
            "vec![" + ", ".join(declarations) + "]",
        )
        if kind == "indicator":
            source = source.replace(
                'frame.bars_by_symbol.get("SPY")',
                "frame.bars_by_symbol.values().next()",
            )
        return source

    def _sdk_identity(self) -> str:
        try:
            metadata = self._sdk_metadata(self.sdk_root)
            canonical = Path(__file__).resolve().parents[2] / "harness" / "strategy-sdk"
            if metadata != self._sdk_metadata(canonical):
                raise ProgramArtifactError(
                    "SDK reference/templates do not match the packaged SDK"
                )
            sources = {}
            for name, crate in (
                ("strategy-sdk", self.sdk_root),
                ("contracts", self.sdk_root.parent / "contracts"),
                ("num", self.sdk_root.parent / "num"),
            ):
                sources[f"{name}/Cargo.toml"] = _hash(
                    (crate / "Cargo.toml").read_bytes()
                )
                for path in sorted((crate / "src").glob("*.rs")):
                    sources[f"{name}/src/{path.name}"] = _hash(path.read_bytes())
        except ProgramArtifactError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ProgramArtifactError(
                "SDK reference sources are unavailable; deploy the matching harness-strategy-sdk sources and supply sdk_root. Remote harness access does not supply local SDK references."
            ) from error
        return _hash(_bytes(sources))

    @staticmethod
    def _sdk_metadata(root: Path) -> tuple[str, int]:
        package = tomllib.loads((root / "Cargo.toml").read_text(encoding="utf-8"))[
            "package"
        ]
        abi = (root / "src" / "abi.rs").read_text(encoding="utf-8")
        matched = re.search(r"\bSDK_ABI_VERSION\s*:\s*u32\s*=\s*(\d+)\s*;", abi)
        if not isinstance(package["version"], str) or matched is None:
            raise ValueError("SDK version metadata is malformed")
        return package["version"], int(matched[1])

    def _snapshot(
        self,
        workspace_id: str,
        kind: ProgramKind,
        program_id: str,
        commit: str,
        git: GitCoordinator | None = None,
        fetched: Mapping[str, bytes] | None = None,
    ) -> dict[str, object]:
        if git is None:
            git = GitCoordinator(self.repositories.open(workspace_id).root)
        dependencies: dict[str, str] = {}
        files: dict[str, str] = {}
        sizes: dict[str, int] = {}
        visiting: set[str] = set()

        def load(
            selected_kind: ProgramKind, selected_id: str
        ) -> tuple[str, dict[str, object]]:
            relative = self._path(
                workspace_id, selected_kind, selected_id, git.repository
            )
            if relative in visiting:
                raise ProgramArtifactError("indicator dependency cycle")
            visiting.add(relative)
            source_key = f"{relative}/program.rs"
            manifest_key = f"{relative}/manifest.json"
            if (
                fetched is not None
                and source_key in fetched
                and manifest_key in fetched
            ):
                source_bytes = fetched[source_key]
                manifest_bytes = fetched[manifest_key]
            else:
                try:
                    revision = git.read_blobs(commit, [source_key, manifest_key])
                except GitPublicationError as error:
                    raise ProgramArtifactError(
                        "committed program does not exist"
                    ) from error
                source_bytes = revision[source_key]
                manifest_bytes = revision[manifest_key]
            try:
                manifest = json.loads(manifest_bytes)
            except ValueError as error:
                raise ProgramArtifactError(
                    "committed program manifest is malformed"
                ) from error
            if (
                not isinstance(manifest, dict)
                or manifest.get("kind") != selected_kind
                or manifest.get("id") != selected_id
            ):
                raise ProgramArtifactError("committed program identity mismatch")
            files[f"{relative}/program.rs"] = _hash(source_bytes)
            files[f"{relative}/manifest.json"] = _hash(manifest_bytes)
            sizes[f"{relative}/program.rs"] = len(source_bytes)
            sizes[f"{relative}/manifest.json"] = len(manifest_bytes)
            self._check_configuration(manifest.get("configuration"))
            declared = manifest.get("indicatorIds")
            if not isinstance(declared, list) or any(
                not isinstance(item, str) for item in declared
            ):
                raise ProgramArtifactError(
                    "committed indicator dependencies are malformed"
                )
            for dependency in declared:
                dep_source, _ = load("indicator", dependency)
                dependencies[dependency] = dep_source
            visiting.remove(relative)
            return source_bytes.decode(), manifest

        source, manifest = load(kind, program_id)
        return {
            "source": source,
            "manifest": manifest,
            "dependencies": dependencies,
            "files": files,
            "sizes": sizes,
            "version": _hash(_bytes(files)),
        }

    def _receipt(
        self, workspace_id: str, kind: ProgramKind, program_id: str, commit: str
    ) -> dict[str, object]:
        snapshot = self._snapshot(workspace_id, kind, program_id, commit)
        return {
            "workspaceId": workspace_id,
            "kind": kind,
            "programId": program_id,
            "artifactPath": self._path(workspace_id, kind, program_id),
            "version": snapshot["version"],
            "sourceSha256": _hash(cast(str, snapshot["source"]).encode()),
            "bundleSha256": snapshot["version"],
            "gitCommit": commit,
            "files": snapshot["files"],
        }

    @staticmethod
    def _receipt_entry(
        kind: ProgramKind,
        program_id: str,
        commit: str,
        snapshot: Mapping[str, object],
    ) -> dict[str, object]:
        manifest = cast(dict[str, object], snapshot["manifest"])
        version = cast(str, snapshot["version"])
        entry: dict[str, object] = {
            "programId": program_id,
            "kind": kind,
            "artifactPath": f"{_DIRECTORIES[kind]}/{program_id}",
            "version": version,
            "sourceSha256": _hash(cast(str, snapshot["source"]).encode()),
            "bundleSha256": version,
            "gitCommit": commit,
        }
        entry.update(WorkspacePrograms._summary(manifest))
        return entry

    @staticmethod
    def _summary(manifest: Mapping[str, object]) -> dict[str, object]:
        configuration = manifest.get("configuration")
        configuration = configuration if isinstance(configuration, Mapping) else {}
        symbols: set[str] = set()
        intervals: set[str] = set()
        subscriptions = configuration.get("subscriptions")
        if isinstance(subscriptions, list):
            for item in subscriptions:
                if isinstance(item, Mapping):
                    declared = item.get("symbols")
                    if isinstance(declared, list):
                        symbols.update(
                            symbol for symbol in declared if isinstance(symbol, str)
                        )
                    interval = item.get("interval")
                    if item.get("data_type") == "bars" and isinstance(interval, str):
                        intervals.add(interval)
        dependencies = manifest.get("indicatorIds")
        return {
            "interval": next(iter(intervals)) if len(intervals) == 1 else None,
            "symbols": sorted(symbols),
            "indicatorIds": (
                [item for item in dependencies if isinstance(item, str)]
                if isinstance(dependencies, list)
                else []
            ),
        }

    def _check_manifest(
        self, workspace_id: str, manifest: Mapping[str, object]
    ) -> None:
        dependencies = manifest["indicatorIds"]
        if not isinstance(dependencies, list) or any(
            not isinstance(item, str) for item in dependencies
        ):
            raise ProgramArtifactError("indicator_ids must be identifiers")
        if len(set(dependencies)) != len(dependencies):
            raise ProgramArtifactError("indicator_ids must be unique")
        for dependency in dependencies:
            self._path(workspace_id, "indicator", dependency)
            if manifest["kind"] == "indicator" and dependency == manifest["id"]:
                raise ProgramArtifactError("indicator cannot depend on itself")
            git = GitCoordinator(self.repositories.open(workspace_id).root)
            snapshot = self._snapshot(
                workspace_id, "indicator", dependency, self._head(git)
            )
            target = self._path(
                workspace_id, cast(str, manifest["kind"]), cast(str, manifest["id"])
            )
            if f"{target}/manifest.json" in cast(dict[str, str], snapshot["files"]):
                raise ProgramArtifactError("indicator dependency cycle")
        if not isinstance(manifest["indicatorType"], str) or (
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", manifest["indicatorType"]) is None
        ):
            raise ProgramArtifactError("indicator_type must be a Rust type identifier")
        _bytes(manifest)

        def check_configuration(value: object) -> None:
            if isinstance(value, Mapping):
                if any(
                    key in value
                    for key in (
                        "source",
                        "source_path",
                        "config_path",
                        "workspace",
                        "strategy_instances",
                        "parameters",
                    )
                ):
                    raise ProgramArtifactError(
                        "configuration cannot override committed program source or paths"
                    )
                if any(
                    isinstance(key, str)
                    and (key.endswith("_path") or key.endswith("Path"))
                    for key in value
                ):
                    raise ProgramArtifactError(
                        "configuration cannot override committed program source or paths"
                    )
                for item in value.values():
                    check_configuration(item)
            elif isinstance(value, list):
                for item in value:
                    check_configuration(item)

        check_configuration(manifest["configuration"])
        self._check_configuration(manifest["configuration"])

    @staticmethod
    def _check_configuration(value: object) -> None:
        if not isinstance(value, Mapping):
            raise ProgramArtifactError("configuration must be an object")
        if any(not isinstance(key, str) for key in value):
            raise ProgramArtifactError("configuration must have string keys")
        _bytes(value)
        dates = []
        for key in ("start", "end"):
            bound = value.get(key)
            if not isinstance(bound, str) or re.fullmatch(_DATE_PATTERN, bound) is None:
                raise ProgramArtifactError(
                    f"configuration.{key} requires an RFC 3339 date with UTC offset"
                )
            try:
                dates.append(datetime.fromisoformat(bound.replace("Z", "+00:00")))
            except ValueError as error:
                raise ProgramArtifactError(
                    f"configuration.{key} is not a valid date"
                ) from error
        if dates[0] >= dates[1]:
            raise ProgramArtifactError("configuration.start must be before end")
        try:
            validate_market_subscriptions(value.get("subscriptions"))
            validate_market_configuration(value)
        except ResearchModelError as error:
            raise ProgramArtifactError(str(error)) from error

    @staticmethod
    def _source_bytes(source: str) -> bytes:
        if (
            not isinstance(source, str)
            or not source.strip()
            or len(source.encode()) > 1_048_576
        ):
            raise ProgramArtifactError("source must be nonempty UTF-8, at most 1 MiB")
        return source.encode()

    @staticmethod
    def _head(git: GitCoordinator) -> str:
        head = git.head()
        if head is None:
            raise ProgramArtifactError("workspace has no committed artifacts")
        return head

    @staticmethod
    def _range(
        text: str,
        start_line: int,
        end_line: int | None,
        max_chars: int = MAX_READ_CHARS,
    ) -> dict[str, object]:
        lines = text.splitlines(keepends=True)
        end = min(start_line + 199, len(lines)) if end_line is None else end_line
        if (
            isinstance(start_line, bool)
            or isinstance(end, bool)
            or start_line < 1
            or end < start_line
            or end - start_line >= 400
            or start_line > len(lines)
            or isinstance(max_chars, bool)
            or not 1 <= max_chars <= MAX_READ_CHARS
        ):
            raise ProgramArtifactError("read range must select 1 to 400 existing lines")
        end = min(end, len(lines))
        selected = "".join(lines[start_line - 1 : end])
        return {
            "source": selected[:max_chars],
            "startLine": start_line,
            "endLine": end,
            "totalLines": len(lines),
            "truncated": len(selected) > max_chars,
            "totalChars": len(selected),
        }

    @staticmethod
    def _assembled_source(snapshot: Mapping[str, object], kind: ProgramKind) -> str:
        modules = "".join(
            f"mod r#{name} {{\n{source}\n}}\n"
            for name, source in sorted(
                cast(dict[str, str], snapshot["dependencies"]).items()
            )
        )
        source = cast(str, snapshot["source"])
        if kind != "indicator":
            return (modules + source).replace("\r\n", "\n")
        indicator_type = cast(dict[str, object], snapshot["manifest"])["indicatorType"]
        return (
            modules
            + f"mod selected_indicator {{\n{source}\n}}\n"
            + f"""
use harness_strategy_sdk::{{export_study_program, Indicator, MarketConsumer,
    ObservationContext, StudyProgram, Subscription}};
use harness_strategy_sdk::serde_json::Value;
struct IndicatorValidation {{ indicator: selected_indicator::{indicator_type} }}
impl MarketConsumer for IndicatorValidation {{
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> {{ Vec::new() }}
    fn indicators(&mut self) -> Vec<&mut dyn Indicator> {{ vec![&mut self.indicator] }}
}}
impl StudyProgram for IndicatorValidation {{
    fn new(_: Value) -> Self {{ Self {{ indicator: Default::default() }} }}
}}
export_study_program!(IndicatorValidation);
"""
        ).replace("\r\n", "\n")

    def _call(self, name: str, arguments: Mapping[str, object]) -> McpToolResult:
        if self.harness is None:
            raise ProgramArtifactError("harness is not configured")
        result = self.harness.call_tool(name, arguments)
        structured = dict(result.structured)
        # Both public injection boundaries share this result surface. Provider
        # failures additionally carry certainty; preserve it without extracting
        # the provider's client or composing a second execution path.
        failure = getattr(result, "failure", None)
        if failure is not None:
            structured.setdefault("executionCertainty", failure.execution_certainty)
            structured.setdefault("retryGuidance", failure.retry_guidance)
            structured.setdefault("error", failure.message)
        return McpToolResult(
            tool=name, is_error=result.is_error, structured=structured, text=result.text
        )

    @staticmethod
    def _outcome(
        result: McpToolResult, *, validation: bool = False
    ) -> dict[str, object]:
        # Rustc's rendered output contains source excerpts. Keep only bounded,
        # actionable fields rather than echoing authored programs in receipts.
        raw = result.structured.get("diagnostics", [])
        diagnostics = []

        def compact(item: Mapping[str, object], depth: int = 0) -> dict[str, object]:
            detail: dict[str, object] = {
                key: str(item[key])[:2000]
                for key in ("message", "level")
                if key in item
            }
            code = item.get("code")
            if isinstance(code, Mapping):
                code = code.get("code")
            if isinstance(code, str):
                detail["code"] = code[:100]
            spans = item.get("spans")
            if isinstance(spans, list):
                detail["spans"] = [
                    {
                        key: value[:2000] if isinstance(value, str) else value
                        for key, value in span.items()
                        if key
                        in (
                            "file_name",
                            "line_start",
                            "line_end",
                            "column_start",
                            "column_end",
                            "is_primary",
                            "label",
                            "suggested_replacement",
                            "suggestion_applicability",
                        )
                        and isinstance(value, (str, int, bool))
                    }
                    for span in spans[:20]
                    if isinstance(span, Mapping)
                ]
            children = item.get("children")
            if depth < 2 and isinstance(children, list):
                detail["children"] = [
                    compact(child, depth + 1)
                    for child in children[:10]
                    if isinstance(child, Mapping)
                ]
            return detail

        if isinstance(raw, list):
            diagnostics = [
                compact(item) for item in raw[:20] if isinstance(item, Mapping)
            ]
        rejected = result.is_error or (
            validation and result.structured.get("valid") is not True
        )
        message = ""
        if rejected and not diagnostics:
            message = str(
                result.structured.get("error")
                or result.structured.get("message")
                or result.text
                or "Harness did not return an explicit valid program result."
            ).split("\n", 1)[0][:2000]
            diagnostics = [{"message": message, "level": "error"}]
        certainty = result.structured.get(
            "executionCertainty", "observed_failure" if rejected else "observed_success"
        )
        return {
            "diagnostics": diagnostics,
            "message": message,
            "executionCertainty": certainty,
            "retryGuidance": result.structured.get("retryGuidance")
            or (
                "Fix the committed source/configuration and validate again."
                if validation
                else "Inspect job/run state before an explicit retry; never automatically replay execution."
            ),
        }

    def _commit(
        self,
        workspace_id: str,
        git: GitCoordinator,
        head: str | None,
        files: Mapping[str, bytes],
        message: str,
    ) -> str:
        root = self.repositories.open(workspace_id).root
        destinations = {}
        for relative in files:
            path = self.repositories.resolve_path(workspace_id, relative)
            current = root
            for part in relative.split("/"):
                current = current / part
                if current.is_symlink():
                    raise ProgramArtifactError(
                        "artifact paths must not contain symlinks"
                    )
            if path.exists():
                if head is None or not git.file_matches(
                    head, relative, path.read_bytes()
                ):
                    raise ProgramArtifactError(
                        "artifact has uncommitted changes; refusing to overwrite"
                    )
            destinations[relative] = path
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith(("GIT_", "GCM_"))
        }
        environment.update(
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": "FCA Thesis",
                "GIT_AUTHOR_EMAIL": "thesis@localhost",
                "GIT_COMMITTER_NAME": "FCA Thesis",
                "GIT_COMMITTER_EMAIL": "thesis@localhost",
                "GIT_LITERAL_PATHSPECS": "1",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )

        def run(*args: str, payload: bytes | None = None) -> str:
            try:
                result = subprocess.run(
                    ["git", *args],
                    cwd=root,
                    env=environment,
                    input=payload,
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise GitPublicationError(
                    "program Git publication outcome is unknown"
                ) from error
            if result.returncode:
                raise GitPublicationError("program Git publication failed")
            return result.stdout.decode().strip()

        for relative in files:
            if run("diff", "--cached", "--name-only", "--", relative):
                raise ProgramArtifactError(
                    "artifact has staged changes; refusing to overwrite"
                )

        git_dir = Path(run("rev-parse", "--absolute-git-dir"))
        with tempfile.TemporaryDirectory(
            prefix="program-index-", dir=git_dir
        ) as temporary:
            environment["GIT_INDEX_FILE"] = str(Path(temporary) / "index")
            run("read-tree", head or "--empty")
            blobs = {}
            for relative, payload in files.items():
                blob = run("hash-object", "-w", "--stdin", payload=payload)
                blobs[relative] = blob
                run("update-index", "--add", "--cacheinfo", "100644", blob, relative)
            tree = run("write-tree")
            commit = run(
                "commit-tree",
                tree,
                *(["-p", head] if head else []),
                "-m",
                "thesis: " + message,
            )
            run("update-ref", "HEAD", commit, head or "0" * len(commit))
            environment.pop("GIT_INDEX_FILE")
            try:
                for relative, path in destinations.items():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    descriptor, name = tempfile.mkstemp(
                        prefix=".program-", dir=path.parent
                    )
                    staged = Path(name)
                    try:
                        with os.fdopen(descriptor, "wb") as stream:
                            stream.write(files[relative])
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(staged, path)
                    finally:
                        staged.unlink(missing_ok=True)
                run(
                    "update-index",
                    "--index-info",
                    payload="".join(
                        f"100644 {blobs[relative]}\t{relative}\n" for relative in files
                    ).encode(),
                )
            except (OSError, GitPublicationError) as error:
                parts = next(iter(files)).split("/")
                kind = cast(
                    ProgramKind,
                    next(
                        key for key, value in _DIRECTORIES.items() if value == parts[0]
                    ),
                )
                raise ProgramPublicationError(
                    self._receipt(workspace_id, kind, parts[1], commit)
                ) from error
        # Publication failure must retain the known local commit for explicit
        # reconciliation, not report a mutation as if nothing happened.
        parts = next(iter(files)).split("/")
        kind = cast(
            ProgramKind,
            next(key for key, value in _DIRECTORIES.items() if value == parts[0]),
        )
        receipt = self._receipt(workspace_id, kind, parts[1], commit)
        try:
            self.repositories.publish(workspace_id)
        except Exception as error:
            raise ProgramPublicationError(receipt) from error
        return commit


__all__ = [
    "ProgramArtifactError",
    "ProgramPublicationError",
    "ProgramHarness",
    "ProgramHarnessResult",
    "ProgramKind",
    "WorkspacePrograms",
    "PROGRAM_TEMPLATES",
    "PROGRAM_ID_PATTERN",
    "PROGRAM_CONFIGURATION_SCHEMA",
    "MAX_READ_CHARS",
    "MAX_LIST_RESULTS",
]
