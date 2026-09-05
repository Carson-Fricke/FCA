"""Durable runtime-state snapshots and restores for the paper runtime.

The store captures the full restorable state of a :class:`~harness.runtime.
paper.PaperShell` at deterministic points — periodic, session-end, and stop —
as a frozen, canonical, hash-verifiable :class:`RuntimeSnapshot`, and restores
it exactly into a fresh :class:`~harness.portfolio.PortfolioHub` or
:class:`~harness.runtime.paper.PaperShell`.  A restarted runtime resumes from
the restored state with accounting books, marks, order attribution, risk-gate
inputs, strategy state, random sources, and the event watermark identical to
the original runtime at the capture point.

Snapshot identity is the domain-separated SHA-256 hash of the canonical
serialization (the repository's ``harness.configuration.hashing`` convention),
so identity carries no wall-clock input and equal states hash equally.  The
format is a cross-language contract (epic T181): every record below carries an
explicit, pinned JSON encoding that must round-trip across the future Zig FFI
boundary unchanged.

Persistence goes through the injected :class:`SnapshotObjectStore` port with
whole-object replacement semantics, matching the lake storage ports.  A save
writes the immutable, content-addressed payload object first, verifies it by
reading it back, and only then atomically flips the ``latest`` pointer, so an
interrupted write can never replace the last valid snapshot.  An optional
local mirror receives the same objects and serves restores when the primary
deployment blob store is unavailable or corrupt.

The synchronous kernel surface here reads no wall time, draws no randomness,
and performs no network I/O.
"""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from random import Random
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable

from harness.configuration import TraderConfig
from harness.configuration.hashing import canonical_configuration_bytes
from harness.core.clock import Clock
from harness.core.symbols import Symbol
from harness.core.types import Fill, Side
from harness.core.types.common import exact_decimal, text
from harness.fills import AssetClass, RemainderPolicy
from harness.portfolio import DuplicateFillError, PortfolioHub, PositionMarker
from harness.runtime.backtest.errors import (
    AbortPolicy,
    FillModel,
    StrategyHaltPolicy,
    TeardownPolicy,
)
from harness.runtime.backtest.fills import FillModelOptions
from harness.runtime.paper import (
    PaperOptions,
    PaperSessionPolicy,
    PaperShell,
)
from lake.contracts import ExchangeSession

FORMAT_VERSION: Final = 1

_SNAPSHOT_DOMAIN: Final = b"fca:harness-runtime-state-snapshot:v1\0"
_MICROSECOND: Final = timedelta(microseconds=1)
_ID_CHARACTERS: Final = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)
_REJECT_POLICIES: Final = frozenset({"reject_order", "halt_strategy", "halt_runtime"})


class SnapshotTrigger(StrEnum):
    """Deterministic capture points for one runtime snapshot."""

    PERIODIC = "periodic"
    SESSION_END = "session_end"
    STOP = "stop"


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class StateStoreError(RuntimeError):
    """Base class for runtime state-store failures."""


class StateSnapshotMissingError(StateStoreError, LookupError):
    """Raised when no valid snapshot is stored for the run."""


class StateSnapshotVersionError(StateStoreError):
    """Raised when a stored snapshot or pointer has an unsupported version."""


class StateSnapshotIntegrityError(StateStoreError):
    """Raised when stored snapshot bytes fail their canonical hash checks."""


class StateSnapshotWriteError(StateStoreError):
    """Raised when a snapshot payload cannot be persisted verifiably."""


class StateSnapshotRestoreError(StateStoreError):
    """Raised when a snapshot cannot be restored into a consistent runtime."""


# --------------------------------------------------------------------------
# Storage port
# --------------------------------------------------------------------------


@runtime_checkable
class SnapshotObjectStore(Protocol):
    """Whole-object storage boundary for snapshot payloads and pointers.

    ``replace`` has whole-object semantics: a successful call makes the
    supplied payload the complete contents at ``relative_path``.  ``read``
    returns the full payload and raises ``KeyError`` or ``OSError`` when the
    object is absent.  These signatures match the lake
    :class:`~lake.storage.parquet.ObjectStore` and
    :class:`~lake.storage.ports.ReadableBlobStore` ports, so lake adapters plug
    in directly.
    """

    def replace(self, relative_path: str, payload: bytes) -> None:
        """Atomically replace the object at a repository-relative path."""
        ...

    def read(self, relative_path: str) -> bytes:
        """Return the full payload of one object."""
        ...


# --------------------------------------------------------------------------
# Canonical value normalization and decoding
# --------------------------------------------------------------------------


def _float_bits(value: float) -> str:
    """Return the exact big-endian IEEE-754 bit pattern of one float."""
    return struct.pack(">d", value).hex()


def _float_from_bits(text_value: str) -> float:
    """Return the float whose exact bit pattern is the given hex text."""
    try:
        raw = bytes.fromhex(text_value)
    except ValueError as error:
        raise StateSnapshotIntegrityError(
            f"malformed float bit pattern {text_value!r}"
        ) from error
    if len(raw) != 8:
        raise StateSnapshotIntegrityError(
            f"float bit pattern {text_value!r} must encode 8 bytes"
        )
    return struct.unpack(">d", raw)[0]


def _freeze(value: object, path: str) -> object:
    """Convert snapshot values into their typed canonical representation.

    Typed canonical values keep exact scalars (``Decimal``, timezone-aware
    ``datetime``, ``date``, ``timedelta``) and demote enums to their values,
    dataclasses to plain mappings, and mappings/sequences to plain
    ``dict``/``list`` containers, so two snapshots of equal state carry
    structurally equal in-memory values.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{path} must be timezone-aware")
        return value.astimezone(UTC)
    if value is None or isinstance(
        value, str | bool | int | Decimal | date | timedelta
    ):
        return value
    if isinstance(value, float):
        raise TypeError(f"{path} must not contain binary floating point")
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise TypeError(f"{path} mapping keys must be non-empty strings")
            frozen[key] = _freeze(item, f"{path}.{key}")
        return frozen
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _freeze(getattr(value, field.name), f"{path}.{field.name}")
            for field in fields(value)
        }
    if isinstance(value, list | tuple):
        return [_freeze(item, f"{path}[{index}]") for index, item in enumerate(value)]
    raise TypeError(f"{path} contains unsupported {type(value).__name__}")


def _normalize(value: object, path: str) -> object:
    """Normalize snapshot values into canonical, tag-encodable data."""
    if isinstance(value, timedelta):
        return {"$timedelta_us": value // _MICROSECOND}
    return _freeze(value, path)


def _decode(value: object) -> object:
    """Decode one canonical tagged value back into its typed scalar."""
    if isinstance(value, Mapping) and len(value) == 1:
        if "$decimal" in value:
            return Decimal(str(value["$decimal"]))
        if "$datetime" in value:
            stamped = str(value["$datetime"]).replace("Z", "+00:00")
            return datetime.fromisoformat(stamped)
        if "$date" in value:
            return date.fromisoformat(str(value["$date"]))
        if "$timedelta_us" in value:
            microseconds = _integer(value["$timedelta_us"], "$timedelta_us")
            return timedelta(microseconds=microseconds)
        if "$float_bits" in value:
            return _float_from_bits(str(value["$float_bits"]))
    if isinstance(value, Mapping):
        return {str(key): _decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StateSnapshotIntegrityError(f"{name} must be an integer")
    return value


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise StateSnapshotIntegrityError(f"{name} must be a string-keyed mapping")
    return value


def _sequence(value: object, name: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(
        value, (list, tuple)
    ):
        raise StateSnapshotIntegrityError(f"{name} must be a sequence")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise StateSnapshotIntegrityError(f"{name} must be a string")
    return value


def _nonnegative_integer(value: object, name: str) -> int:
    value = _integer(value, name)
    if value < 0:
        raise StateSnapshotIntegrityError(f"{name} must not be negative")
    return value


def _decimal(value: object, name: str) -> Decimal:
    try:
        return exact_decimal(value, name)
    except (TypeError, ValueError) as error:
        raise StateSnapshotIntegrityError(
            f"{name} must be an exact decimal value"
        ) from error


def _as_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    return value


def _as_optional_datetime(value: object, name: str) -> datetime | None:
    return None if value is None else _as_datetime(value, name)


def _as_date(value: object, name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError(f"{name} must be a date")
    return value


def _as_timedelta(value: object, name: str) -> timedelta:
    if not isinstance(value, timedelta):
        raise TypeError(f"{name} must be a timedelta")
    return value


def _require_digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise StateSnapshotIntegrityError(f"{name} must be a SHA-256 digest")
    return value


def _safe_token(value: str) -> str:
    if not value or any(character not in _ID_CHARACTERS for character in value):
        raise ValueError(
            f"run identifier {value!r} must contain only letters, digits, "
            "'_', '-', and '.'"
        )
    if value.startswith("."):
        raise ValueError(f"run identifier {value!r} is not a safe path token")
    return value


def _read_object(store: SnapshotObjectStore, key: str) -> bytes:
    try:
        return store.read(key)
    except (KeyError, OSError) as error:
        raise StateSnapshotMissingError(
            f"object {key!r} is not available in the snapshot store"
        ) from error


def _load_json(raw: bytes, key: str) -> object:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StateSnapshotIntegrityError(
            f"object {key!r} is not valid JSON"
        ) from error


# --------------------------------------------------------------------------
# Stored pointer
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredSnapshot:
    """Version-checked pointer to one persisted snapshot payload."""

    format_version: int
    run_id: str
    snapshot_id: str
    payload_sha256: str
    byte_length: int

    def __post_init__(self) -> None:
        if self.format_version != FORMAT_VERSION:
            raise StateSnapshotVersionError(
                f"snapshot format version {self.format_version} is not "
                f"supported; this build reads version {FORMAT_VERSION}"
            )
        text(self.run_id, "run_id")
        _require_digest(self.snapshot_id, "snapshot_id")
        _require_digest(self.payload_sha256, "payload_sha256")
        _nonnegative_integer(self.byte_length, "byte_length")
        if self.byte_length <= 0:
            raise StateSnapshotIntegrityError("byte_length must be positive")

    def to_json(self) -> dict[str, object]:
        """Return the canonical JSON-ready pointer record."""
        return {
            "format_version": self.format_version,
            "run_id": self.run_id,
            "snapshot_id": self.snapshot_id,
            "payload_sha256": self.payload_sha256,
            "byte_length": self.byte_length,
        }

    @classmethod
    def from_json(cls, data: object) -> StoredSnapshot:
        """Rebuild a pointer from parsed canonical JSON."""
        values = _mapping(data, "snapshot pointer")
        try:
            return cls(
                format_version=_integer(values.get("format_version"), "format_version"),
                run_id=text(_string(values.get("run_id"), "run_id"), "run_id"),
                snapshot_id=_require_digest(values.get("snapshot_id"), "snapshot_id"),
                payload_sha256=_require_digest(
                    values.get("payload_sha256"), "payload_sha256"
                ),
                byte_length=_integer(values.get("byte_length"), "byte_length"),
            )
        except StateSnapshotVersionError:
            raise
        except StateSnapshotIntegrityError:
            raise
        except (TypeError, ValueError) as error:
            raise StateSnapshotIntegrityError(
                f"snapshot pointer is malformed: {error}"
            ) from error


# --------------------------------------------------------------------------
# Snapshot records
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RandomState:
    """Exact serialized state of one seeded ``random.Random`` source."""

    instance_id: str
    version: int
    internal: tuple[int, ...]
    gauss_next: float | None

    def __post_init__(self) -> None:
        text(self.instance_id, "instance_id")
        _nonnegative_integer(self.version, "random state version")
        if not isinstance(self.internal, tuple) or not all(
            isinstance(item, int) and not isinstance(item, bool)
            for item in self.internal
        ):
            raise StateSnapshotIntegrityError(
                "random internal state must be a tuple of integers"
            )
        if self.gauss_next is not None and not isinstance(self.gauss_next, float):
            raise StateSnapshotIntegrityError(
                "random gauss state must be a float or None"
            )

    @classmethod
    def from_random(cls, instance_id: str, source: Random) -> RandomState:
        """Capture the exact state of one seeded random source."""
        if not isinstance(source, Random):
            raise TypeError("source must be a random.Random instance")
        state = source.getstate()
        if not isinstance(state, tuple) or len(state) != 3:
            raise StateSnapshotIntegrityError(
                "random.getstate() returned an unexpected structure"
            )
        version, internal, gauss_next = state
        if not isinstance(internal, tuple):
            raise StateSnapshotIntegrityError("random internal state must be a tuple")
        return cls(
            instance_id=instance_id,
            version=_integer(version, "random state version"),
            internal=tuple(
                _nonnegative_integer(item, "random internal state") for item in internal
            ),
            gauss_next=None if gauss_next is None else float(gauss_next),
        )

    def apply(self, target: Random) -> None:
        """Restore this exact state into one random source."""
        if not isinstance(target, Random):
            raise TypeError("target must be a random.Random instance")
        target.setstate((self.version, tuple(self.internal), self.gauss_next))

    def to_json(self) -> dict[str, object]:
        """Return the canonical JSON-ready random-state record."""
        gauss: dict[str, object] | None = None
        if self.gauss_next is not None:
            gauss = {"$float_bits": _float_bits(self.gauss_next)}
        return {
            "instance_id": self.instance_id,
            "version": self.version,
            "internal": list(self.internal),
            "gauss_next": gauss,
        }

    @classmethod
    def from_json(cls, data: object) -> RandomState:
        """Rebuild one random-state record from parsed canonical JSON."""
        values = _mapping(data, "random state")
        gauss = values.get("gauss_next")
        if gauss is not None:
            gauss = _decode(gauss)
            if not isinstance(gauss, float):
                raise StateSnapshotIntegrityError(
                    "random gauss state must decode to a float"
                )
        internal = _sequence(values.get("internal"), "random internal state")
        return cls(
            instance_id=text(
                _string(values.get("instance_id"), "instance_id"), "instance_id"
            ),
            version=_nonnegative_integer(values.get("version"), "random version"),
            internal=tuple(
                _nonnegative_integer(item, "random internal state") for item in internal
            ),
            gauss_next=gauss,
        )


@dataclass(frozen=True, slots=True)
class StrategyState:
    """One strategy's frozen ``StateStore`` values at the capture point."""

    instance_id: str
    values: Mapping[str, object]

    def __post_init__(self) -> None:
        text(self.instance_id, "instance_id")
        values: dict[str, object] = {}
        for key, value in self.values.items():
            if not isinstance(key, str) or not key.strip():
                raise StateSnapshotIntegrityError(
                    "strategy state keys must be non-empty strings"
                )
            values[key.strip()] = _freeze(value, f"strategy state {key!r}")
        object.__setattr__(self, "values", MappingProxyType(values))

    def to_json(self) -> dict[str, object]:
        """Return the canonical JSON-ready strategy-state record."""
        return {
            "instance_id": self.instance_id,
            "values": {
                key: _normalize(item, f"strategy state {key!r}")
                for key, item in self.values.items()
            },
        }

    @classmethod
    def from_json(cls, data: object) -> StrategyState:
        """Rebuild one strategy-state record from parsed canonical JSON."""
        values = _mapping(data, "strategy state")
        state = _mapping(values.get("values"), "strategy state values")
        return cls(
            instance_id=text(
                _string(values.get("instance_id"), "instance_id"), "instance_id"
            ),
            values={key: _decode(item) for key, item in state.items()},
        )


@dataclass(frozen=True, slots=True)
class BookState:
    """One virtual book's exact restorable accounting state.

    Book ledgers are restored by replaying the captured booked fills in
    application order, which reproduces cash, positions, cost bases, cash
    flows, realized PnL, and fill attribution exactly.
    """

    book_id: str
    initial_cash: Decimal
    cash: Decimal
    fill_ids: tuple[str, ...]
    fills: tuple[Fill, ...]
    markers: tuple[PositionMarker, ...]

    def __post_init__(self) -> None:
        text(self.book_id, "book_id")
        object.__setattr__(
            self, "initial_cash", exact_decimal(self.initial_cash, "initial_cash")
        )
        object.__setattr__(self, "cash", exact_decimal(self.cash, "cash"))
        fill_ids = tuple(self.fill_ids)
        fills = tuple(self.fills)
        if len(fill_ids) != len(fills):
            raise StateSnapshotIntegrityError(
                "book fill identifiers and fills must have equal length"
            )
        for fill_id, fill in zip(fill_ids, fills, strict=True):
            if fill_id != fill.fill_id:
                raise StateSnapshotIntegrityError(
                    "book fill identifiers must match the captured fills in order"
                )
        object.__setattr__(self, "fill_ids", fill_ids)
        object.__setattr__(self, "fills", fills)
        markers = tuple(sorted(self.markers, key=lambda marker: str(marker.symbol)))
        seen: set[str] = set()
        for marker in markers:
            symbol = str(marker.symbol)
            if symbol in seen:
                raise StateSnapshotIntegrityError(
                    f"book {self.book_id!r} captures a duplicate marker for {symbol!r}"
                )
            seen.add(symbol)
        object.__setattr__(self, "markers", markers)

    def to_json(self) -> dict[str, object]:
        """Return the canonical JSON-ready book record."""
        return {
            "book_id": self.book_id,
            "initial_cash": self.initial_cash,
            "cash": self.cash,
            "fill_ids": list(self.fill_ids),
            "fills": [_fill_json(fill) for fill in self.fills],
            "markers": [
                {"symbol": str(marker.symbol), "price": marker.price}
                for marker in self.markers
            ],
        }

    @classmethod
    def from_json(cls, data: object) -> BookState:
        """Rebuild one book record from parsed canonical JSON."""
        values = {
            key: _decode(item) for key, item in _mapping(data, "book state").items()
        }
        return cls(
            book_id=text(_string(values.get("book_id"), "book_id"), "book_id"),
            initial_cash=_decimal(values.get("initial_cash"), "initial_cash"),
            cash=_decimal(values.get("cash"), "cash"),
            fill_ids=tuple(
                text(_string(item, "fill id"), "fill id")
                for item in _sequence(values.get("fill_ids"), "fill_ids")
            ),
            fills=tuple(
                _fill_from_json(item)
                for item in _sequence(values.get("fills"), "book fills")
            ),
            markers=tuple(
                _marker_from_json(item)
                for item in _sequence(values.get("markers"), "book markers")
            ),
        )


@dataclass(frozen=True, slots=True)
class RiskState:
    """Risk-gate identity and per-strategy gate inputs at the capture point.

    The pipeline itself is stateless configuration in this repository; its
    restorable identity is the rejection policy and configured rule order,
    while the per-strategy gate inputs (exact daily loss) are captured
    alongside the books they derive from.
    """

    on_reject: str
    rule_ids: tuple[str, ...]
    daily_losses: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        text(self.on_reject, "on_reject")
        if self.on_reject not in _REJECT_POLICIES:
            raise StateSnapshotIntegrityError(
                f"unsupported risk rejection policy {self.on_reject!r}"
            )
        for rule_id in self.rule_ids:
            text(rule_id, "rule_id")
        losses: dict[str, Decimal] = {}
        for key, value in self.daily_losses.items():
            if not isinstance(key, str) or not key.strip():
                raise StateSnapshotIntegrityError(
                    "daily loss keys must be non-empty strings"
                )
            losses[key.strip()] = exact_decimal(value, "daily loss")
        object.__setattr__(
            self, "daily_losses", MappingProxyType(dict(sorted(losses.items())))
        )

    def to_json(self) -> dict[str, object]:
        """Return the canonical JSON-ready risk record."""
        return {
            "on_reject": self.on_reject,
            "rule_ids": list(self.rule_ids),
            "daily_losses": dict(self.daily_losses),
        }

    @classmethod
    def from_json(cls, data: object) -> RiskState:
        """Rebuild one risk record from parsed canonical JSON."""
        values = _mapping(data, "risk state")
        losses = _mapping(values.get("daily_losses"), "daily losses")
        return cls(
            on_reject=_string(values.get("on_reject"), "on_reject"),
            rule_ids=tuple(
                text(_string(item, "rule id"), "rule_id")
                for item in _sequence(values.get("rule_ids"), "risk rule ids")
            ),
            daily_losses={
                key: _decimal(_decode(value), f"daily loss {key!r}")
                for key, value in losses.items()
            },
        )


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    """Frozen, canonical, hash-verifiable full runtime state.

    ``snapshot_id`` is the domain-separated SHA-256 of the canonical
    serialization; two captures of equal runtime state produce the identical
    identifier, and the identity carries no wall-clock input.  The structure
    is a cross-language contract (epic T181) pinned by the explicit JSON
    encoding below.
    """

    format_version: int
    run_id: str
    trigger: SnapshotTrigger
    sequence: int
    dispatched_count: int
    market_event_count: int
    options: Mapping[str, object]
    books: tuple[BookState, ...]
    prices: Mapping[str, Decimal]
    order_counts: Mapping[str, int]
    order_owners: Mapping[str, str]
    strategy_state: tuple[StrategyState, ...]
    random_states: tuple[RandomState, ...]
    risk: RiskState

    def __post_init__(self) -> None:
        if self.format_version != FORMAT_VERSION:
            raise StateSnapshotVersionError(
                f"snapshot format version {self.format_version} is not "
                f"supported; this build writes version {FORMAT_VERSION}"
            )
        text(self.run_id, "run_id")
        if not isinstance(self.trigger, SnapshotTrigger):
            raise StateSnapshotIntegrityError("trigger must be a SnapshotTrigger")
        _nonnegative_integer(self.sequence, "sequence")
        _nonnegative_integer(self.dispatched_count, "dispatched_count")
        _nonnegative_integer(self.market_event_count, "market_event_count")
        known = {field.name for field in fields(PaperOptions)}
        options: dict[str, object] = {}
        for key, value in self.options.items():
            if not isinstance(key, str) or not key.strip():
                raise StateSnapshotIntegrityError(
                    "option keys must be non-empty strings"
                )
            if key not in known:
                raise StateSnapshotIntegrityError(f"unknown runtime option {key!r}")
            options[key] = value
        if _string(options.get("run_id", ""), "options.run_id") != self.run_id:
            raise StateSnapshotIntegrityError(
                "runtime options must carry the snapshot run identifier"
            )
        object.__setattr__(self, "options", MappingProxyType(options))
        book_ids = tuple(book.book_id for book in self.books)
        if len(set(book_ids)) != len(book_ids):
            raise StateSnapshotIntegrityError("books must have unique identifiers")
        prices: dict[str, Decimal] = {}
        for key, value in self.prices.items():
            if not isinstance(key, str) or not key.strip():
                raise StateSnapshotIntegrityError(
                    "price keys must be non-empty strings"
                )
            prices[key.strip()] = exact_decimal(value, f"price {key!r}")
        object.__setattr__(
            self, "prices", MappingProxyType(dict(sorted(prices.items())))
        )
        counts: dict[str, int] = {}
        for key, value in self.order_counts.items():
            if not isinstance(key, str) or not key.strip():
                raise StateSnapshotIntegrityError(
                    "order count keys must be non-empty strings"
                )
            _nonnegative_integer(value, f"order count {key!r}")
            counts[key.strip()] = value
        object.__setattr__(
            self, "order_counts", MappingProxyType(dict(sorted(counts.items())))
        )
        owners: dict[str, str] = {}
        for key, value in self.order_owners.items():
            if not isinstance(key, str) or not key.strip():
                raise StateSnapshotIntegrityError(
                    "order owner keys must be non-empty strings"
                )
            owners[key.strip()] = text(value, f"order owner {key!r}")
        object.__setattr__(
            self, "order_owners", MappingProxyType(dict(sorted(owners.items())))
        )
        object.__setattr__(
            self,
            "strategy_state",
            tuple(sorted(self.strategy_state, key=lambda item: item.instance_id)),
        )
        object.__setattr__(
            self,
            "random_states",
            tuple(sorted(self.random_states, key=lambda item: item.instance_id)),
        )
        strategy_ids = tuple(item.instance_id for item in self.strategy_state)
        if len(set(strategy_ids)) != len(strategy_ids):
            raise StateSnapshotIntegrityError(
                "strategy state must have unique instance identifiers"
            )
        random_ids = tuple(item.instance_id for item in self.random_states)
        if len(set(random_ids)) != len(random_ids):
            raise StateSnapshotIntegrityError(
                "random states must have unique instance identifiers"
            )
        if not isinstance(self.risk, RiskState):
            raise StateSnapshotIntegrityError("risk must be a RiskState record")

    # -- canonical identity ---------------------------------------------------

    def to_json(self) -> dict[str, object]:
        """Return the canonical JSON-ready snapshot content."""
        return {
            "format_version": self.format_version,
            "run_id": self.run_id,
            "trigger": self.trigger.value,
            "sequence": self.sequence,
            "dispatched_count": self.dispatched_count,
            "market_event_count": self.market_event_count,
            "options": {
                key: _normalize(value, f"options.{key}")
                for key, value in self.options.items()
            },
            "books": [book.to_json() for book in self.books],
            "prices": dict(self.prices),
            "order_counts": dict(self.order_counts),
            "order_owners": dict(self.order_owners),
            "strategy_state": [item.to_json() for item in self.strategy_state],
            "random_states": [item.to_json() for item in self.random_states],
            "risk": self.risk.to_json(),
        }

    @property
    def snapshot_id(self) -> str:
        """Return the domain-separated SHA-256 identity of this snapshot."""
        return hashlib.sha256(self.canonical_bytes).hexdigest()

    @property
    def canonical_bytes(self) -> bytes:
        """Return the canonical UTF-8 bytes the snapshot identity hashes."""
        return _SNAPSHOT_DOMAIN + canonical_configuration_bytes(self.to_json())

    def payload_bytes(self) -> bytes:
        """Return the canonical stored envelope for this snapshot."""
        return canonical_configuration_bytes(
            {
                "snapshot_id": self.snapshot_id,
                "snapshot": self.to_json(),
            }
        )

    @classmethod
    def from_json(cls, data: object) -> RuntimeSnapshot:
        """Rebuild one snapshot from parsed canonical JSON."""
        values = _mapping(data, "snapshot")
        trigger = _string(values.get("trigger"), "trigger")
        if trigger not in tuple(item.value for item in SnapshotTrigger):
            raise StateSnapshotIntegrityError(f"unknown snapshot trigger {trigger!r}")
        prices = _mapping(values.get("prices"), "prices")
        counts = _mapping(values.get("order_counts"), "order counts")
        owners = _mapping(values.get("order_owners"), "order owners")
        options = _mapping(values.get("options"), "options")
        return cls(
            format_version=_nonnegative_integer(
                values.get("format_version"), "format_version"
            ),
            run_id=text(_string(values.get("run_id"), "run_id"), "run_id"),
            trigger=SnapshotTrigger(trigger),
            sequence=_nonnegative_integer(values.get("sequence"), "sequence"),
            dispatched_count=_nonnegative_integer(
                values.get("dispatched_count"), "dispatched_count"
            ),
            market_event_count=_nonnegative_integer(
                values.get("market_event_count"), "market_event_count"
            ),
            options={key: _decode(item) for key, item in options.items()},
            books=tuple(
                BookState.from_json(item)
                for item in _sequence(values.get("books"), "books")
            ),
            prices={
                key: _decimal(_decode(value), f"price {key!r}")
                for key, value in prices.items()
            },
            order_counts={
                key: _nonnegative_integer(value, f"order count {key!r}")
                for key, value in counts.items()
            },
            order_owners={
                text(_string(key, "order id"), "order id"): text(
                    _string(item, "order owner"), "order owner"
                )
                for key, item in owners.items()
            },
            strategy_state=tuple(
                StrategyState.from_json(item)
                for item in _sequence(values.get("strategy_state"), "strategy state")
            ),
            random_states=tuple(
                RandomState.from_json(item)
                for item in _sequence(values.get("random_states"), "random states")
            ),
            risk=RiskState.from_json(values.get("risk")),
        )

    # -- runtime reconstruction -----------------------------------------------

    def paper_options(self) -> PaperOptions:
        """Rebuild the runtime options captured with this snapshot.

        The non-deterministic ``idle_pace`` pacing callback is not state and
        is not captured; the rebuilt options carry ``idle_pace=None``.
        """
        values = {key: _decode(item) for key, item in self.options.items()}
        rebuilt_fill_options: FillModelOptions | None = None
        fill_options = values.get("fill_options")
        if fill_options is not None:
            options = _mapping(fill_options, "fill_options")
            rebuilt_fill_options = FillModelOptions(
                response_latency=_as_timedelta(
                    options.get("response_latency"), "fill_options.response_latency"
                ),
                remainder_policy=RemainderPolicy(
                    _string(options.get("remainder_policy"), "remainder_policy")
                ),
                require_trade_through=_bool(
                    options.get("require_trade_through"), "require_trade_through"
                ),
                minimum_trade_through=_decimal(
                    options.get("minimum_trade_through"), "minimum_trade_through"
                ),
                strict=_bool(options.get("strict"), "strict"),
            )
        rebuilt_session: ExchangeSession | None = None
        session = values.get("session")
        if session is not None:
            session_values = _mapping(session, "session")
            rebuilt_session = ExchangeSession(
                session_date=_as_date(
                    session_values.get("session_date"), "session_date"
                ),
                open_time=_as_datetime(session_values.get("open_time"), "open_time"),
                close_time=_as_datetime(session_values.get("close_time"), "close_time"),
            )
        try:
            return PaperOptions(
                run_id=text(_string(values.get("run_id"), "run_id"), "run_id"),
                initial_cash=_decimal(values.get("initial_cash"), "initial_cash"),
                fill_model=FillModel(_string(values.get("fill_model"), "fill_model")),
                fill_options=rebuilt_fill_options,
                fee_precision=_decimal(values.get("fee_precision"), "fee_precision"),
                asset_class=AssetClass(
                    _string(values.get("asset_class"), "asset_class")
                ),
                history_capacity=_nonnegative_integer(
                    values.get("history_capacity"), "history_capacity"
                ),
                abort_policy=AbortPolicy(
                    _string(values.get("abort_policy"), "abort_policy")
                ),
                strategy_halt_policy=StrategyHaltPolicy(
                    _string(values.get("strategy_halt_policy"), "strategy_halt_policy")
                ),
                teardown_policy=TeardownPolicy(
                    _string(values.get("teardown_policy"), "teardown_policy")
                ),
                extra_latency=_as_timedelta(
                    values.get("extra_latency"), "extra_latency"
                ),
                session_policy=PaperSessionPolicy(
                    _string(values.get("session_policy"), "session_policy")
                ),
                session=rebuilt_session,
                start=_as_optional_datetime(values.get("start"), "start"),
            )
        except StateSnapshotRestoreError:
            raise
        except (TypeError, ValueError) as error:
            raise StateSnapshotRestoreError(
                f"captured runtime options cannot be rebuilt: {error}"
            ) from error

    @property
    def book_ids(self) -> tuple[str, ...]:
        """Return captured book identifiers in registration order."""
        return tuple(book.book_id for book in self.books)


@dataclass(frozen=True, slots=True)
class SnapshotSchedule:
    """Deterministic periodic capture cadence over admitted market events."""

    every_events: int

    def __post_init__(self) -> None:
        if isinstance(self.every_events, bool) or not isinstance(
            self.every_events, int
        ):
            raise ValueError("every_events must be an integer")
        if self.every_events <= 0:
            raise ValueError("every_events must be positive")

    def due(self, market_event_count: int) -> bool:
        """Return whether a periodic snapshot is due after this many events."""
        if market_event_count < 0:
            raise ValueError("market_event_count must not be negative")
        return market_event_count > 0 and market_event_count % self.every_events == 0


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


def _payload_key(run_id: str, snapshot_id: str) -> str:
    return f"runs/{run_id}/snapshots/{snapshot_id}.json"


def _pointer_key(run_id: str) -> str:
    return f"runs/{run_id}/latest.json"


class RuntimeStateStore:
    """Persist, verify, and restore canonical runtime state snapshots.

    The store is bound to one deployment run identifier and writes every
    object below that run's prefix in the injected primary store, plus an
    optional local mirror.  Saves are verify-then-commit: the content-addressed
    payload is written and read back on every store before the small
    ``latest`` pointer object is atomically replaced, so an interrupted or
    torn write raises and leaves the previous valid snapshot in place.
    """

    __slots__ = ("_run_id", "_stores")

    def __init__(
        self,
        store: SnapshotObjectStore,
        *,
        run_id: str,
        mirror: SnapshotObjectStore | None = None,
    ) -> None:
        if not isinstance(store, SnapshotObjectStore):
            raise TypeError("store must implement SnapshotObjectStore")
        if mirror is not None and not isinstance(mirror, SnapshotObjectStore):
            raise TypeError("mirror must implement SnapshotObjectStore")
        self._run_id = _safe_token(text(run_id, "run_id"))
        stores: list[SnapshotObjectStore] = [store]
        if mirror is not None:
            stores.append(mirror)
        self._stores = tuple(stores)

    @property
    def run_id(self) -> str:
        """Return the deployment run identifier this store is bound to."""
        return self._run_id

    # -- capture ---------------------------------------------------------------

    def capture(
        self,
        shell: PaperShell,
        *,
        options: PaperOptions,
        strategy_ids: Sequence[str],
        trigger: SnapshotTrigger,
        sequence: int = 0,
        order_owners: Mapping[str, str] | None = None,
        strategy_state: Mapping[str, Mapping[str, object]] | None = None,
        random_sources: Mapping[str, Random] | None = None,
    ) -> RuntimeSnapshot:
        """Capture the full restorable runtime state of one paper shell.

        Everything is read through the shell's public supervision surface: the
        portfolio hub and its virtual books, the latest marks, per-strategy
        order counts and daily losses, the risk pipeline identity, and the
        in-flight session result (dispatch watermark and booked fills).  The
        capture point is deterministic: no wall-clock value enters the
        snapshot.
        """
        if not isinstance(shell, PaperShell):
            raise TypeError("shell must be a PaperShell")
        if not isinstance(options, PaperOptions):
            raise TypeError("options must be a PaperOptions")
        if not isinstance(trigger, SnapshotTrigger):
            raise TypeError("trigger must be a SnapshotTrigger")
        if shell.run_id != self._run_id:
            raise StateStoreError(
                f"shell run {shell.run_id!r} does not match store run {self._run_id!r}"
            )
        if options.run_id != self._run_id:
            raise StateStoreError(
                f"options run {options.run_id!r} does not match store run "
                f"{self._run_id!r}"
            )
        _nonnegative_integer(sequence, "sequence")
        ids = tuple(dict.fromkeys(strategy_ids))
        if not ids:
            raise ValueError("strategy_ids must not be empty")

        result = shell.result()
        fills_by_id = {fill.fill_id: fill for fill in result.fills}
        books: list[BookState] = []
        for book in shell.hub.books:
            fills: list[Fill] = []
            for fill_id in book.fill_ids:
                fill = fills_by_id.get(fill_id)
                if fill is None:
                    raise StateStoreError(
                        f"book {book.book_id!r} references fill {fill_id!r} that "
                        "the session fill history does not contain"
                    )
                fills.append(fill)
            books.append(
                BookState(
                    book_id=book.book_id,
                    initial_cash=book.initial_cash,
                    cash=book.cash,
                    fill_ids=book.fill_ids,
                    fills=tuple(fills),
                    markers=book.snapshot.markers,
                )
            )
        prices: dict[str, Decimal] = {}
        for symbol, price in shell.prices.items():
            if not isinstance(price, Decimal):
                raise StateStoreError(
                    f"mark for {symbol!r} is not an exact Decimal value"
                )
            prices[str(symbol)] = price
        counts = {instance_id: shell.order_count(instance_id) for instance_id in ids}
        losses = {instance_id: shell.daily_loss(instance_id) for instance_id in ids}
        risk = RiskState(
            on_reject=shell.pipeline.on_reject,
            rule_ids=tuple(_rule_id(rule) for rule in shell.pipeline.rules),
            daily_losses=losses,
        )
        strategies = tuple(
            StrategyState(instance_id=instance_id, values=dict(values))
            for instance_id, values in (strategy_state or {}).items()
        )
        randoms = tuple(
            RandomState.from_random(instance_id, source)
            for instance_id, source in (random_sources or {}).items()
        )
        return RuntimeSnapshot(
            format_version=FORMAT_VERSION,
            run_id=self._run_id,
            trigger=trigger,
            sequence=sequence,
            dispatched_count=result.dispatched_count,
            market_event_count=result.market_event_count,
            options=_options_json(options),
            books=tuple(books),
            prices=prices,
            order_counts=counts,
            order_owners=dict(order_owners or {}),
            strategy_state=strategies,
            random_states=randoms,
            risk=risk,
        )

    # -- persistence -----------------------------------------------------------

    def save(self, snapshot: RuntimeSnapshot) -> StoredSnapshot:
        """Persist one snapshot atomically and return its pointer record."""
        if not isinstance(snapshot, RuntimeSnapshot):
            raise TypeError("snapshot must be a RuntimeSnapshot")
        if snapshot.run_id != self._run_id:
            raise StateStoreError(
                f"snapshot run {snapshot.run_id!r} does not match store run "
                f"{self._run_id!r}"
            )
        payload = snapshot.payload_bytes()
        pointer = StoredSnapshot(
            format_version=FORMAT_VERSION,
            run_id=snapshot.run_id,
            snapshot_id=snapshot.snapshot_id,
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            byte_length=len(payload),
        )
        payload_key = _payload_key(snapshot.run_id, snapshot.snapshot_id)
        pointer_key = _pointer_key(snapshot.run_id)
        for store in self._stores:
            store.replace(payload_key, payload)
            if store.read(payload_key) != payload:
                raise StateSnapshotWriteError(
                    f"snapshot payload at {payload_key!r} did not persist "
                    "verbatim; the previous snapshot remains the latest valid "
                    "snapshot"
                )
        pointer_bytes = canonical_configuration_bytes(pointer.to_json())
        for store in self._stores:
            store.replace(pointer_key, pointer_bytes)
            if store.read(pointer_key) != pointer_bytes:
                raise StateSnapshotWriteError(
                    f"snapshot pointer at {pointer_key!r} did not persist "
                    "verbatim; mirrors and the previous payload remain intact"
                )
        return pointer

    def load(self) -> RuntimeSnapshot:
        """Return the latest valid snapshot, falling back to the mirror."""
        failures: list[StateStoreError] = []
        for store in self._stores:
            try:
                return self._load_from(store)
            except StateStoreError as error:
                failures.append(error)
        if not failures:
            raise StateSnapshotMissingError(
                f"no snapshot is stored for run {self._run_id!r}"
            )
        for priority in (
            StateSnapshotIntegrityError,
            StateSnapshotVersionError,
            StateSnapshotMissingError,
        ):
            for failure in failures:
                if isinstance(failure, priority):
                    raise failure
        raise failures[0]

    def latest(self) -> StoredSnapshot:
        """Return the pointer record of the latest stored snapshot."""
        pointer_key = _pointer_key(self._run_id)
        errors: list[StateStoreError] = []
        for store in self._stores:
            try:
                raw = _read_object(store, pointer_key)
            except StateStoreError as error:
                errors.append(error)
                continue
            return StoredSnapshot.from_json(_load_json(raw, pointer_key))
        if not errors:
            raise StateSnapshotMissingError(
                f"no snapshot pointer is stored for run {self._run_id!r}"
            )
        raise errors[0]

    def _load_from(self, store: SnapshotObjectStore) -> RuntimeSnapshot:
        pointer_key = _pointer_key(self._run_id)
        pointer = StoredSnapshot.from_json(
            _load_json(_read_object(store, pointer_key), pointer_key)
        )
        if pointer.run_id != self._run_id:
            raise StateSnapshotIntegrityError(
                f"snapshot pointer at {pointer_key!r} references run {pointer.run_id!r}"
            )
        payload_key = _payload_key(self._run_id, pointer.snapshot_id)
        payload = _read_object(store, payload_key)
        if len(payload) != pointer.byte_length:
            raise StateSnapshotIntegrityError(
                f"snapshot payload at {payload_key!r} has an unexpected length"
            )
        if hashlib.sha256(payload).hexdigest() != pointer.payload_sha256:
            raise StateSnapshotIntegrityError(
                f"snapshot payload at {payload_key!r} fails its SHA-256 digest"
            )
        envelope = _mapping(_load_json(payload, payload_key), "snapshot envelope")
        snapshot_id = _require_digest(envelope.get("snapshot_id"), "snapshot_id")
        data = _mapping(envelope.get("snapshot"), "snapshot content")
        snapshot = RuntimeSnapshot.from_json(data)
        if snapshot.snapshot_id != snapshot_id:
            raise StateSnapshotIntegrityError(
                f"snapshot content hashes to {snapshot.snapshot_id!r} but the "
                f"envelope declares {snapshot_id!r}"
            )
        if snapshot.run_id != self._run_id:
            raise StateSnapshotIntegrityError(
                f"snapshot run {snapshot.run_id!r} does not match store run "
                f"{self._run_id!r}"
            )
        return snapshot

    # -- restore ---------------------------------------------------------------

    def restore_hub(self, snapshot: RuntimeSnapshot) -> PortfolioHub:
        """Rebuild the captured portfolio exactly into a fresh hub."""
        _validate_snapshot_run(self, snapshot)
        hub = PortfolioHub(snapshot.run_id)
        marks: dict[str, Decimal] = {}
        for book_state in snapshot.books:
            hub.create_book(book_state.book_id, book_state.initial_cash)
            for fill in book_state.fills:
                try:
                    hub.apply_fill(book_state.book_id, fill)
                except DuplicateFillError as error:
                    raise StateSnapshotRestoreError(
                        f"snapshot for {snapshot.run_id!r} contains duplicate "
                        f"fill {fill.fill_id!r}"
                    ) from error
            book = hub.book(book_state.book_id)
            if book.cash != book_state.cash:
                raise StateSnapshotRestoreError(
                    f"replayed book {book_state.book_id!r} cash {book.cash} does "
                    f"not match the captured cash {book_state.cash}"
                )
            for marker in book_state.markers:
                symbol = str(marker.symbol)
                previous = marks.get(symbol)
                if previous is not None and previous != marker.price:
                    raise StateSnapshotRestoreError(
                        f"captured books disagree on the mark for {symbol!r}"
                    )
                marks[symbol] = marker.price
        for symbol in sorted(marks):
            hub.mark(symbol, marks[symbol])
        return hub

    def restore_shell(
        self,
        snapshot: RuntimeSnapshot,
        configuration: TraderConfig,
        *,
        wall_clock: Clock | None = None,
    ) -> PaperShell:
        """Rebuild a paper shell whose supervision state matches the snapshot.

        The shell is constructed through its public API with the captured
        runtime options, then its public mutators replay the captured books,
        marks, order counts, and order attribution.  The shell's fresh kernel
        and venue continue from the captured event watermark; strategy
        bindings restart (see the module integration notes).
        """
        _validate_snapshot_run(self, snapshot)
        options = snapshot.paper_options()
        shell = (
            PaperShell(configuration, options=options)
            if wall_clock is None
            else PaperShell(configuration, options=options, wall_clock=wall_clock)
        )
        if tuple(shell.hub.book_ids) != snapshot.book_ids:
            raise StateSnapshotRestoreError(
                f"configuration books {tuple(shell.hub.book_ids)} do not match "
                f"snapshot books {snapshot.book_ids}"
            )
        self.restore_into_shell(snapshot, shell)
        return shell

    def restore_into_shell(self, snapshot: RuntimeSnapshot, shell: PaperShell) -> None:
        """Replay captured state into one existing shell's public surface."""
        _validate_snapshot_run(self, snapshot)
        hub = shell.hub
        marks: dict[str, Decimal] = {}
        for book_state in snapshot.books:
            book = hub.book(book_state.book_id)
            if book.initial_cash != book_state.initial_cash:
                raise StateSnapshotRestoreError(
                    f"book {book_state.book_id!r} initial cash {book.initial_cash} "
                    f"does not match the captured {book_state.initial_cash}"
                )
            for fill in book_state.fills:
                try:
                    hub.apply_fill(book_state.book_id, fill)
                except DuplicateFillError as error:
                    raise StateSnapshotRestoreError(
                        f"shell book {book_state.book_id!r} already accounts fill "
                        f"{fill.fill_id!r}"
                    ) from error
            if book.cash != book_state.cash:
                raise StateSnapshotRestoreError(
                    f"replayed book {book_state.book_id!r} cash {book.cash} does "
                    f"not match the captured cash {book_state.cash}"
                )
            for marker in book_state.markers:
                symbol = str(marker.symbol)
                previous = marks.get(symbol)
                if previous is not None and previous != marker.price:
                    raise StateSnapshotRestoreError(
                        f"captured books disagree on the mark for {symbol!r}"
                    )
                marks[symbol] = marker.price
        for symbol in sorted(marks):
            hub.mark(symbol, marks[symbol])
        for instance_id, count in snapshot.order_counts.items():
            for _ in range(count):
                shell.count_order(instance_id)
        for order_id, instance_id in snapshot.order_owners.items():
            shell.own_order(order_id, instance_id)

    def restore_randoms(self, snapshot: RuntimeSnapshot) -> dict[str, Random]:
        """Rebuild each captured random source with its exact state."""
        _validate_snapshot_run(self, snapshot)
        restored: dict[str, Random] = {}
        for record in snapshot.random_states:
            source = Random(0)
            record.apply(source)
            restored[record.instance_id] = source
        return restored

    def restore_strategy_state(
        self, snapshot: RuntimeSnapshot
    ) -> dict[str, Mapping[str, object]]:
        """Rebuild each strategy's frozen ``StateStore`` values."""
        _validate_snapshot_run(self, snapshot)
        return {
            record.instance_id: MappingProxyType(dict(record.values))
            for record in snapshot.strategy_state
        }


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------


def _validate_snapshot_run(store: RuntimeStateStore, snapshot: RuntimeSnapshot) -> None:
    if snapshot.run_id != store.run_id:
        raise StateSnapshotRestoreError(
            f"snapshot run {snapshot.run_id!r} does not match store run "
            f"{store.run_id!r}"
        )


def _options_json(options: PaperOptions) -> dict[str, object]:
    values: dict[str, object] = {}
    for field in fields(PaperOptions):
        value = getattr(options, field.name)
        if callable(value):
            continue
        values[field.name] = _freeze(value, f"options.{field.name}")
    return values


def _fill_json(fill: Fill) -> dict[str, object]:
    return {
        "event_id": fill.event_id,
        "timestamp": _normalize(fill.timestamp, "fill.timestamp"),
        "sequence": fill.sequence,
        "fill_id": fill.fill_id,
        "order_id": fill.order_id,
        "symbol": str(fill.symbol),
        "side": fill.side.value,
        "quantity": _normalize(fill.quantity, "fill.quantity"),
        "price": _normalize(fill.price, "fill.price"),
        "fee": _normalize(fill.fee, "fill.fee"),
        "liquidity": fill.liquidity,
    }


def _fill_from_json(data: object) -> Fill:
    values = _mapping(data, "fill record")
    return Fill(
        event_id=text(_string(values.get("event_id"), "event_id"), "event_id"),
        timestamp=_as_datetime(values.get("timestamp"), "fill timestamp"),
        sequence=_nonnegative_integer(values.get("sequence"), "fill sequence"),
        fill_id=text(_string(values.get("fill_id"), "fill_id"), "fill_id"),
        order_id=text(_string(values.get("order_id"), "order_id"), "order_id"),
        symbol=Symbol(_string(values.get("symbol"), "fill symbol")),
        side=Side(_string(values.get("side"), "fill side")),
        quantity=_decimal(values.get("quantity"), "fill quantity"),
        price=_decimal(values.get("price"), "fill price"),
        fee=_decimal(values.get("fee"), "fill fee"),
        liquidity=(
            None
            if values.get("liquidity") is None
            else _string(values.get("liquidity"), "fill liquidity")
        ),
    )


def _marker_from_json(data: object) -> PositionMarker:
    values = _mapping(data, "marker record")
    return PositionMarker(
        Symbol(_string(values.get("symbol"), "marker symbol")),
        _decimal(values.get("price"), "marker price"),
    )


def _rule_id(rule: object) -> str:
    value = getattr(rule, "rule_id", type(rule).__name__)
    return text(str(value), "rule_id")


def _bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean")
    return value


__all__ = [
    "BookState",
    "FORMAT_VERSION",
    "RandomState",
    "RiskState",
    "RuntimeSnapshot",
    "RuntimeStateStore",
    "SnapshotObjectStore",
    "SnapshotSchedule",
    "SnapshotTrigger",
    "StateSnapshotIntegrityError",
    "StateSnapshotMissingError",
    "StateSnapshotRestoreError",
    "StateSnapshotVersionError",
    "StateSnapshotWriteError",
    "StateStoreError",
    "StoredSnapshot",
    "StrategyState",
]
