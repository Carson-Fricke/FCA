"""Import one terminal harness screening run into a workspace record.

Mirrors :mod:`thesis.backtests.run_import`: the importer reads the run catalog
(``get_job_result``, whose ``state`` is the job state), the artifact index
(``list_artifacts``), and the harness ``screen.json`` artifact
(``read_artifact``) through the injected MCP tool provider, then builds a
:class:`ScreeningRecord`.  Unlike the backtest importer, a missing or unreadable
``screen.json`` fails closed: a screening result is exactly its artifact, so an
import without one is not a result.

Nothing is recomputed.  Rows and metrics are the harness's ranked output as
published; no row or statistic is derived from other artifacts, fills, equity,
or trades.  A metric a row did not publish stays absent, never zero.  An empty
row list is kept empty and says so through ``rows_reason``.

The artifact contract is frozen.  ``screen.json`` declares
``schema == SCREEN_SCHEMA`` and carries ``as_of`` -- there is no ``asOf``
spelling; its ``status`` is ``succeeded`` or ``failed``; rows are exactly
``{"symbol": <str>, "metrics": {<str>: <decimal string>}}`` with no ``rank``
key because row order is the rank; and ``job_id``, ``definition_id``,
``definition_hash``, and ``bundle_hash`` must echo the job and the definition
being imported.  A failed run surfaces its reason: the artifact ``error`` when
that artifact is readable for the job, else the job record's ``error_message``.

The tool provider contract is the typed surface of
:class:`~thesis.integrations.mcp.HarnessMcpClient`, so the service can pass a
real client (or any structurally equivalent fake) without an adapter.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from thesis.integrations.mcp import HarnessMcpError, McpToolResult
from thesis.screening.definitions import ScreenerDefinition

#: The harness result artifact a screening run is imported from.
SCREEN_ARTIFACT = "screen.json"

#: The frozen ``screen.json`` schema identifier.
SCREEN_SCHEMA = "fca-harness-screen-v2"

#: Job states that are still in flight; the status tool reports these while
#: polling.  A terminal artifact is never written with one of these states.
PENDING_JOB_STATES = frozenset({"queued", "running"})
#: Job states that ended without a usable result.
FAILED_JOB_STATES = frozenset({"failed", "cancelled"})
#: Job states that produced a result artifact.
COMPLETE_JOB_STATES = frozenset({"succeeded"})
#: The frozen ``screen.json`` artifact statuses that carry a usable result.
COMPLETE_ARTIFACT_STATES = frozenset({"succeeded"})
#: Artifact statuses that ended without a usable result.
FAILED_ARTIFACT_STATES = frozenset({"failed"})
#: The ``rows_reason`` of a succeeded artifact that published an empty row list.
NO_ROWS_REASON = "artifact_has_no_rows"

_RECORD_HASH_DOMAIN = b"fca:thesis-screening-result:v1\0"
_ROW_KEYS = frozenset({"symbol", "metrics"})


class ScreeningRunError(RuntimeError):
    """A harness screening run cannot be imported (missing, failed, unusable).

    ``job_id`` carries the retained execution identity when one was allocated,
    so a caller can inspect an unknown outcome instead of replaying it.
    """

    def __init__(self, message: str, *, job_id: str | None = None) -> None:
        super().__init__(message)
        self.job_id = job_id


class ScreeningRunPending(ScreeningRunError):
    """The harness job is still queued or running; the caller may retry."""


class ScreeningArtifactError(ScreeningRunError):
    """The harness ``screen.json`` artifact is missing or invalid."""


class ScreeningToolProvider(Protocol):
    """The MCP tool surface a screener run launches, polls, and imports over."""

    def run_screener(
        self,
        *,
        config: Mapping[str, object],
        workspace: str | None = None,
        job_id: str | None = None,
    ) -> McpToolResult: ...

    def get_job_status(self, *, job_id: str) -> McpToolResult: ...

    def get_job_result(self, *, job_id: str) -> McpToolResult: ...

    def list_artifacts(self, *, job_id: str) -> McpToolResult: ...

    def read_artifact(self, *, job_id: str, name: str) -> McpToolResult: ...


@dataclass(frozen=True, slots=True)
class ScreenRow:
    """One ranked screening row: a symbol and the metrics it published.

    The row's rank is its position in the artifact's ``rows`` order; the frozen
    artifact carries no rank key, and no rank key is accepted on import.  A
    metric the row did not publish has no key in ``metrics``; it is never filled
    in with a zero.
    """

    symbol: str
    metrics: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _text(self.symbol, "row symbol"))
        metrics = _metrics(self.metrics)
        object.__setattr__(self, "metrics", MappingProxyType(metrics))

    def document(self) -> dict[str, object]:
        """Return the portable row representation."""
        return {
            "symbol": self.symbol,
            "metrics": dict(self.metrics),
        }


@dataclass(frozen=True, slots=True)
class ScreeningRecord:
    """One imported screening result with its definition and run provenance.

    ``rows_reason`` is ``None`` when ``rows`` is non-empty and
    :data:`NO_ROWS_REASON` when the artifact published an empty row list, so an
    empty list always says why it is empty.  Every field is required: nothing
    stands in for a value the import did not read.
    """

    result_id: str
    workspace_id: str
    definition_id: str
    definition_hash: str
    job_id: str
    status: str
    as_of: str
    rows: tuple[ScreenRow, ...]
    rows_reason: str | None
    provenance: Mapping[str, object]

    def __post_init__(self) -> None:
        for name in ("result_id", "workspace_id", "definition_id", "job_id"):
            object.__setattr__(
                self, name, _text(getattr(self, name), name.replace("_", " "))
            )
        object.__setattr__(
            self, "definition_hash", _text(self.definition_hash, "definition hash")
        )
        object.__setattr__(self, "status", _text(self.status, "status"))
        object.__setattr__(self, "as_of", _text(self.as_of, "as_of"))
        rows = tuple(self.rows)
        if not all(isinstance(row, ScreenRow) for row in rows):
            raise ScreeningArtifactError("record rows must be ScreenRow values")
        expected_reason = None if rows else NO_ROWS_REASON
        if self.rows_reason != expected_reason:
            raise ScreeningArtifactError(
                "record rows_reason must be None for published rows and "
                f"{NO_ROWS_REASON!r} for an empty row list"
            )
        object.__setattr__(self, "rows", rows)
        object.__setattr__(
            self,
            "provenance",
            MappingProxyType(dict(self.provenance)),
        )

    def document(self) -> dict[str, object]:
        """Return the portable JSON document the HTTP surface serves."""
        return {
            "as_of": self.as_of,
            "definition_hash": self.definition_hash,
            "definition_id": self.definition_id,
            "provenance": dict(self.provenance),
            "result_id": self.result_id,
            "rows": [row.document() for row in self.rows],
            "rows_reason": self.rows_reason,
            "job_id": self.job_id,
            "status": self.status,
            "workspace_id": self.workspace_id,
        }


def screening_result_id(document: Mapping[str, object]) -> str:
    """Return the content address of one screening record document."""
    try:
        canonical = json.dumps(
            dict(document),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ScreeningArtifactError(
            "screening record is not JSON-canonical"
        ) from error
    digest = hashlib.sha256(_RECORD_HASH_DOMAIN + canonical).hexdigest()
    return f"sha256:{digest}"


def import_screening_run(
    *,
    tools: ScreeningToolProvider,
    workspace_id: str,
    definition: ScreenerDefinition,
    job_id: str,
    as_of: str,
) -> ScreeningRecord:
    """Import one completed harness screening run as a workspace record.

    Raises :class:`ScreeningRunPending` while the run catalog reports an
    in-flight status, :class:`ScreeningRunError` when the run ended without a
    usable result, and :class:`ScreeningArtifactError` when the ``screen.json``
    artifact is missing, unreadable, not the frozen schema, or not produced for
    this job.  There is no partial success: the record is built only from the
    terminal run and its artifact, and nothing is recomputed from it.
    """
    resolved_workspace = _required_text(workspace_id, "workspace id")
    resolved_job = _required_text(job_id, "job id")
    _required_text(as_of, "as_of")
    catalog = _tool_document(
        tools.get_job_result(job_id=resolved_job), f"get_job_result({resolved_job!r})"
    )
    state = _optional_text(catalog.get("state"))
    if state is None:
        raise ScreeningRunError(f"harness job {resolved_job!r} reported no state")
    if state in PENDING_JOB_STATES:
        raise ScreeningRunPending(f"harness job {resolved_job!r} is still {state}")
    if state in FAILED_JOB_STATES:
        raise ScreeningRunError(failed_run_message(tools, resolved_job, state, catalog))
    if state not in COMPLETE_JOB_STATES:
        raise ScreeningRunError(
            f"harness job {resolved_job!r} has no usable terminal status {state!r}"
        )

    listing = _tool_document(
        tools.list_artifacts(job_id=resolved_job),
        f"list_artifacts({resolved_job!r})",
        error_type=ScreeningArtifactError,
    )
    entries = _artifact_entries(listing, resolved_job)
    screen_entry = _screen_entry(entries, resolved_job)
    raw_artifact = _tool_document(
        tools.read_artifact(job_id=resolved_job, name=SCREEN_ARTIFACT),
        f"read_artifact({SCREEN_ARTIFACT!r})",
        error_type=ScreeningArtifactError,
    )
    document = _screen_document(raw_artifact, resolved_job)
    _verify_schema(document, resolved_job)
    _verify_job(document, resolved_job)
    artifact_status = _optional_text(document.get("status"))
    if artifact_status is None:
        raise ScreeningArtifactError(
            f"harness job {resolved_job!r} screen artifact reported no status"
        )
    if artifact_status in FAILED_ARTIFACT_STATES:
        raise ScreeningRunError(_failure_message(document, resolved_job))
    if artifact_status not in COMPLETE_ARTIFACT_STATES:
        raise ScreeningArtifactError(
            f"harness job {resolved_job!r} screen artifact has an invalid status "
            f"{artifact_status!r}"
        )
    rows = _rows(document.get("rows"), resolved_job)
    rows_reason = None if rows else NO_ROWS_REASON
    _verify_definition(document, definition, resolved_job)
    resolved_as_of = _text(document.get("as_of"), "as_of")
    provenance: dict[str, object] = {
        "artifact": screen_entry,
        "artifacts": entries,
        "catalog": catalog,
        "screen": {key: value for key, value in document.items() if key != "rows"},
    }
    payload: dict[str, object] = {
        "as_of": resolved_as_of,
        "definition_hash": definition.definition_sha256,
        "definition_id": definition.definition_id,
        "provenance": provenance,
        "rows": [row.document() for row in rows],
        "rows_reason": rows_reason,
        "job_id": resolved_job,
        "status": artifact_status,
        "workspace_id": resolved_workspace,
    }
    return ScreeningRecord(
        result_id=screening_result_id(payload),
        workspace_id=resolved_workspace,
        definition_id=definition.definition_id,
        definition_hash=definition.definition_sha256,
        job_id=resolved_job,
        status=artifact_status,
        as_of=resolved_as_of,
        rows=rows,
        rows_reason=rows_reason,
        provenance=provenance,
    )


def _tool_document(
    result: McpToolResult,
    label: str,
    *,
    error_type: type[ScreeningRunError] = ScreeningRunError,
) -> dict[str, object]:
    if result.is_error:
        message = result.text.strip() or f"{label} failed"
        raise error_type(f"{label} failed: {message}")
    return dict(result.structured)


def _artifact_entries(
    listing: Mapping[str, object], job_id: str
) -> list[dict[str, object]]:
    raw = listing.get("artifacts")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ScreeningArtifactError(
            f"harness job {job_id!r} returned no artifact index"
        )
    entries: list[dict[str, object]] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise ScreeningArtifactError(
                f"harness job {job_id!r} returned an invalid artifact entry"
            )
        entries.append({str(key): value for key, value in entry.items()})
    return entries


def _screen_entry(
    entries: Sequence[Mapping[str, object]], job_id: str
) -> dict[str, object]:
    for entry in entries:
        if entry.get("name") == SCREEN_ARTIFACT:
            return dict(entry)
    raise ScreeningArtifactError(
        f"harness job {job_id!r} has no {SCREEN_ARTIFACT!r} artifact"
    )


def _screen_document(raw: Mapping[str, object], job_id: str) -> dict[str, object]:
    data = _artifact_bytes(raw)
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ScreeningArtifactError(
            f"harness job {job_id!r} returned an unreadable {SCREEN_ARTIFACT!r}"
        ) from error
    if not isinstance(document, Mapping):
        raise ScreeningArtifactError(
            f"harness job {job_id!r} {SCREEN_ARTIFACT!r} is not an object"
        )
    return {str(key): value for key, value in document.items()}


def _artifact_bytes(raw: Mapping[str, object]) -> bytes:
    content = raw.get("content")
    if not isinstance(content, str):
        raise ScreeningArtifactError("artifact content must be encoded text")
    encoding = raw.get("encoding")
    if isinstance(encoding, str) and encoding.lower() == "hex":
        try:
            return bytes.fromhex(content)
        except ValueError as error:
            raise ScreeningArtifactError("artifact hex content is invalid") from error
    return content.encode("utf-8")


def _verify_schema(document: Mapping[str, object], job_id: str) -> None:
    if document.get("schema") != SCREEN_SCHEMA:
        raise ScreeningArtifactError(
            f"harness job {job_id!r} screen artifact schema must be {SCREEN_SCHEMA!r}"
        )


def _verify_job(document: Mapping[str, object], job_id: str) -> None:
    """The artifact must name the job it was read for; a missing or other id fails."""
    echoed = _optional_text(document.get("job_id"))
    if echoed is None:
        raise ScreeningArtifactError(
            f"harness job {job_id!r} screen artifact requires 'job_id'"
        )
    if echoed != job_id:
        raise ScreeningArtifactError(
            f"harness job {job_id!r} screen artifact was produced for job {echoed!r}"
        )


def _rows(value: object, job_id: str) -> tuple[ScreenRow, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ScreeningArtifactError(
            f"harness job {job_id!r} screen result requires a row list"
        )
    rows: list[ScreenRow] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ScreeningArtifactError(
                f"harness job {job_id!r} returned an invalid screen row"
            )
        row = {str(key): item_value for key, item_value in item.items()}
        missing = sorted(_ROW_KEYS - set(row))
        if missing:
            names = ", ".join(repr(name) for name in missing)
            raise ScreeningArtifactError(
                f"harness job {job_id!r} screen row requires {names}"
            )
        unexpected = sorted(set(row) - _ROW_KEYS)
        if unexpected:
            names = ", ".join(repr(name) for name in unexpected)
            raise ScreeningArtifactError(
                f"harness job {job_id!r} screen row has unexpected keys: {names}"
            )
        rows.append(
            ScreenRow(
                symbol=_text(row.get("symbol"), "row symbol"),
                metrics=_metrics(row.get("metrics")),
            )
        )
    return tuple(rows)


def _metrics(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ScreeningArtifactError("screen row metrics must be an object")
    metrics: dict[str, object] = {}
    for key, metric in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ScreeningArtifactError("screen metric names must be non-empty text")
        if not isinstance(metric, str) or not metric.strip():
            raise ScreeningArtifactError(
                f"screen metric {key!r} must be a non-empty decimal string"
            )
        metrics[key] = metric
    return metrics


def _verify_definition(
    document: Mapping[str, object], definition: ScreenerDefinition, job_id: str
) -> None:
    expected = (
        ("definition_id", definition.definition_id),
        ("definition_hash", definition.definition_sha256),
        ("bundle_hash", definition.source_sha256),
    )
    for field_name, wanted in expected:
        echoed = document.get(field_name)
        if not isinstance(echoed, str) or not echoed.strip():
            raise ScreeningArtifactError(
                f"harness job {job_id!r} screen artifact requires {field_name!r}"
            )
        if echoed.strip() != wanted:
            raise ScreeningArtifactError(
                f"harness job {job_id!r} was produced for a different definition"
            )


def _failure_message(document: Mapping[str, object], job_id: str) -> str:
    return _with_reason(
        f"harness job {job_id!r} failed", _optional_text(document.get("error"))
    )


def failed_run_message(
    tools: ScreeningToolProvider,
    job_id: str,
    state: str,
    catalog: Mapping[str, object],
) -> str:
    """Name a failed run with the reason the harness published for it.

    The screen artifact's ``error`` is the screening run's own reason and wins
    when that artifact is readable for this job; otherwise the job record's
    ``error_message`` is used.  When neither is published the message says so.
    """
    message = f"harness job {job_id!r} ended in state {state!r}"
    reason = _artifact_error(tools, job_id) or _optional_text(
        catalog.get("error_message")
    )
    return _with_reason(message, reason)


def _with_reason(message: str, reason: str | None) -> str:
    if reason is None:
        return f"{message}; no failure reason was published"
    return f"{message}: {reason}"


def _artifact_error(tools: ScreeningToolProvider, job_id: str) -> str | None:
    """Return the ``error`` of this job's readable ``screen.json``, when published.

    A missing, unreadable, or foreign artifact contributes no reason: a failed
    run is still reported as failed, never as another job's failure.
    """
    try:
        listing = _tool_document(
            tools.list_artifacts(job_id=job_id),
            f"list_artifacts({job_id!r})",
            error_type=ScreeningArtifactError,
        )
        entries = _artifact_entries(listing, job_id)
        if not any(entry.get("name") == SCREEN_ARTIFACT for entry in entries):
            return None
        raw_artifact = _tool_document(
            tools.read_artifact(job_id=job_id, name=SCREEN_ARTIFACT),
            f"read_artifact({SCREEN_ARTIFACT!r})",
            error_type=ScreeningArtifactError,
        )
        document = _screen_document(raw_artifact, job_id)
        _verify_job(document, job_id)
    except (ScreeningArtifactError, HarnessMcpError):
        return None
    return _optional_text(document.get("error"))


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScreeningArtifactError(f"{label} must be non-empty text")
    return value.strip()


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value.strip()


__all__ = [
    "COMPLETE_ARTIFACT_STATES",
    "COMPLETE_JOB_STATES",
    "FAILED_ARTIFACT_STATES",
    "FAILED_JOB_STATES",
    "NO_ROWS_REASON",
    "PENDING_JOB_STATES",
    "SCREEN_ARTIFACT",
    "SCREEN_SCHEMA",
    "ScreenRow",
    "ScreeningArtifactError",
    "ScreeningRecord",
    "ScreeningRunError",
    "ScreeningRunPending",
    "ScreeningToolProvider",
    "failed_run_message",
    "import_screening_run",
    "screening_result_id",
]
