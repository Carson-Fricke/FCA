"""Database-clock leases validated in the transaction of every mutation."""

from __future__ import annotations

from uuid import uuid4

from thesis_runtime.storage.ownership import Claim, OwnershipLost

from .store import PostgresStore, identity


class PostgresOwnershipStore:
    def __init__(self, store: PostgresStore) -> None:
        self.store = store

    def acquire(self, workspace_id: str, resource: str, seconds: float) -> Claim | None:
        with self.store.transaction(workspace_id, fenced=False) as connection:
            previous = self.store.read(
                workspace_id, identity("Ownership:", resource), connection=connection
            )
            row = connection.execute(
                "SELECT extract(epoch FROM clock_timestamp())"
            ).fetchone()
            assert row is not None
            now = float(row[0])
            if previous is not None and previous["expiresAt"] > now:
                return None
            token = uuid4().hex
            fence = 1 if previous is None else int(previous["fence"]) + 1
            self.store.put(
                connection,
                {
                    "id": identity("Ownership:", resource),
                    "workspaceId": workspace_id,
                    "recordType": "Ownership",
                    "resource": resource,
                    "token": token,
                    "fence": fence,
                    "expiresAt": now + seconds,
                },
            )
            return Claim(workspace_id, resource, token, fence)

    def _change(self, claim: Claim, seconds: float | None) -> None:
        with self.store.transaction(claim.workspace_id, fenced=False) as connection:
            document = self.store.read(
                claim.workspace_id,
                identity("Ownership:", claim.resource),
                connection=connection,
            )
            row = connection.execute(
                "SELECT extract(epoch FROM clock_timestamp())"
            ).fetchone()
            assert row is not None
            now = float(row[0])
            if (
                document is None
                or document["token"] != claim.token
                or document["fence"] != claim.fence
                or document["expiresAt"] <= now
            ):
                raise OwnershipLost("stale ownership: " + claim.resource)
            if seconds is not None:
                document["expiresAt"] = 0 if seconds == 0 else now + seconds
                self.store.put(connection, document)

    def check(self, claim: Claim) -> None:
        self._change(claim, None)

    def renew(self, claim: Claim, seconds: float) -> None:
        self._change(claim, seconds)

    def release(self, claim: Claim) -> None:
        self._change(claim, 0)
