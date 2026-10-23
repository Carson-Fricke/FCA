"""FCA research records and retained submissions on the selected PostgreSQL pool."""

from collections.abc import Mapping

from thesis.persistence.submissions import require_same_submission, submission_document
from thesis.research.investigations import Investigation
from thesis_runtime.adapters.postgres.store import PostgresStore, identity


class PostgresSubmissionStore:
    def __init__(self, store: PostgresStore) -> None:
        self.store = store

    def retain(
        self,
        workspace_id: str,
        job_id: str,
        operation: str,
        inputs: Mapping[str, object],
    ) -> None:
        payload = submission_document(workspace_id, job_id, operation, inputs)
        with self.store.transaction(workspace_id) as connection:
            key = "JobSubmission:" + job_id
            previous = self.store.read(
                workspace_id, key, collection="submissions", connection=connection
            )
            if previous is not None:
                require_same_submission(previous["payload"], payload)
            else:
                self.store.put(
                    connection,
                    {
                        "id": key,
                        "workspaceId": workspace_id,
                        "recordType": "JobSubmission",
                        "payload": payload,
                    },
                    collection="submissions",
                    create=True,
                )

    def get(self, workspace_id: str, job_id: str) -> dict[str, object] | None:
        row = self.store.read(
            workspace_id, "JobSubmission:" + job_id, collection="submissions"
        )
        return None if row is None else row["payload"]

    def list(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        return tuple(
            row["payload"]
            for row in self.store.query(
                "workspace_id=%s AND record_type='JobSubmission'",
                [workspace_id],
                collection="submissions",
                order="id",
            )
        )


class PostgresInvestigationStore:
    def __init__(self, store: PostgresStore) -> None:
        self.store = store

    def save(self, record: Investigation) -> Investigation:
        with self.store.transaction(record.workspace_id) as connection:
            self.store.put(
                connection,
                {
                    "id": identity("", record.investigation_id),
                    "workspaceId": record.workspace_id,
                    "record": record.as_dict(),
                },
                collection="investigations",
            )
        return record

    def get(self, workspace_id: str, investigation_id: str) -> Investigation | None:
        from thesis.persistence.investigations import decode_investigation

        row = self.store.read(
            workspace_id, identity("", investigation_id), collection="investigations"
        )
        return None if row is None else decode_investigation(row["record"])

    def list(self, workspace_id: str) -> tuple[Investigation, ...]:
        from thesis.persistence.investigations import decode_investigation

        return tuple(
            decode_investigation(row["record"])
            for row in self.store.query(
                "workspace_id=%s",
                [workspace_id],
                collection="investigations",
                order="id",
            )
        )
