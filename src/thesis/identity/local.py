"""Local JSON-file identity stores for accounts, invites, and sessions.

Local development persists the identity records in repository-ignored JSON
files under ``.local/thesis/identity``.  The adapters here implement the
storage-agnostic ports in :mod:`thesis.identity.ports` without importing a
storage provider, so the domain and application layers stay independent of
where the records live.

An invite revocation and a session revocation share a name and signature but
mean different things, so the ports are implemented by three sibling adapters
over one atomic-JSON base rather than by a single class.  Each adapter owns one
file and serializes access with an ``RLock``; every write creates parent
directories, writes a temporary sibling, fsyncs it, and atomically replaces the
destination with :func:`os.replace`.  Serialized order is deterministic and
timestamps are always supplied by the caller, so the stores never read the wall
clock.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Final

from thesis.identity.models import (
    Account,
    Email,
    Identifier,
    Invite,
    Session,
    Sha256Digest,
    normalize_email,
)
from thesis.identity.owner import Clock, seed_owner_account

_SCHEMA_VERSION: Final = 1
_LOCAL_IDENTITY_PARTS: Final = (".local", "thesis", "identity")


class LocalIdentityStoreError(RuntimeError):
    """Local identity state is unsafe, unavailable, or malformed."""


def default_local_identity_root() -> Path:
    """Return the repository-ignored root for local identity JSON state.

    The layout follows the market-lake local convention: the repository root
    owns a git-ignored ``.local`` tree, and Thesis identity files live under
    ``.local/thesis/identity``.  An explicit path always overrides this
    default, which is what tests pass over ``tmp_path``.
    """
    repository_root = Path(__file__).resolve().parents[3]
    return repository_root.joinpath(*_LOCAL_IDENTITY_PARTS)


def _require_aware(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("timestamp must be a timezone-aware datetime")
    return value


def _replace_timestamp[T: (Account, Invite, Session)](
    record: T, **updates: object
) -> T:
    """Return a revalidated copy of ``record`` with lifecycle fields replaced."""
    values = record.model_dump(mode="json")
    values.update(updates)
    return type(record).model_validate(values)


class _LocalJsonCollection[T: (Account, Invite, Session)]:
    """Thread-safe, atomically replaced JSON collection of one record type."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None,
        *,
        collection: str,
        model: type[T],
    ) -> None:
        if path is None:
            path = default_local_identity_root() / f"{collection}.json"
        self._path = Path(path).expanduser().resolve()
        self._collection = collection
        self._model = model
        self._lock = RLock()

    @property
    def path(self) -> Path:
        """Return the resolved JSON file backing this store."""
        return self._path

    def _read(self) -> dict[str, T]:
        if not self._path.exists():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or set(payload) != {
                "schema_version",
                self._collection,
            }:
                raise ValueError("store root has unexpected fields")
            if payload["schema_version"] != _SCHEMA_VERSION:
                raise ValueError("store schema version is unsupported")
            rows = payload[self._collection]
            if not isinstance(rows, list):
                raise ValueError("store collection must be a list")
            records = tuple(
                self._model.model_validate_json(json.dumps(row)) for row in rows
            )
            result = {record.id: record for record in records}
            if len(result) != len(records):
                raise ValueError("store contains duplicate ids")
            return result
        except (OSError, TypeError, ValueError) as error:
            raise LocalIdentityStoreError(
                f"local identity store is invalid: {self._path}"
            ) from error

    def _write(self, records: Iterable[T]) -> None:
        ordered = sorted(records, key=lambda record: (record.created_at, record.id))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": _SCHEMA_VERSION,
            self._collection: [record.model_dump(mode="json") for record in ordered],
        }
        serialized = (
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
        )
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(serialized)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, self._path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


class LocalAccountStore(_LocalJsonCollection[Account]):
    """JSON-file account directory with a unique normalized email index."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        super().__init__(path, collection="accounts", model=Account)

    def get(self, account_id: Identifier) -> Account | None:
        """Return the account with ``account_id``, or ``None``."""
        with self._lock:
            return self._read().get(account_id)

    def get_by_email(self, email: Email) -> Account | None:
        """Return the account whose normalized email matches, or ``None``."""
        wanted = normalize_email(email)
        with self._lock:
            for account in self._read().values():
                if account.email == wanted:
                    return account
        return None

    def put(self, account: Account) -> Account:
        """Insert or replace ``account`` while keeping emails unique."""
        if not isinstance(account, Account):
            raise TypeError("account must be an Account")
        with self._lock:
            records = self._read()
            for existing in records.values():
                if existing.email == account.email and existing.id != account.id:
                    raise ValueError("email is already bound to another account")
            records[account.id] = account
            self._write(records.values())
        return account

    def list(self) -> tuple[Account, ...]:
        """Return every stored account ordered by id."""
        with self._lock:
            accounts = sorted(self._read().values(), key=lambda item: item.id)
        return tuple(accounts)


class LocalInviteStore(_LocalJsonCollection[Invite]):
    """JSON-file invite store with an atomic single-use consume contract."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        super().__init__(path, collection="invites", model=Invite)

    def get(self, invite_id: Identifier) -> Invite | None:
        """Return the invite with ``invite_id``, or ``None``."""
        with self._lock:
            return self._read().get(invite_id)

    def get_by_token_hash(self, token_hash: Sha256Digest) -> Invite | None:
        """Return the invite matching the hashed token, or ``None``."""
        with self._lock:
            matches = sorted(
                (
                    invite
                    for invite in self._read().values()
                    if invite.token_hash == token_hash
                ),
                key=lambda invite: invite.id,
            )
        return matches[0] if matches else None

    def list(self) -> tuple[Invite, ...]:
        """Return every stored invite ordered by id."""
        with self._lock:
            invites = sorted(self._read().values(), key=lambda item: item.id)
        return tuple(invites)

    def put(self, invite: Invite) -> Invite:
        """Insert or replace ``invite`` by id."""
        if not isinstance(invite, Invite):
            raise TypeError("invite must be an Invite")
        with self._lock:
            records = self._read()
            records[invite.id] = invite
            self._write(records.values())
        return invite

    def consume(
        self,
        invite_id: Identifier,
        *,
        token_hash: Sha256Digest,
        at: datetime,
        email: Email | None = None,
    ) -> Invite | None:
        """Atomically redeem a single-use invite.

        Compare-and-set: succeeds only while the stored invite still carries
        ``token_hash``, is neither consumed nor revoked, is redeemable at
        ``at``, and — when ``email`` is supplied — is bound to that address.
        The read, comparison, replacement, and write happen under one lock, so
        concurrent callers produce exactly one winning transition.
        """
        with self._lock:
            records = self._read()
            invite = records.get(invite_id)
            if invite is None or invite.token_hash != token_hash:
                return None
            if not invite.is_redeemable(at):
                return None
            if email is not None and not invite.is_bound_to(email):
                return None
            consumed = _replace_timestamp(invite, consumed_at=at)
            records[invite_id] = consumed
            self._write(records.values())
        return consumed

    def revoke(self, invite_id: Identifier, *, at: datetime) -> Invite | None:
        """Atomically revoke an unconsumed invite, returning it or ``None``."""
        with self._lock:
            records = self._read()
            invite = records.get(invite_id)
            if (
                invite is None
                or invite.consumed_at is not None
                or invite.revoked_at is not None
            ):
                return None
            revoked = _replace_timestamp(invite, revoked_at=at)
            records[invite_id] = revoked
            self._write(records.values())
        return revoked


class LocalSessionStore(_LocalJsonCollection[Session]):
    """JSON-file session store supporting explicit revocation and expiry."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        super().__init__(path, collection="sessions", model=Session)

    def get(self, session_id: Identifier) -> Session | None:
        """Return the session with ``session_id``, or ``None``."""
        with self._lock:
            return self._read().get(session_id)

    def put(self, session: Session) -> Session:
        """Insert or replace ``session`` by id."""
        if not isinstance(session, Session):
            raise TypeError("session must be a Session")
        with self._lock:
            records = self._read()
            records[session.id] = session
            self._write(records.values())
        return session

    def revoke(self, session_id: Identifier, *, at: datetime) -> Session | None:
        """Atomically mark an active session revoked, returning it or ``None``."""
        with self._lock:
            records = self._read()
            session = records.get(session_id)
            if session is None or not session.is_active(at):
                return None
            revoked = _replace_timestamp(session, revoked_at=at)
            records[session_id] = revoked
            self._write(records.values())
        return revoked

    def delete_expired(self, *, at: datetime) -> int:
        """Delete sessions expired at ``at`` and return the number removed."""
        checked_at = _require_aware(at)
        with self._lock:
            records = self._read()
            expired = [
                session_id
                for session_id, session in records.items()
                if checked_at >= session.expires_at
            ]
            if not expired:
                return 0
            for session_id in expired:
                del records[session_id]
            self._write(records.values())
        return len(expired)


@dataclass(frozen=True, slots=True)
class LocalIdentityStore:
    """The three local JSON adapters over one shared identity root."""

    accounts: LocalAccountStore
    invites: LocalInviteStore
    sessions: LocalSessionStore

    @classmethod
    def open(cls, root: str | os.PathLike[str] | None = None) -> LocalIdentityStore:
        """Open the three adapters over ``root`` or the local default root."""
        directory = (
            Path(root).expanduser()
            if root is not None
            else default_local_identity_root()
        )
        return cls(
            accounts=LocalAccountStore(directory / "accounts.json"),
            invites=LocalInviteStore(directory / "invites.json"),
            sessions=LocalSessionStore(directory / "sessions.json"),
        )


def seed_local_owner(
    store: LocalIdentityStore,
    *,
    email: str,
    display_name: str | None = None,
    clock: Clock,
) -> Account:
    """Seed the default owner into a local identity store's account file.

    A thin convenience over :func:`thesis.identity.owner.seed_owner_account`
    for the local composition point; it adds no behavior of its own.
    """
    return seed_owner_account(
        store.accounts, email=email, display_name=display_name, clock=clock
    )


__all__ = [
    "LocalAccountStore",
    "LocalIdentityStore",
    "LocalIdentityStoreError",
    "LocalInviteStore",
    "LocalSessionStore",
    "default_local_identity_root",
    "seed_local_owner",
]
