"""Recorder configuration and record-kind enums."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

from harness.core.clock import Clock
from harness.core.types.common import text


class ActionKind(StrEnum):
    """Why a strategy produced one recorded intent."""

    SUBMIT = "submit"
    CANCEL = "cancel"
    REPLACE = "replace"
    HOLD = "hold"
    CUSTOM = "custom"


class TransitionReason(StrEnum):
    """What moved a position from one state to the next."""

    FILL = "fill"
    MARK = "mark"
    SNAPSHOT = "snapshot"
    CLOSE = "close"


class MarkerScope(StrEnum):
    """Kind of recorded state marker."""

    RUN = "run"
    STATE = "state"
    POSITION = "position"
    NOTE = "note"


def _key_text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{path} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True, slots=True)
class RecorderConfig:
    """Immutable configuration for one recorded run."""

    run_id: str
    directory: str | Path
    seed: str | int | bytes = "harness-run-recorder"
    batch_size: int = 4096
    record_events: bool = False
    record_payloads: bool = True
    compression: str = "zstd"
    parquet_version: str = "2.6"
    clock: Clock | None = None
    strategy_id: str | None = None
    book_id: str | None = None
    account_id: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", text(self.run_id, "run_id"))
        object.__setattr__(self, "directory", Path(self.directory))
        if isinstance(self.batch_size, bool) or not isinstance(self.batch_size, int):
            raise TypeError("batch_size must be a positive integer")
        if self.batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        if not isinstance(self.compression, str) or not self.compression.strip():
            raise ValueError("compression must be a non-empty string")
        object.__setattr__(self, "compression", self.compression.strip())
        if (
            not isinstance(self.parquet_version, str)
            or not self.parquet_version.strip()
        ):
            raise ValueError("parquet_version must be a non-empty string")
        object.__setattr__(self, "parquet_version", self.parquet_version.strip())
        if self.clock is not None and not isinstance(self.clock, Clock):
            raise TypeError("clock must implement Clock")
        if self.strategy_id is not None:
            object.__setattr__(
                self, "strategy_id", text(self.strategy_id, "strategy_id")
            )
        if self.book_id is not None:
            object.__setattr__(self, "book_id", text(self.book_id, "book_id"))
        if self.account_id is not None:
            object.__setattr__(self, "account_id", text(self.account_id, "account_id"))
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(
                {
                    _key_text(key, "metadata key"): str(value)
                    for key, value in self.metadata.items()
                }
            ),
        )
