"""Workspace backtest discovery from retained submissions and operation references.

The harness owns execution state. Reports are optional projections and never
determine membership in this catalog. Reads cannot submit, retry or replay work.
Terminal observations are cached because an accepted execution is immutable.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import RLock

from thesis.integrations.mcp import HarnessMcpError
from thesis.persistence.submissions import SubmissionStore
from thesis_runtime.contracts.records import ToolJob

JobReader = Callable[[str], Mapping[str, object]]
ArtifactReader = Callable[[str, str], bytes]
TrackingReader = Callable[[str], tuple[ToolJob, ...]]
SummaryReader = Callable[[str], tuple[dict[str, object], ...]]
_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


class BacktestCatalog:
    """Observational, workspace-scoped index over existing execution identities."""

    def __init__(
        self,
        *,
        submissions: SubmissionStore | None,
        tracking: TrackingReader | None,
        reports: SummaryReader,
        read_job: JobReader | None,
        read_artifact: ArtifactReader | None = None,
        list_artifacts: JobReader | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._submissions = submissions
        self._tracking = tracking
        self._reports = reports
        self._read_job = read_job
        self._read_artifact = read_artifact
        self._list_artifacts = list_artifacts
        self._clock = clock or (lambda: datetime.now(UTC))
        self._observations: OrderedDict[str, dict[str, object]] = OrderedDict()
        self._guard = RLock()

    def _references(self, workspace_id: str) -> dict[str, dict[str, object]]:
        references: dict[str, dict[str, object]] = {}
        for report in self._reports(workspace_id):
            references[str(report["job_id"])] = dict(report)
        if self._submissions is not None:
            for submission in self._submissions.list(workspace_id):
                if submission["operation"] != "run_backtest":
                    continue
                inputs = _mapping(submission["inputs"])
                attribution = _mapping(inputs.get("attribution"))
                job_id = str(submission["job_id"])
                references[job_id] = {
                    **references.get(job_id, {}),
                    "job_id": job_id,
                    "created_at": submission["retained_at"],
                    "name": inputs.get("name"),
                    "accepted_input": inputs.get("config"),
                    "attribution": dict(attribution),
                    "declared_strategy_id": inputs.get("strategy_id"),
                    "strategy_id": attribution.get("programId")
                    or inputs.get("strategy_id")
                    or references.get(job_id, {}).get("strategy_id"),
                }
        if self._tracking is not None:
            for job in self._tracking(workspace_id):
                if job.tool_name != "run_backtest":
                    continue
                references[job.job_id] = {
                    **references.get(job.job_id, {}),
                    "job_id": job.job_id,
                    "created_at": job.created_at.isoformat(),
                    "chat_id": job.chat_id,
                    "execution_id": job.execution_id,
                    "attribution": {
                        **_mapping(references.get(job.job_id, {}).get("attribution")),
                        **dict(job.attribution),
                    },
                    "strategy_id": job.attribution.get("programId")
                    or references.get(job.job_id, {}).get("strategy_id"),
                }
        return references

    @staticmethod
    def _program_ids(reference: Mapping[str, object]) -> set[str]:
        # Instance names belong to an execution and are not artifact identities.
        identity = _text(
            _mapping(reference.get("attribution")).get("programId")
        ) or _text(reference.get("declared_strategy_id"))
        return {identity} if identity else set()

    def strategy_counts(self, workspace_id: str) -> dict[str, int]:
        """Count explicit retained program links without polling external jobs."""
        counts: dict[str, int] = {}
        for reference in self._references(workspace_id).values():
            for identity in self._program_ids(reference):
                counts[identity] = counts.get(identity, 0) + 1
        return counts

    def count(self, workspace_id: str) -> int:
        """Count retained executions without requiring external status reads."""
        return len(self._references(workspace_id))

    def page(
        self,
        workspace_id: str,
        *,
        offset: int = 0,
        limit: int = 50,
        strategy_id: str | None = None,
    ) -> dict[str, object]:
        references = self._references(workspace_id)
        eligible = (
            reference
            for reference in references.values()
            if strategy_id is None or strategy_id in self._program_ids(reference)
        )
        ordered = sorted(
            eligible, key=lambda row: str(row.get("created_at", "")), reverse=True
        )
        selected = ordered[offset : offset + limit]
        if selected:
            with ThreadPoolExecutor(max_workers=min(4, len(selected))) as pool:
                items = list(
                    pool.map(lambda row: self._observe(workspace_id, row), selected)
                )
        else:
            items = []
        return {"items": items, "total": len(ordered), "offset": offset, "limit": limit}

    def get(self, workspace_id: str, job_id: str) -> dict[str, object]:
        reference = self._references(workspace_id).get(job_id)
        if reference is None:
            raise KeyError(job_id)
        return self._observe(workspace_id, reference, include_input=True)

    def _observe(
        self,
        workspace_id: str,
        reference: Mapping[str, object],
        *,
        include_input: bool = False,
    ) -> dict[str, object]:
        job_id = str(reference["job_id"])
        key = workspace_id + ":" + job_id
        with self._guard:
            cached = self._observations.get(key)
        error: str | None = None
        observation: dict[str, object] = dict(cached or {})
        if cached is None or cached.get("state") not in _TERMINAL:
            if self._read_job is None:
                error = (
                    "Harness status is unavailable. The submission remains retained."
                )
            else:
                try:
                    observation = dict(self._read_job(job_id))
                    if observation.get("job_id") != job_id:
                        raise ValueError(
                            "job observation identity does not match retained submission"
                        )
                    if observation.get("kind") != "backtest":
                        raise ValueError(
                            "retained backtest identity resolved to another job kind"
                        )
                    observation["observed_at"] = self._clock().isoformat()
                    with self._guard:
                        self._observations[key] = dict(observation)
                        while len(self._observations) > 128:
                            self._observations.popitem(last=False)
                except (HarnessMcpError, ValueError, OSError) as cause:
                    error = str(cause)
                    observation = dict(cached or {})
        accepted = _mapping(
            observation.get("accepted_input") or reference.get("accepted_input")
        )
        instances = accepted.get("strategy_instances")
        programs: list[dict[str, object]] = []
        if isinstance(instances, Sequence) and not isinstance(instances, (str, bytes)):
            for instance in instances:
                if not isinstance(instance, Mapping):
                    continue
                parameters = _mapping(instance.get("parameters"))
                source = _text(parameters.get("source"))
                program = {
                    "strategy_id": reference.get("strategy_id")
                    if len(instances) == 1 and reference.get("strategy_id")
                    else instance.get("instance_id"),
                    "instance_id": instance.get("instance_id"),
                    "bundle_hash": instance.get("bundle_hash"),
                    "source_sha256": None
                    if source is None
                    else hashlib.sha256(source.encode()).hexdigest(),
                    "parameters": {
                        k: v
                        for k, v in parameters.items()
                        if k not in {"source", "source_path"}
                    },
                }
                if include_input:
                    program["source"] = source
                programs.append(program)
        progress = observation.get("progress")
        stages = (
            [dict(stage) for stage in progress if isinstance(stage, Mapping)]
            if isinstance(progress, list)
            else []
        )
        created_at = observation.get("created_at") or reference.get("created_at")
        created = _time(created_at)
        started = next(
            (
                _time(stage.get("started_at"))
                for stage in stages
                if stage.get("stage") == "compiling"
            ),
            None,
        )
        ended = (
            _time(observation.get("updated_at"))
            if observation.get("state") in _TERMINAL
            else self._clock()
        )
        metrics = _mapping(observation.get("metrics"))
        state = observation.get("state", "unknown")
        strategy_id = observation.get("strategy_id") or reference.get("strategy_id")
        if not strategy_id and programs:
            strategy_id = ",".join(str(program["strategy_id"]) for program in programs)
        row: dict[str, object] = {
            **{
                k: v
                for k, v in reference.items()
                if k not in {"accepted_input", "attribution", "declared_strategy_id"}
            },
            "workspace_id": workspace_id,
            "job_id": job_id,
            "name": reference.get("name") or strategy_id or job_id,
            "strategy_id": strategy_id or "",
            "programs": programs,
            "input_origin": "harness_accepted"
            if isinstance(observation.get("accepted_input"), Mapping)
            else "retained_submission"
            if isinstance(reference.get("accepted_input"), Mapping)
            else "unavailable",
            "state": state,
            "created_at": created_at,
            "updated_at": observation.get("updated_at"),
            "observed_at": observation.get("observed_at"),
            "status_error": error,
            "error_message": observation.get("error_message"),
            "period_start": accepted.get("start") or observation.get("data_start"),
            "period_end": accepted.get("end") or observation.get("data_end"),
            "progress": stages,
            "queue_seconds": None
            if created is None or started is None
            else max(0, (started - created).total_seconds()),
            "execution_seconds": None
            if started is None or ended is None
            else max(0, (ended - started).total_seconds()),
            "metrics": dict(metrics),
            "artifact_count": observation.get("artifact_count", 0),
            "total_bytes": observation.get("total_bytes", 0),
            "evidence": "no_trades"
            if state == "succeeded" and str(metrics.get("fill_count")) == "0"
            else "available"
            if state == "succeeded"
            else "incomplete",
        }
        if include_input:
            row["accepted_input"] = dict(_mapping(observation.get("accepted_input")))
            row["submitted_input"] = dict(_mapping(reference.get("accepted_input")))
            if state in _TERMINAL and self._list_artifacts is not None:
                try:
                    artifacts = self._list_artifacts(job_id)
                    if artifacts.get("job_id") != job_id:
                        raise ValueError(
                            "artifact manifest identity does not match retained execution"
                        )
                    row["artifacts"] = artifacts.get("artifacts", [])
                except (HarnessMcpError, ValueError, OSError) as cause:
                    row["evidence_error"] = str(cause)
            if state == "succeeded" and self._read_artifact is not None:
                try:
                    entries = row.get("artifacts", [])
                    if isinstance(entries, list):
                        for entry in entries:
                            if (
                                isinstance(entry, Mapping)
                                and entry.get("name") == "backtest.json"
                                and isinstance(entry.get("sizeBytes"), int)
                                and entry["sizeBytes"] > 8 * 1024 * 1024
                            ):
                                raise ValueError(
                                    "backtest evidence exceeds inspection limit; inspect its published artifact"
                                )
                    raw = self._read_artifact(job_id, "backtest.json")
                    if len(raw) > 8 * 1024 * 1024:
                        raise ValueError("backtest evidence exceeds inspection limit")
                    evidence = _mapping(json.loads(raw))
                    row["portfolio"] = evidence.get("portfolio")
                    row["custom_metrics"] = evidence.get("custom_metrics")
                    row["fills"] = evidence.get("fills")
                except (HarnessMcpError, ValueError, OSError) as cause:
                    row["evidence_error"] = str(cause)
        return row
