"""Git-backed evidence reports for Thesis workflow outcomes.

Phase 1 keeps this builder beside the workflow that produces it.  The record
shape is stable and portable so a later reports capability can move it to
``src/thesis/reports/evidence.py`` without changing serialized evidence.

Every report produced by this module is explicitly labeled as exploratory
Phase 1 history.  A profitable backtest is never validated evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from thesis.strategies.bundles import StrategyBundle

EVIDENCE_GRADE = "exploratory"
HISTORICAL_PHASE_LABEL = "phase-1-historical"
EXPLORATORY_DISCLOSURE = (
    "Phase 1 historical findings are exploratory and must not be treated as "
    "validated evidence."
)
_REPORT_SCHEMA = "fca-thesis-evidence-report-v1"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_WORKSPACE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class EvidenceReportError(ValueError):
    """Raised when an evidence report would be incomplete or mislabeled."""


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise EvidenceReportError(f"{label} must be a safe identifier")
    return value


def _digest(value: str, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise EvidenceReportError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _limitations(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, str):
        raise EvidenceReportError("limitations must be a sequence of strings")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise EvidenceReportError("limitations must contain non-empty strings")
        normalized.append(value.strip())
    if EXPLORATORY_DISCLOSURE not in normalized:
        normalized.append(EXPLORATORY_DISCLOSURE)
    return tuple(sorted(dict.fromkeys(normalized)))


@dataclass(frozen=True, slots=True)
class EvidenceReport:
    """The complete, labeled outcome record for one workflow execution."""

    workspace_id: str
    execution_id: str
    strategy_id: str
    strategy_version: str
    bundle_sha256: str
    evidence_grade: Literal["exploratory"] = EVIDENCE_GRADE
    historical: bool = True
    phase_label: str = HISTORICAL_PHASE_LABEL
    schema: str = _REPORT_SCHEMA
    job_id: str | None = None
    backtest_state: str | None = None
    harness_manifest_reference: str | None = None
    metrics: object | None = None
    artifact_references: tuple[Mapping[str, object], ...] = field(default_factory=tuple)
    git_commits: tuple[str, ...] = ()
    journal_id: str | None = None
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_id", _workspace_id(self.workspace_id))
        object.__setattr__(
            self, "execution_id", _identifier(self.execution_id, "execution_id")
        )
        object.__setattr__(
            self, "strategy_id", _identifier(self.strategy_id, "strategy_id")
        )
        object.__setattr__(
            self, "bundle_sha256", _digest(self.bundle_sha256, "bundle_sha256")
        )
        if self.evidence_grade != EVIDENCE_GRADE:
            raise EvidenceReportError(
                f"Phase 1 evidence must carry the {EVIDENCE_GRADE!r} grade"
            )
        if self.historical is not True or self.phase_label != HISTORICAL_PHASE_LABEL:
            raise EvidenceReportError("Phase 1 evidence must be labeled as history")
        if self.backtest_state is not None and (
            not isinstance(self.backtest_state, str) or not self.backtest_state.strip()
        ):
            raise EvidenceReportError("backtest_state must be non-empty when set")
        if self.metrics is not None and not isinstance(self.metrics, Mapping):
            raise EvidenceReportError("metrics must be a JSON-shaped mapping")
        object.__setattr__(
            self,
            "artifact_references",
            tuple(
                MappingProxyType(dict(reference))
                for reference in self.artifact_references
            ),
        )
        for commit in self.git_commits:
            if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
                raise EvidenceReportError("git_commits must contain Git object IDs")
        object.__setattr__(self, "limitations", _limitations(tuple(self.limitations)))

    def as_dict(self) -> dict[str, object]:
        """Return the portable report representation for transport and Git."""
        return {
            "artifactReferences": [
                dict(reference) for reference in self.artifact_references
            ],
            "backtestState": self.backtest_state,
            "bundleSha256": self.bundle_sha256,
            "evidenceGrade": self.evidence_grade,
            "executionId": self.execution_id,
            "gitCommits": list(self.git_commits),
            "harnessManifestReference": self.harness_manifest_reference,
            "historical": self.historical,
            "jobId": self.job_id,
            "journalId": self.journal_id,
            "limitations": list(self.limitations),
            "metrics": self.metrics,
            "phaseLabel": self.phase_label,
            "schema": self.schema,
            "strategyId": self.strategy_id,
            "strategyVersion": self.strategy_version,
            "workspaceId": self.workspace_id,
        }

    def to_bytes(self) -> bytes:
        """Return deterministic report bytes suitable for Git publication."""
        document = self.as_dict()
        return (
            json.dumps(
                document,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")

    @property
    def content_sha256(self) -> str:
        """Return the content digest of the serialized report."""
        return hashlib.sha256(self.to_bytes()).hexdigest()


def _workspace_id(value: str) -> str:
    if not isinstance(value, str) or _WORKSPACE_ID.fullmatch(value) is None:
        raise EvidenceReportError("workspace_id must be a safe workspace identifier")
    return value


def build_evidence_report(
    *,
    workspace_id: str,
    execution_id: str,
    bundle: StrategyBundle,
    backtest: Mapping[str, object] | None = None,
    import_receipt: Mapping[str, object] | None = None,
    journal_id: str | None = None,
    limitations: Sequence[str] = (),
) -> EvidenceReport:
    """Assemble the labeled report from recorded workflow phase outputs."""
    summary = {} if backtest is None else backtest
    job_id = summary.get("jobId")
    state = summary.get("state")
    manifest = import_receipt.get("harnessManifest") if import_receipt else None
    manifest_reference = None
    if isinstance(manifest, Mapping):
        identifier = manifest.get("id")
        digest = manifest.get("sha256")
        if isinstance(identifier, str) and isinstance(digest, str):
            manifest_reference = f"{identifier}@sha256:{digest}"
    artifact_references: tuple[Mapping[str, object], ...] = ()
    git_commits: list[str] = []
    if import_receipt is not None:
        raw_artifacts = import_receipt.get("artifacts")
        if isinstance(raw_artifacts, Sequence) and not isinstance(raw_artifacts, str):
            artifact_references = tuple(
                reference
                for reference in raw_artifacts
                if isinstance(reference, Mapping)
            )
        commit = import_receipt.get("gitCommit")
        if isinstance(commit, str):
            git_commits.append(commit)
        for reference in artifact_references:
            reference_commit = reference.get("gitCommit")
            if isinstance(reference_commit, str):
                git_commits.append(reference_commit)
    return EvidenceReport(
        workspace_id=workspace_id,
        execution_id=execution_id,
        strategy_id=bundle.strategy_id,
        strategy_version=bundle.version,
        bundle_sha256=bundle.bundle_sha256,
        job_id=job_id if isinstance(job_id, str) else None,
        backtest_state=state if isinstance(state, str) else None,
        harness_manifest_reference=manifest_reference,
        metrics=summary.get("metrics"),
        artifact_references=artifact_references,
        git_commits=tuple(git_commits),
        journal_id=journal_id,
        limitations=tuple(limitations) + tuple(bundle.limitations),
    )


__all__ = [
    "EVIDENCE_GRADE",
    "EXPLORATORY_DISCLOSURE",
    "EvidenceReport",
    "EvidenceReportError",
    "HISTORICAL_PHASE_LABEL",
    "build_evidence_report",
]
