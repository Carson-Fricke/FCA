"""Read-only navigation metadata from current canonical timeline receipts.

Owns bounded titles and receipt pointers, never transcript persistence, provider
projection, execution, or inferred historical formats. The timeline router owns
authorization and paging; the UI resolves conversation/turn hierarchy.
"""

from collections.abc import Mapping
from typing import Any

OUTLINE_RESULT_TYPES = (
    "workflow_execution",
    "run_study",
    "author_study",
    "update_study",
    "read_study_output",
    "read_study",
    "read_artifact",
    "list_artifacts",
)
OUTLINE_METADATA_FIELDS = (
    "state",
    "executionId",
    "workflowId",
    "title",
    "job_id",
    "programId",
    "program_id",
    "name",
    "kind",
)
OUTLINE_METADATA_ARRAYS = (("artifacts", "name"),)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def project_outline_item(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Select navigation facts without copying reasoning, content, or results."""
    base = {
        "seq": item["seq"],
        "threadId": item.get("threadId") or "root",
        "messageId": item["messageId"],
        "datumIndex": None,
        "entityId": None,
        "executionId": item["executionId"],
        "requestMessageId": item["requestMessageId"],
        "status": None,
    }
    if item["role"] == "user" and base["threadId"] == "root":
        return [
            {
                **base,
                "id": item["messageId"],
                "kind": "turn",
                "title": item["title"],
            }
        ]
    rows = []
    if (
        base["threadId"] == "root"
        and item["direction"] == "outgoing"
        and item["hasRequests"]
    ):
        rows.append({**base, "id": item["messageId"], "kind": "request", "title": ""})
    arguments = item["arguments"]
    indexed = item["indexed"]
    datums = item["datums"]
    for index, datum in enumerate(datums):
        data = datum.get("structured") or {}
        kind = datum["datumType"]
        row = {
            **base,
            "id": f"{item['messageId']}:datum:{index}"
            if indexed
            else item["messageId"],
            "datumIndex": index if indexed else None,
        }
        status = _text(data.get("state"))
        if not datum.get("ok"):
            status = "failed"
        if kind == "workflow_execution":
            identity = _text(data.get("executionId"))
            if identity:
                rows.append(
                    {
                        **row,
                        "kind": "workflow",
                        "title": _text(data.get("title"))
                        or _text(data.get("workflowId"))
                        or "Workflow",
                        "entityId": identity,
                        "status": status,
                    }
                )
        elif kind in {
            "run_study",
            "author_study",
            "update_study",
            "read_study_output",
            "read_study",
        } or (data.get("kind") == "study" and _text(data.get("job_id"))):
            identity = (
                _text(data.get("job_id"))
                or _text(data.get("programId"))
                or _text(data.get("program_id"))
            )
            if identity:
                rows.append(
                    {
                        **row,
                        "kind": "study",
                        "title": _text(data.get("programId"))
                        or _text(data.get("program_id"))
                        or _text(arguments.get("program_id"))
                        or "Study evidence",
                        "entityId": identity,
                        "status": status
                        or ("submitted" if kind == "run_study" else None),
                    }
                )
        elif kind == "read_artifact":
            name = _text(data.get("name")) or _text(arguments.get("name"))
            if name:
                rows.append(
                    {
                        **row,
                        "kind": "artifact",
                        "title": name[:160],
                        "entityId": f"{data.get('job_id', '')}:{name}",
                        "status": status,
                    }
                )
        elif kind == "list_artifacts":
            for artifact in data.get("artifacts", ()):
                name = (
                    _text(artifact.get("name"))
                    if isinstance(artifact, Mapping)
                    else None
                )
                if name:
                    rows.append(
                        {
                            **row,
                            "id": f"{row['id']}:{name}",
                            "kind": "artifact",
                            "title": name[:160],
                            "entityId": f"{data.get('job_id', '')}:{name}",
                            "status": status,
                        }
                    )
    return rows
