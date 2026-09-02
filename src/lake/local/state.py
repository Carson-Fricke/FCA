"""Deterministic filesystem layout and catalog state for local mode."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from threading import RLock

from lake.catalog.models import CoverageRecord, CoverageStatus, canonical_symbol
from lake.contracts import AssetClass, DataType

_MARKER_NAME = ".market-lake-local"
_MARKER_CONTENT = "market-lake-local-v1\n"
_STATE_SCHEMA_VERSION = 1


class LocalStateError(RuntimeError):
    """Local state is unsafe, unavailable, or malformed."""


@dataclass(frozen=True, slots=True)
class LocalLayout:
    """Repository-ignored directories owned by one local market-lake instance."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.expanduser().resolve())

    @classmethod
    def from_environment(
        cls,
        data_dir: str | os.PathLike[str] | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> LocalLayout:
        """Resolve an explicit path, environment override, or repository default."""
        environment = os.environ if environ is None else environ
        configured = data_dir
        if configured is None:
            configured = environment.get("LAKE_LOCAL_DATA_DIR")
        if configured is not None and not str(configured).strip():
            raise LocalStateError("LAKE_LOCAL_DATA_DIR must not be empty")
        root = (
            Path(configured)
            if configured is not None
            else Path(__file__).resolve().parents[3] / ".local" / "market-lake"
        )
        return cls(root)

    @property
    def objects(self) -> Path:
        return self.root / "objects"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def catalog(self) -> Path:
        return self.state / "catalog.json"

    @property
    def marker(self) -> Path:
        return self.root / _MARKER_NAME

    def initialize(self) -> None:
        """Create or validate a directory dedicated to local market-lake state."""
        if self.root.exists() and not self.root.is_dir():
            raise LocalStateError(f"local data path is not a directory: {self.root}")
        if self.root.is_dir() and not self.marker.exists():
            try:
                has_entries = next(self.root.iterdir(), None) is not None
            except OSError as error:
                raise LocalStateError(
                    f"cannot inspect local data directory: {self.root}"
                ) from error
            if has_entries:
                raise LocalStateError(
                    "refusing to use a non-empty directory not created by local mode: "
                    f"{self.root}"
                )

        self.root.mkdir(parents=True, exist_ok=True)
        if self.marker.exists():
            if not self.marker.is_file():
                raise LocalStateError(f"invalid local data marker: {self.marker}")
            if self.marker.read_text(encoding="ascii") != _MARKER_CONTENT:
                raise LocalStateError(f"invalid local data marker: {self.marker}")
        else:
            self.marker.write_text(_MARKER_CONTENT, encoding="ascii")
        self.objects.mkdir(exist_ok=True)
        self.state.mkdir(exist_ok=True)

    def reset(self) -> bool:
        """Remove a previously initialized local tree and nothing else."""
        if not self.root.exists():
            return False
        if not self.root.is_dir() or not self.marker.is_file():
            raise LocalStateError(
                "refusing to reset a directory without the local-mode marker: "
                f"{self.root}"
            )
        if self.marker.read_text(encoding="ascii") != _MARKER_CONTENT:
            raise LocalStateError(f"invalid local data marker: {self.marker}")
        shutil.rmtree(self.root)
        return True


class LocalCatalogRepository:
    """Coverage catalog persisted as stable, atomically replaced JSON."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path).resolve()
        self._lock = RLock()

    def get(
        self,
        asset_class: AssetClass,
        data_type: DataType,
        symbol: str,
        session_date: date,
    ) -> CoverageRecord | None:
        key = (
            AssetClass(asset_class),
            DataType(data_type),
            canonical_symbol(symbol),
            session_date,
        )
        with self._lock:
            return self._read().get(key)

    def upsert(self, record: CoverageRecord) -> CoverageRecord:
        self.upsert_many((record,))
        return record

    def upsert_many(self, records: Iterable[CoverageRecord]) -> None:
        """Replace all supplied logical keys in one catalog-file update."""
        updates = tuple(records)
        if len({record.key for record in updates}) != len(updates):
            raise ValueError("coverage update contains duplicate keys")
        with self._lock:
            current = self._read()
            current.update((record.key, record) for record in updates)
            self._write(current.values())

    def list(
        self,
        *,
        asset_class: AssetClass | None = None,
        data_type: DataType | None = None,
        symbol: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[CoverageRecord, ...]:
        normalized_asset = AssetClass(asset_class) if asset_class is not None else None
        normalized_type = DataType(data_type) if data_type is not None else None
        normalized_symbol = canonical_symbol(symbol) if symbol is not None else None
        if start is not None and end is not None and start > end:
            raise ValueError("start must be on or before end")
        with self._lock:
            records = sorted(self._read().values(), key=_record_sort_key)
        return tuple(
            record
            for record in records
            if (normalized_asset is None or record.asset_class is normalized_asset)
            and (normalized_type is None or record.data_type is normalized_type)
            and (normalized_symbol is None or record.symbol == normalized_symbol)
            and (start is None or record.session_date >= start)
            and (end is None or record.session_date <= end)
        )

    def _read(
        self,
    ) -> dict[tuple[AssetClass, DataType, str, date], CoverageRecord]:
        if not self._path.exists():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or set(payload) != {
                "schema_version",
                "coverage",
            }:
                raise ValueError("catalog root has unexpected fields")
            if payload["schema_version"] != _STATE_SCHEMA_VERSION:
                raise ValueError("catalog schema version is unsupported")
            rows = payload["coverage"]
            if not isinstance(rows, list):
                raise ValueError("catalog coverage must be a list")
            records = tuple(_record_from_json(row) for row in rows)
            result = {record.key: record for record in records}
            if len(result) != len(records):
                raise ValueError("catalog contains duplicate coverage keys")
            return result
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as error:
            raise LocalStateError(f"local catalog is invalid: {self._path}") from error

    def _write(self, records: Iterable[CoverageRecord]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": _STATE_SCHEMA_VERSION,
            "coverage": [
                _record_to_json(record)
                for record in sorted(records, key=_record_sort_key)
            ],
        }
        serialized = (
            json.dumps(
                payload,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
            + "\n"
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


class LocalFileIssuer:
    """Issue local file URLs confined to the configured object root."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root).resolve()

    def issue(self, object_path: str) -> str:
        if (
            not object_path
            or "\\" in object_path
            or object_path.startswith("/")
            or any(part in {"", ".", ".."} for part in object_path.split("/"))
        ):
            raise ValueError("object path must be a canonical relative POSIX path")
        relative = PurePosixPath(object_path)
        target = (self._root / Path(*relative.parts)).resolve()
        if target != self._root and self._root not in target.parents:
            raise ValueError("object path escapes the local object root")
        if not target.is_file():
            raise LocalStateError(f"local object is unavailable: {object_path}")
        return target.as_uri()


def _record_sort_key(record: CoverageRecord) -> tuple[str, str, str, date]:
    return (
        record.asset_class.value,
        record.data_type.value,
        record.symbol,
        record.session_date,
    )


def _record_to_json(record: CoverageRecord) -> dict[str, object]:
    return {
        "asset_class": record.asset_class.value,
        "byte_size": record.byte_size,
        "checksum": record.checksum,
        "data_type": record.data_type.value,
        "error_message": record.error_message,
        "object_path": record.object_path,
        "row_count": record.row_count,
        "session_date": record.session_date.isoformat(),
        "status": record.status.value,
        "symbol": record.symbol,
    }


def _record_from_json(value: object) -> CoverageRecord:
    if not isinstance(value, dict) or set(value) != {
        "asset_class",
        "byte_size",
        "checksum",
        "data_type",
        "error_message",
        "object_path",
        "row_count",
        "session_date",
        "status",
        "symbol",
    }:
        raise ValueError("coverage row has unexpected fields")
    if not isinstance(value["session_date"], str):
        raise TypeError("coverage session_date must be a string")
    if not isinstance(value["symbol"], str):
        raise TypeError("coverage symbol must be a string")
    if not isinstance(value["row_count"], int) or isinstance(value["row_count"], bool):
        raise TypeError("coverage row_count must be an integer")
    if not isinstance(value["byte_size"], int) or isinstance(value["byte_size"], bool):
        raise TypeError("coverage byte_size must be an integer")
    for field in ("object_path", "checksum", "error_message"):
        if value[field] is not None and not isinstance(value[field], str):
            raise TypeError(f"coverage {field} must be a string or null")
    return CoverageRecord(
        asset_class=AssetClass(value["asset_class"]),
        data_type=DataType(value["data_type"]),
        symbol=value["symbol"],
        session_date=date.fromisoformat(value["session_date"]),
        status=CoverageStatus(value["status"]),
        object_path=value["object_path"],
        row_count=value["row_count"],
        byte_size=value["byte_size"],
        checksum=value["checksum"],
        error_message=value["error_message"],
    )


__all__ = [
    "LocalCatalogRepository",
    "LocalFileIssuer",
    "LocalLayout",
    "LocalStateError",
]
