"""Hypothesis-validation HTTP surface.

The router is a thin transport adapter: it translates validated payloads into
:class:`ValidationService` calls and never touches workflow internals.  The
service launches a hypothesis-validation workflow through the injected
:class:`~thesis.chat.responder.ValidationRuntime` and records the finished
contract, verdict, and evidence in an in-memory workspace store, so the UI can
launch a claim and then read its record.  The runtime is resolved from
``app.state.workflow_runtime`` and the surface fails closed with a 503 when no
runtime is configured.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from thesis.chat.contracts import RecordNotFound, WorkflowExecution
from thesis.chat.responder import ValidationRuntime, launch_hypothesis_validation
from thesis.chat.service import ChatService
from thesis.http.chat import get_chat_service

if TYPE_CHECKING:
    from thesis.workflows.hypothesis_validation import (
        HypothesisSpecification,
        HypothesisValidationOutcome,
        HypothesisValidationPhase,
    )

_DEFAULT_ACTOR = "thesis-agent"
_CHAT_TITLE_LIMIT = 200


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ValidationEntry:
    """One launched validation run with its durable execution record."""

    execution: WorkflowExecution
    outcome: HypothesisValidationOutcome
    claim: str


class LaunchValidationRequest(BaseModel):
    """Request boundary for one hypothesis-validation launch."""

    model_config = ConfigDict(extra="forbid")

    claim: str = Field(min_length=1, max_length=8_192)
    actor_id: str = Field(default=_DEFAULT_ACTOR, min_length=1, max_length=128)
    chat_id: str | None = Field(default=None, min_length=1, max_length=128)
    investigation_id: str | None = Field(default=None, min_length=1, max_length=128)
    experiment_id: str | None = Field(default=None, min_length=1, max_length=128)


class ValidationService:
    """Launch and read hypothesis-validation records over an injected runtime."""

    def __init__(
        self,
        *,
        runtime: ValidationRuntime,
        chat_service: ChatService,
    ) -> None:
        self._runtime = runtime
        self._chat = chat_service
        self._records: dict[str, list[ValidationEntry]] = {}

    @property
    def runtime(self) -> ValidationRuntime:
        """Return the injected workflow runtime."""
        return self._runtime

    def list(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        """Return every validation record for the workspace, newest first."""
        return tuple(
            self._document(entry) for entry in self._records.get(workspace_id, ())
        )

    def get(self, workspace_id: str, validation_id: str) -> dict[str, object]:
        """Return one validation record by its execution identifier."""
        for entry in self._records.get(workspace_id, ()):
            if entry.execution.execution_id == validation_id:
                return self._document(entry)
        raise RecordNotFound(
            f"validation {validation_id!r} was not found in workspace {workspace_id!r}"
        )

    def launch(
        self,
        workspace_id: str,
        *,
        claim: str,
        actor_id: str = _DEFAULT_ACTOR,
        chat_id: str | None = None,
        investigation_id: str | None = None,
        experiment_id: str | None = None,
    ) -> dict[str, object]:
        """Launch and execute one validation run, then return its record.

        A chat is created for the claim when the caller does not supply one, so
        the launch always records an execution against a real conversation.
        """
        if chat_id is None:
            chat = self._chat.create_chat(
                workspace_id,
                title=_chat_title(claim),
                owner_actor_id=actor_id,
            )
            chat_id = chat.chat_id
        _, outcome = launch_hypothesis_validation(
            self._runtime,
            self._chat,
            workspace_id=workspace_id,
            chat_id=chat_id,
            actor_id=actor_id,
            claim=claim,
            investigation_id=investigation_id,
            experiment_id=experiment_id,
        )
        execution = self._chat.get_execution(workspace_id, outcome.execution_id)
        entry = ValidationEntry(execution=execution, outcome=outcome, claim=claim)
        self._records.setdefault(workspace_id, []).insert(0, entry)
        return self._document(entry)

    def _document(self, entry: ValidationEntry) -> dict[str, object]:
        execution = entry.execution
        outcome = entry.outcome
        specification = outcome.specification
        verdict = outcome.verdict
        report = outcome.report
        return {
            "id": execution.execution_id,
            "workspace_id": execution.workspace_id,
            "status": _status(outcome.phase),
            "claim": entry.claim,
            "contract": _contract_document(outcome.specification),
            "verdict": "pending" if verdict is None else verdict.outcome,
            "verdict_detail": None if verdict is None else verdict.as_dict(),
            "evidence": _evidence_items(outcome),
            "created_at": _iso(execution.created_at),
            "updated_at": _iso(execution.finished_at or execution.updated_at),
            "hypothesis": None if specification is None else specification.claim,
            "execution": execution.as_dict(),
            "report": None if report is None else report.as_dict(),
            "backtest": (
                None if outcome.backtest is None else outcome.backtest.as_dict()
            ),
            "readiness": (
                None if outcome.readiness is None else outcome.readiness.as_dict()
            ),
            "window": None if outcome.window is None else outcome.window.as_dict(),
            "transitions": [item.as_dict() for item in outcome.transitions],
            "journal_id": outcome.journal_id,
            "detail": outcome.detail,
        }


def _chat_title(claim: str) -> str:
    collapsed = " ".join(claim.split())
    title = f"Validation: {collapsed}" if collapsed else "Validation"
    return title[:_CHAT_TITLE_LIMIT]


def _status(phase: HypothesisValidationPhase) -> str:
    if phase.value == "completed":
        return "completed"
    if phase.value == "failed":
        return "failed"
    return "running"


def _direction(operator: str) -> str:
    if operator in {"gte", "gt"}:
        return "increase"
    return "decrease"


def _contract_document(
    specification: HypothesisSpecification | None,
) -> dict[str, object] | None:
    if specification is None:
        return None
    criteria = tuple(specification.criteria or ())
    primary = criteria[0] if criteria else None
    bundle = specification.bundle
    return {
        "schema_version": 1,
        "hypothesis": specification.claim,
        "metric": None if primary is None else primary.metric,
        "threshold": None if primary is None else primary.threshold,
        "direction": None if primary is None else _direction(primary.operator),
        "evidence_requirements": [
            criterion.label
            or f"{criterion.metric} {criterion.operator} {criterion.threshold}"
            for criterion in criteria
        ],
        "claim_type": specification.claim_type,
        "variables": list(specification.variables),
        "data_requirements": dict(specification.data_requirements),
        "criteria": [criterion.as_dict() for criterion in criteria],
        "strategy": {
            "strategy_id": bundle.strategy_id,
            "version": bundle.version,
            "sha256": bundle.bundle_sha256,
        },
    }


def _evidence_items(outcome: HypothesisValidationOutcome) -> list[dict[str, object]]:
    backtest = outcome.backtest
    report = outcome.report
    run_id = None if backtest is None else backtest.job_id
    items: list[dict[str, object]] = []
    if backtest is not None:
        manifest_reference = (
            None if report is None else report.harness_manifest_reference
        )
        items.append(
            {
                "id": f"{outcome.execution_id}-backtest",
                "title": f"Backtest {run_id}",
                "reference": manifest_reference or f"backtests/{run_id}",
                "kind": "backtest",
                "run_id": run_id,
                "artifact": None,
            }
        )
    if report is not None:
        for index, reference in enumerate(report.artifact_references):
            uri = _reference_uri(reference)
            items.append(
                {
                    "id": f"{outcome.execution_id}-artifact-{index}",
                    "title": uri or f"Artifact {index + 1}",
                    "reference": uri or "",
                    "kind": "artifact",
                    "run_id": run_id,
                    "artifact": uri,
                }
            )
    return items


def _reference_uri(reference: object) -> str | None:
    if isinstance(reference, Mapping):
        value = reference.get("uri")
        if isinstance(value, str) and value.strip():
            return value
    return None


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def get_workflow_runtime(request: Request) -> ValidationRuntime:
    """Resolve the workflow runtime from app state, or fail closed with 503."""
    runtime = getattr(request.app.state, "workflow_runtime", None)
    if runtime is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "validation_unavailable",
                "message": "workflow runtime is not configured",
            },
        )
    return runtime


def get_validation_service(
    request: Request,
    runtime: Annotated[ValidationRuntime, Depends(get_workflow_runtime)],
) -> ValidationService:
    """Resolve (or build) the validation service bound to the app runtime."""
    service = getattr(request.app.state, "validation_service", None)
    if not isinstance(service, ValidationService) or service.runtime is not runtime:
        service = ValidationService(
            runtime=runtime, chat_service=get_chat_service(request)
        )
        request.app.state.validation_service = service
    return service


def _service_error(error: Exception) -> HTTPException:
    if isinstance(error, (RecordNotFound, KeyError)):
        detail = error.args[0] if error.args else error
        return HTTPException(
            status_code=404,
            detail={"code": "validation_not_found", "message": str(detail)},
        )
    if isinstance(error, ValueError):
        return HTTPException(status_code=422, detail=str(error))
    return HTTPException(
        status_code=500,
        detail={"code": "validation_error", "message": "validation request failed"},
    )


def create_validation_router() -> APIRouter:
    """Create the hypothesis-validation router; the runtime resolves from state."""
    router = APIRouter(prefix="/v1/workspaces/{workspace_id}", tags=["validation"])

    @router.post("/validation")
    async def launch_validation(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: LaunchValidationRequest,
        service: Annotated[ValidationService, Depends(get_validation_service)],
    ) -> dict[str, object]:
        try:
            return service.launch(
                workspace_id,
                claim=request.claim,
                actor_id=request.actor_id,
                chat_id=request.chat_id,
                investigation_id=request.investigation_id,
                experiment_id=request.experiment_id,
            )
        except Exception as error:
            raise _service_error(error) from error

    @router.get("/validation")
    async def list_validations(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        service: Annotated[ValidationService, Depends(get_validation_service)],
    ) -> dict[str, object]:
        return {"items": list(service.list(workspace_id))}

    @router.get("/validation/{validation_id}")
    async def get_validation(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        validation_id: Annotated[str, Path(min_length=1, max_length=128)],
        service: Annotated[ValidationService, Depends(get_validation_service)],
    ) -> dict[str, object]:
        try:
            return service.get(workspace_id, validation_id)
        except Exception as error:
            raise _service_error(error) from error

    return router


__all__ = [
    "LaunchValidationRequest",
    "ValidationEntry",
    "ValidationService",
    "create_validation_router",
    "get_validation_service",
    "get_workflow_runtime",
]
