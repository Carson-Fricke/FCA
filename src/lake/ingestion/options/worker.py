"""Queue controller and composition for option ingestion."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import cast

from lake.catalog.front_sets import DailyFrontSetRepository
from lake.catalog.models import ContractRecord, OptionType
from lake.configuration import QueueSettings
from lake.contracts import AssetClass, DataType
from lake.coverage import CoverageSession
from lake.ingestion.common import AtomicIngestCommitter, WriterFactory
from lake.ingestion.options.transaction import (
    OptionIngestTransaction,
    OptionMarketDataClient,
)
from lake.queue.worker import (
    DeadLetterSink,
    QueueDelivery,
    QueueHandler,
    QueueTransport,
    QueueWorker,
    QueueWorkerPolicy,
    WorkerClock,
)
from lake.storage.parquet import ParquetWriter


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
            data_types=cast(Sequence[DataType | str], raw_data_types),
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


build_option_worker = create_option_worker
OptionWorker = OptionQueueHandler

__all__ = [
    "OptionQueueHandler",
    "OptionWorker",
    "build_option_worker",
    "create_option_worker",
]
