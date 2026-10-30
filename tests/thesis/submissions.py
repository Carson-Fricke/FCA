"""In-memory external submission-store double for isolated behavior fixtures."""

from collections.abc import Mapping
from copy import deepcopy

from thesis.persistence.submissions import require_same_submission, submission_document


class MemorySubmissions:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], dict[str, object]] = {}

    def retain(
        self,
        workspace_id: str,
        job_id: str,
        operation: str,
        inputs: Mapping[str, object],
    ) -> None:
        document = submission_document(workspace_id, job_id, operation, inputs)
        key = (workspace_id, job_id)
        if key in self.records:
            require_same_submission(self.records[key], document)
        else:
            self.records[key] = document

    def get(self, workspace_id: str, job_id: str) -> dict[str, object] | None:
        return deepcopy(self.records.get((workspace_id, job_id)))

    def find(self, job_id: str) -> dict[str, object] | None:
        matches = [value for (_, key), value in self.records.items() if key == job_id]
        if len(matches) > 1:
            raise ValueError("ambiguous retained execution identity")
        return deepcopy(matches[0]) if matches else None

    def list(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        return tuple(
            deepcopy(value)
            for key, value in self.records.items()
            if key[0] == workspace_id
        )
