"""Versioned, dependency-injected workflow checkpoints.

The checkpoint store deliberately knows nothing about a workflow runner.  It
only persists opaque JSON state with enough metadata for a runner to reject a
checkpoint that it can no longer understand.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TypedDict, cast

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)
type JsonObject = dict[str, JsonValue]

_SCHEMA_VERSION = 1


class CheckpointError(RuntimeError):
    """Base error for checkpoint persistence and validation failures."""


class CheckpointCompatibilityError(CheckpointError):
    """A checkpoint is valid JSON but incompatible with the active runner."""


class CheckpointNotFound(CheckpointError):
    """The requested workflow has no checkpoint."""


class CheckpointStorage(Protocol):
    """Minimal durable storage boundary used by :class:`CheckpointStore`."""

    def load(self) -> Mapping[str, object]:
        """Load the complete checkpoint projection."""

    def save(self, value: Mapping[str, object]) -> None:
        """Atomically persist the complete checkpoint projection."""


class _Projection(TypedDict):
    """Typed view over the validated checkpoint projection."""

    schemaVersion: int
    checkpoints: dict[str, object]


class MemoryCheckpointStorage:
    """Deterministic storage useful for tests and process-local composition."""

    def __init__(self, initial: Mapping[str, object] | None = None) -> None:
        self._value: JsonObject = _json_object(
            {"schemaVersion": _SCHEMA_VERSION, "checkpoints": {}}
            if initial is None
            else initial
        )

    def load(self) -> Mapping[str, object]:
        """Return a detached checkpoint projection."""
        return _json_object(self._value)

    def save(self, value: Mapping[str, object]) -> None:
        """Replace the projection with a detached JSON copy."""
        self._value = _json_object(value)


class JsonCheckpointStorage:
    """Small local durable storage adapter for checkpoint projections."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Mapping[str, object]:
        """Load the projection, treating a missing file as an empty store."""
        if not self.path.exists():
            return {"schemaVersion": _SCHEMA_VERSION, "checkpoints": {}}
        if self.path.is_symlink() or not self.path.is_file():
            raise CheckpointError(f"checkpoint path is not a plain file: {self.path}")
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                value: object = json.load(stream)
        except (OSError, json.JSONDecodeError) as error:
            raise CheckpointError(f"could not read checkpoints: {self.path}") from error
        try:
            return _json_object(value)
        except TypeError as error:
            raise CheckpointError(
                f"checkpoint projection is invalid: {self.path}"
            ) from error

    def save(self, value: Mapping[str, object]) -> None:
        """Durably replace the checkpoint file without exposing a partial JSON file."""
        encoded = _canonical_json(_json_object(value)) + b"\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            if os.name != "nt":
                descriptor = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise CheckpointError(
                f"could not write checkpoints: {self.path}"
            ) from error


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """An immutable checkpoint and the runner version that produced it."""

    checkpoint_id: str
    workflow_id: str
    checkpoint_version: int
    intent_epoch: int
    state: Mapping[str, JsonValue]
    saved_at: str

    @property
    def version(self) -> int:
        """Return the schema version of the opaque workflow state."""
        return self.checkpoint_version

    def as_dict(self) -> JsonObject:
        """Return the durable JSON representation."""
        return {
            "checkpointId": self.checkpoint_id,
            "checkpointVersion": self.checkpoint_version,
            "intentEpoch": self.intent_epoch,
            "savedAt": self.saved_at,
            "state": _json_object(self.state),
            "workflowId": self.workflow_id,
        }


class CheckpointStore:
    """Persist and validate the latest checkpoint for each workflow."""

    def __init__(
        self,
        storage: CheckpointStorage,
        clock: Callable[[], datetime],
        *,
        supported_version: int = 1,
        token_factory: Callable[[], object] | None = None,
    ) -> None:
        if supported_version < 1:
            raise ValueError("supported_version must be positive")
        self.storage = storage
        self.clock = clock
        self.supported_version = supported_version
        self.token_factory = token_factory

    def save(
        self,
        workflow_id: str,
        state: Mapping[str, object],
        *,
        intent_epoch: int,
        checkpoint_version: int | None = None,
        checkpoint_id: str | None = None,
    ) -> Checkpoint:
        """Append a checkpoint and return its immutable record.

        Checkpoint IDs are deterministic when no token factory is supplied.  A
        factory can be injected by applications that need a distinct ID for
        each save while keeping identity generation outside this module.
        """
        _validate_identifier(workflow_id, "workflow_id")
        if intent_epoch < 0:
            raise ValueError("intent_epoch must not be negative")
        version = (
            self.supported_version if checkpoint_version is None else checkpoint_version
        )
        self.validate_version(version)
        normalized_state = _json_object(state)
        saved_at = _timestamp(self.clock)
        if checkpoint_id is None:
            supplied_token = (
                None if self.token_factory is None else self.token_factory()
            )
            if supplied_token is None:
                material = {
                    "intentEpoch": intent_epoch,
                    "state": normalized_state,
                    "version": version,
                    "workflowId": workflow_id,
                }
                checkpoint_id = f"checkpoint-{_digest(material)[:32]}"
            else:
                checkpoint_id = f"checkpoint-{_token(supplied_token)}"
        _validate_identifier(checkpoint_id, "checkpoint_id")
        checkpoint = Checkpoint(
            checkpoint_id=checkpoint_id,
            workflow_id=workflow_id,
            checkpoint_version=version,
            intent_epoch=intent_epoch,
            state=normalized_state,
            saved_at=saved_at,
        )
        projection = _projection(self.storage.load())
        records = projection["checkpoints"].setdefault(workflow_id, [])
        if not isinstance(records, list):
            raise CheckpointError(f"checkpoint records are invalid for {workflow_id!r}")
        for existing_value in records:
            existing = _checkpoint_from_value(existing_value)
            if existing.checkpoint_id == checkpoint_id:
                if (
                    existing.workflow_id != checkpoint.workflow_id
                    or existing.checkpoint_version != checkpoint.checkpoint_version
                    or existing.intent_epoch != checkpoint.intent_epoch
                    or existing.state != checkpoint.state
                ):
                    raise CheckpointError(
                        f"checkpoint ID {checkpoint_id!r} identifies different state"
                    )
                return existing
        records.append(checkpoint.as_dict())
        projection["checkpoints"][workflow_id] = records
        self.storage.save(projection)
        return checkpoint

    write = save
    create = save

    def get(self, workflow_id: str) -> Checkpoint | None:
        """Return and validate the newest checkpoint for a workflow."""
        _validate_identifier(workflow_id, "workflow_id")
        projection = _projection(self.storage.load())
        records = projection["checkpoints"].get(workflow_id, [])
        if not isinstance(records, list):
            raise CheckpointError(f"checkpoint records are invalid for {workflow_id!r}")
        if not records:
            return None
        return _checkpoint_from_value(records[-1])

    load = get

    def require(self, workflow_id: str) -> Checkpoint:
        """Return a checkpoint or raise an explicit not-found error."""
        checkpoint = self.get(workflow_id)
        if checkpoint is None:
            raise CheckpointNotFound(workflow_id)
        return checkpoint

    def validate(
        self,
        checkpoint: Checkpoint,
        *,
        expected_version: int | None = None,
        expected_workflow_id: str | None = None,
    ) -> Checkpoint:
        """Validate a checkpoint against the active runner contract."""
        expected = (
            self.supported_version if expected_version is None else expected_version
        )
        self.validate_version(expected)
        if checkpoint.checkpoint_version != expected:
            raise CheckpointCompatibilityError(
                f"checkpoint version {checkpoint.checkpoint_version} is incompatible; "
                f"expected {expected}"
            )
        if (
            expected_workflow_id is not None
            and checkpoint.workflow_id != expected_workflow_id
        ):
            raise CheckpointCompatibilityError("checkpoint belongs to another workflow")
        return checkpoint

    def restore(
        self,
        workflow_id: str,
        *,
        expected_version: int | None = None,
    ) -> Mapping[str, JsonValue]:
        """Return compatible opaque state for a workflow."""
        checkpoint = self.require(workflow_id)
        self.validate(
            checkpoint,
            expected_version=expected_version,
            expected_workflow_id=workflow_id,
        )
        return _json_object(checkpoint.state)

    def compatible(
        self, workflow_id: str, *, expected_version: int | None = None
    ) -> bool:
        """Return false for absent or incompatible checkpoints."""
        checkpoint = self.get(workflow_id)
        if checkpoint is None:
            return False
        try:
            self.validate(
                checkpoint,
                expected_version=expected_version,
                expected_workflow_id=workflow_id,
            )
        except CheckpointCompatibilityError:
            return False
        return True

    def validate_version(self, version: int) -> None:
        """Reject versions this store cannot restore."""
        if version != self.supported_version:
            raise CheckpointCompatibilityError(
                f"checkpoint version {version} is unsupported; "
                f"supported version is {self.supported_version}"
            )


def _projection(value: Mapping[str, object]) -> _Projection:
    normalized = _json_object(value)
    projection = cast(_Projection, normalized)
    schema = normalized.get("schemaVersion")
    if schema != _SCHEMA_VERSION:
        raise CheckpointError(f"unsupported checkpoint storage schema: {schema!r}")
    checkpoints = projection.get("checkpoints")
    if not isinstance(checkpoints, dict):
        raise CheckpointError("checkpoint projection must contain an object of records")
    return projection


def _checkpoint_from_value(value: object) -> Checkpoint:
    if not isinstance(value, dict):
        raise CheckpointError("checkpoint record must be an object")
    try:
        checkpoint_id = _required_string(value, "checkpointId")
        workflow_id = _required_string(value, "workflowId")
        version = value["checkpointVersion"]
        intent_epoch = value["intentEpoch"]
        saved_at = _required_string(value, "savedAt")
        state = value["state"]
    except KeyError as error:
        raise CheckpointError(f"checkpoint is missing {error.args[0]!r}") from error
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or not isinstance(intent_epoch, int)
        or isinstance(intent_epoch, bool)
        or intent_epoch < 0
    ):
        raise CheckpointError("checkpoint version and intent epoch must be integers")
    if not isinstance(state, dict):
        raise CheckpointError("checkpoint state must be a JSON object")
    try:
        normalized_state = _json_object(state)
    except TypeError as error:
        raise CheckpointError("checkpoint state is invalid") from error
    return Checkpoint(
        checkpoint_id=checkpoint_id,
        workflow_id=workflow_id,
        checkpoint_version=version,
        intent_epoch=intent_epoch,
        state=normalized_state,
        saved_at=saved_at,
    )


def _json_object(value: object) -> JsonObject:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        decoded: object = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise TypeError("checkpoint values must be finite JSON values") from error
    if not isinstance(decoded, dict) or not _is_json_value(decoded):
        raise TypeError("checkpoint values must be a JSON object")
    return cast(JsonObject, decoded)


def _is_json_value(value: object) -> bool:
    if value is None or isinstance(value, (bool, int, str)):
        return True
    if isinstance(value, float):
        return value == value and value not in (float("inf"), float("-inf"))
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _is_json_value(item) for key, item in value.items()
        )
    return False


def _canonical_json(value: JsonValue | Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _required_string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise CheckpointError(f"checkpoint field {key!r} must be a non-empty string")
    return item


def _validate_identifier(value: str, name: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError(f"{name} must be a non-empty safe string")


def _token(value: object) -> str:
    if isinstance(value, bool):
        raise ValueError("token factory returned a boolean")
    token = str(value).strip()
    if not token or any(ord(char) < 32 for char in token):
        raise ValueError("token factory returned an invalid token")
    return token


def _timestamp(clock: Callable[[], datetime]) -> str:
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("checkpoint clock must return a timezone-aware timestamp")
    return now.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "Checkpoint",
    "CheckpointCompatibilityError",
    "CheckpointError",
    "CheckpointNotFound",
    "CheckpointStorage",
    "CheckpointStore",
    "JsonCheckpointStorage",
    "MemoryCheckpointStorage",
    "JsonObject",
    "JsonValue",
]
