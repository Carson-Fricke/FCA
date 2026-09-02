"""Dependency-injected option symbol-day ingestion and queue handling.

Option work is deliberately kept at the OSI symbol-day boundary.  The option
client can batch provider requests internally, while each selected contract
gets deterministic trade and quote objects plus matching coverage rows.  Queue
delivery remains owned by :mod:`lake.queue_worker`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol, cast, runtime_checkable

from lake.alpaca import NormalizedOptionQuote, NormalizedOptionTrade
from lake.catalog import ContractRecord, CoverageRecord, CoverageStatus, OptionType
from lake.contracts import DailyFrontSetRepository
from lake.coverage import CoverageSession
from lake.equity_ingest import (
    AtomicIngestCommitter,
    StagedParquetObject,
    WriterFactory,
)
from lake.parquet import ParquetMetadata, ParquetWriter
from lake.partition_paths import AssetClass, DataType
from lake.queue_worker import (
    DeadLetterSink,
    QueueDelivery,
    QueueHandler,
    QueueTransport,
    QueueWorker,
    QueueWorkerPolicy,
    WorkerClock,
)
from lake.settings import QueueSettings


@runtime_checkable
class OptionMarketDataClient(Protocol):
    """Provider boundary for selected OPRA contracts."""

    async def get_option_trades(
        self,
        contracts: Sequence[ContractRecord],
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> Sequence[NormalizedOptionTrade]:
        """Fetch normalized trades for selected contracts and a session."""

    async def get_option_quotes(
        self,
        contracts: Sequence[ContractRecord],
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> Sequence[NormalizedOptionQuote]:
        """Fetch normalized quotes for selected contracts and a session."""


@dataclass(frozen=True, slots=True)
class OptionIngestResult:
    """Published metadata for one OSI contract and session transaction."""

    contract: ContractRecord
    session_date: date
    coverage: tuple[CoverageRecord, ...]
    objects: tuple[ParquetMetadata, ...]

    @property
    def symbol(self) -> str:
        """Return the canonical OSI symbol."""
        return self.contract.symbol


class OptionIngestTransaction:
    """Fetch and atomically publish one selected OSI symbol-day."""

    def __init__(
        self,
        *,
        client: OptionMarketDataClient,
        committer: AtomicIngestCommitter,
        writer_factory: WriterFactory = ParquetWriter,
    ) -> None:
        self._client = client
        self._committer = committer
        self._writer_factory = writer_factory

    async def run(
        self,
        contract: ContractRecord,
        session: CoverageSession,
        *,
        data_types: Sequence[DataType | str] | DataType | str = (
            DataType.TRADE,
            DataType.QUOTE,
        ),
    ) -> OptionIngestResult:
        """Fetch and publish requested option datasets for one session.

        Empty responses from a closed session are written as valid empty
        Parquet objects and cataloged as terminal ``empty`` coverage.  A later
        delivery replaces the same paths and catalog keys instead of appending.
        """
        normalized_contract = _contract(contract)
        requested = _normalize_data_types(data_types)
        results = await self._fetch(normalized_contract, session, requested)
        fetch_error = _fetch_error(results)
        if fetch_error is not None:
            return self._publish_error(
                normalized_contract, session, requested, fetch_error
            )

        records_by_type: dict[DataType, Sequence[object]] = {}
        for data_type, result in results:
            records_by_type[data_type] = cast(Sequence[object], result)
        try:
            staged, metadata = self._stage(
                normalized_contract,
                session,
                requested,
                records_by_type,
            )
        except Exception as error:
            return self._publish_error(
                normalized_contract,
                session,
                requested,
                _error_message(error),
            )

        coverage = tuple(
            _coverage_record(
                normalized_contract,
                session,
                data_type,
                metadata[data_type],
                len(records_by_type[data_type]),
            )
            for data_type in requested
        )
        self._committer.commit(objects=staged, coverage=coverage)
        return OptionIngestResult(
            contract=normalized_contract,
            session_date=session.session_date,
            coverage=coverage,
            objects=tuple(metadata[data_type] for data_type in requested),
        )

    async def _fetch(
        self,
        contract: ContractRecord,
        session: CoverageSession,
        data_types: Sequence[DataType],
    ) -> list[tuple[DataType, object]]:
        requests: list[Awaitable[object]] = []
        labels: list[DataType] = []
        for data_type in data_types:
            labels.append(data_type)
            if data_type is DataType.TRADE:
                request = self._client.get_option_trades(
                    (contract,), session.open_time, session.close_time
                )
            else:
                request = self._client.get_option_quotes(
                    (contract,), session.open_time, session.close_time
                )
            requests.append(cast(Awaitable[object], request))

        values = list(await asyncio.gather(*requests, return_exceptions=True))
        return list(zip(labels, values, strict=True))

    def _stage(
        self,
        contract: ContractRecord,
        session: CoverageSession,
        data_types: Sequence[DataType],
        records_by_type: Mapping[DataType, Sequence[object]],
    ) -> tuple[tuple[StagedParquetObject, ...], dict[DataType, ParquetMetadata]]:
        store = _StagingObjectStore()
        writer = self._writer_factory(store)
        metadata: dict[DataType, ParquetMetadata] = {}
        for data_type in data_types:
            records = records_by_type[data_type]
            if data_type is DataType.TRADE:
                mappings = _trade_mappings(records, contract, session)
            else:
                mappings = _quote_mappings(records, contract, session)
            metadata[data_type] = writer.write(
                mappings,
                session_date=session.session_date,
                asset_class=AssetClass.OPTION,
                data_type=data_type,
                symbol=contract.symbol,
            )
        staged = tuple(
            StagedParquetObject(item, store.read(item.path))
            for item in (metadata[data_type] for data_type in data_types)
        )
        return staged, metadata

    def _publish_error(
        self,
        contract: ContractRecord,
        session: CoverageSession,
        data_types: Sequence[DataType],
        message: str,
    ) -> OptionIngestResult:
        coverage = tuple(
            CoverageRecord(
                asset_class=AssetClass.OPTION,
                data_type=data_type,
                symbol=contract.symbol,
                session_date=session.session_date,
                status=CoverageStatus.ERROR,
                error_message=message,
            )
            for data_type in data_types
        )
        self._committer.commit(objects=(), coverage=coverage)
        return OptionIngestResult(
            contract=contract,
            session_date=session.session_date,
            coverage=coverage,
            objects=(),
        )


class OptionQueueHandler:
    """Decode one option queue payload and delegate to the transaction."""

    def __init__(
        self,
        transaction: OptionIngestTransaction,
        front_sets: DailyFrontSetRepository | None = None,
    ) -> None:
        self._transaction = transaction
        self._front_sets = front_sets

    @classmethod
    def from_transaction(
        cls,
        transaction: OptionIngestTransaction,
        front_sets: DailyFrontSetRepository | None = None,
    ) -> OptionQueueHandler:
        """Create a queue boundary around an injected transaction."""
        return cls(transaction, front_sets)

    async def __call__(self, delivery: QueueDelivery) -> None:
        payload = delivery.payload
        if payload.get("asset_class") != AssetClass.OPTION.value:
            raise ValueError("option queue message must use asset_class 'option'")
        contract = _contract_from_payload(payload)
        session_date = _required_date(payload, "session_date")
        if self._front_sets is not None:
            front_set = self._front_sets.get(contract.underlying, session_date)
            if front_set is None:
                raise ValueError("option front set is not cataloged")
            selected = next(
                (
                    item
                    for item in front_set.contracts
                    if item.symbol == contract.symbol
                ),
                None,
            )
            if selected is None:
                raise ValueError("option symbol is not in the cataloged front set")
            contract = selected

        session = CoverageSession(
            session_date=session_date,
            open_time=_required_datetime(payload, "start_time"),
            close_time=_required_datetime(payload, "end_time"),
        )
        raw_data_types = payload.get("data_types")
        if not isinstance(raw_data_types, (list, tuple)) or not raw_data_types:
            raise ValueError(
                "option queue message data_types must be a non-empty array"
            )
        if any(not isinstance(value, (DataType, str)) for value in raw_data_types):
            raise ValueError("option queue message data_types must contain strings")
        await self._transaction.run(
            contract,
            session,
            data_types=cast(
                Sequence[DataType | str],
                raw_data_types,
            ),
        )


def create_option_worker(
    *,
    queue: QueueTransport,
    client: OptionMarketDataClient,
    committer: AtomicIngestCommitter,
    policy: QueueWorkerPolicy | None = None,
    settings: QueueSettings | None = None,
    front_sets: DailyFrontSetRepository | None = None,
    writer_factory: WriterFactory = ParquetWriter,
    dead_letter: DeadLetterSink | None = None,
    clock: WorkerClock | None = None,
    idle_sleep_seconds: float = 1.0,
) -> QueueWorker:
    """Build the shared queue worker around option-specific ingestion only."""
    if policy is None:
        if settings is None:
            raise ValueError("policy or settings must be supplied")
        policy = QueueWorkerPolicy.from_settings(settings)
    handler: QueueHandler = OptionQueueHandler(
        OptionIngestTransaction(
            client=client,
            committer=committer,
            writer_factory=writer_factory,
        ),
        front_sets,
    )
    return QueueWorker(
        queue=queue,
        handler=handler,
        policy=policy,
        dead_letter=dead_letter,
        clock=clock,
        idle_sleep_seconds=idle_sleep_seconds,
    )


build_option_worker = create_option_worker
OptionWorker = OptionQueueHandler


class _StagingObjectStore:
    """Private store that keeps objects invisible until the atomic commit."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def replace(self, relative_path: str, payload: bytes) -> None:
        self._objects[relative_path] = payload

    def read(self, relative_path: str) -> bytes:
        try:
            return self._objects[relative_path]
        except KeyError as error:
            raise RuntimeError(
                f"writer did not stage expected object {relative_path!r}"
            ) from error


def _contract(contract: ContractRecord) -> ContractRecord:
    if not isinstance(contract, ContractRecord):
        raise TypeError("option work requires a ContractRecord")
    return contract


def _normalize_data_types(
    values: Sequence[DataType | str] | DataType | str,
) -> tuple[DataType, ...]:
    if isinstance(values, (DataType, str)):
        values = (values,)
    normalized = {DataType(value) for value in values}
    if not normalized:
        raise ValueError("data_types must not be empty")
    unsupported = normalized - {DataType.TRADE, DataType.QUOTE}
    if unsupported:
        raise ValueError("option data_types must be trades or quotes")
    return tuple(
        data_type
        for data_type in (DataType.TRADE, DataType.QUOTE)
        if data_type in normalized
    )


def _fetch_error(results: Sequence[tuple[DataType, object]]) -> str | None:
    errors: list[str] = []
    for data_type, result in results:
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException):
            errors.append(f"{data_type.value}: {_error_message(result)}")
    return "; ".join(errors) if errors else None


def _trade_mappings(
    records: Sequence[object],
    contract: ContractRecord,
    session: CoverageSession,
) -> list[Mapping[str, object]]:
    normalized = sorted(
        (
            _validated_trade(cast(NormalizedOptionTrade, record), contract, session)
            for record in records
        ),
        key=lambda record: (
            record.timestamp,
            record.exchange,
            record.price,
            record.size,
            record.condition,
        ),
    )
    return [
        {
            "timestamp": record.timestamp,
            "symbol": record.symbol,
            "exchange": record.exchange,
            "price": record.price,
            "size": record.size,
            "condition": record.condition,
        }
        for record in normalized
    ]


def _quote_mappings(
    records: Sequence[object],
    contract: ContractRecord,
    session: CoverageSession,
) -> list[Mapping[str, object]]:
    normalized = sorted(
        (
            _validated_quote(cast(NormalizedOptionQuote, record), contract, session)
            for record in records
        ),
        key=lambda record: (
            record.timestamp,
            record.bid_exchange,
            record.bid_price,
            record.bid_size,
            record.ask_exchange,
            record.ask_price,
            record.ask_size,
            record.condition,
        ),
    )
    return [
        {
            "timestamp": record.timestamp,
            "symbol": record.symbol,
            "bid_exchange": record.bid_exchange,
            "bid_price": record.bid_price,
            "bid_size": record.bid_size,
            "ask_exchange": record.ask_exchange,
            "ask_price": record.ask_price,
            "ask_size": record.ask_size,
            "condition": record.condition,
        }
        for record in normalized
    ]


def _validated_trade(
    record: NormalizedOptionTrade,
    contract: ContractRecord,
    session: CoverageSession,
) -> NormalizedOptionTrade:
    _validate_record(record, contract, session)
    return record


def _validated_quote(
    record: NormalizedOptionQuote,
    contract: ContractRecord,
    session: CoverageSession,
) -> NormalizedOptionQuote:
    _validate_record(record, contract, session)
    return record


def _validate_record(
    record: NormalizedOptionTrade | NormalizedOptionQuote,
    contract: ContractRecord,
    session: CoverageSession,
) -> None:
    if record.timestamp.tzinfo is None or record.timestamp.utcoffset() is None:
        raise ValueError(f"{contract.symbol} record timestamp must be timezone-aware")
    timestamp = record.timestamp.astimezone(UTC)
    if not session.open_time <= timestamp < session.close_time:
        raise ValueError(
            f"{contract.symbol} record timestamp is outside session {session.session_date}"
        )
    if record.symbol.strip().upper() != contract.symbol:
        raise ValueError("option record symbol does not match the requested symbol")
    if record.underlying != contract.underlying:
        raise ValueError("option record underlying does not match the contract")
    if record.expiration_date != contract.expiration_date:
        raise ValueError("option record expiration does not match the contract")
    if record.strike_price != contract.strike_price:
        raise ValueError("option record strike does not match the contract")
    if record.option_type != contract.option_type:
        raise ValueError("option record type does not match the contract")


def _coverage_record(
    contract: ContractRecord,
    session: CoverageSession,
    data_type: DataType,
    metadata: ParquetMetadata,
    row_count: int,
) -> CoverageRecord:
    if session.is_partial:
        status = CoverageStatus.PARTIAL
    elif row_count == 0:
        status = CoverageStatus.EMPTY
    else:
        status = CoverageStatus.COMPLETE
    return CoverageRecord(
        asset_class=AssetClass.OPTION,
        data_type=data_type,
        symbol=contract.symbol,
        session_date=session.session_date,
        status=status,
        object_path=metadata.path,
        row_count=metadata.rows,
        byte_size=metadata.bytes,
        checksum=metadata.checksum,
    )


def _contract_from_payload(payload: Mapping[str, object]) -> ContractRecord:
    symbol = _required_text(payload, "symbol")
    underlying = _required_text(payload, "underlying")
    expiration = _required_date(payload, "expiration_date")
    strike_text = _required_text(payload, "strike_price")
    try:
        strike = Decimal(strike_text)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("strike_price must be a decimal number") from error
    try:
        option_type = OptionType(_required_text(payload, "option_type"))
    except ValueError as error:
        raise ValueError("option_type must be 'call' or 'put'") from error
    return ContractRecord(
        symbol=symbol,
        underlying=underlying,
        expiration_date=expiration,
        strike_price=strike,
        option_type=option_type,
    )


def _required_text(payload: Mapping[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"option queue field {name!r} must be a non-empty string")
    return value


def _required_date(payload: Mapping[str, object], name: str) -> date:
    value = _required_text(payload, name)
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"option queue field {name!r} must be an ISO date") from error


def _required_datetime(payload: Mapping[str, object], name: str) -> datetime:
    value = _required_text(payload, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            f"option queue field {name!r} must be an ISO datetime"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"option queue field {name!r} must include a timezone")
    return parsed.astimezone(UTC)


def _error_message(error: BaseException) -> str:
    detail = str(error).strip()
    return type(error).__name__ if not detail else f"{type(error).__name__}: {detail}"


__all__ = [
    "OptionIngestResult",
    "OptionIngestTransaction",
    "OptionMarketDataClient",
    "OptionQueueHandler",
    "OptionWorker",
    "build_option_worker",
    "create_option_worker",
]
