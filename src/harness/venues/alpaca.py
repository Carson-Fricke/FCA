"""Live Alpaca equity execution venue for the fire-and-forget boundary.

This is the first live broker venue in the harness. It mirrors the
``harness.data.alpaca_ws`` split: a synchronous, kernel-facing
:class:`AlpacaExecutionInbox` normalizes broker trade-update frames into the
same :class:`~harness.core.types.accounting.ExecutionReport`,
:class:`~harness.core.types.accounting.Fill`, and
:class:`~harness.core.types.accounting.Position` records used by replay,
while :class:`AlpacaBroker` owns the async REST command transport and the
strategy-tagged UUID7 client-order-id policy.

Fire-and-forget boundary:

- Commands are one-way. ``submit``, ``cancel``, ``replace``, and
  ``request_position_snapshot`` return a :class:`AlpacaCommandReceipt` that
  carries only the dispatched command identity, never a venue outcome.
- The venue exposes no state introspection: there is no order book, working
  state, or venue-side status accessor on the venue object.
- Every execution outcome arrives as a kernel event through the inbox, stamped
  with an explicit timestamp and a monotonic sequence so the deterministic
  event bus can order reports (class priority 4) before fills (priority 5).
- Alpaca venue identities are translated back to kernel order identities with
  :class:`AlpacaOrderDirectory`, which records only what this adapter itself
  dispatched. Frames for orders this adapter never submitted surface under
  their wire ``client_order_id`` so late reports after a restart stay visible.

Capability matrix — equity-only in this adapter:

- Single-leg equity submit: market, limit, stop, and stop-limit orders with
  day, GTC, and IOC time-in-force.
- Single-leg equity cancel: resolved through the directory to the bound
  client order id.
- Single-leg equity replace: quantity, time-in-force, and prices. Symbol,
  side, and order type are fixed by Alpaca replace; type changes require
  cancel plus a fresh submit.
- Multi-leg and option orders, crypto pairs, extended-hours flags, and
  non-equity position snapshots are not supported and raise
  :class:`AlpacaVenueError` rather than degrading silently.
- UUID7 client order ids are minted only here, tagged with the strategy
  identity supplied at construction, per the live-adapter ID policy in
  ``harness.core.ids``.

The REST client follows the ``lake.providers.alpaca`` async transport
patterns: the ``httpx.AsyncClient`` is injectable and only owned when this
module creates it, retries cover transport errors and the standard retryable
status codes with ``Retry-After`` handling, and every retry re-sends the same
minted client order id so Alpaca's duplicate-id rejection bounds any
ambiguous first attempt.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import ClassVar, Protocol, cast
from urllib.parse import quote
from uuid import UUID

import httpx

from harness.core.symbols import Symbol, is_osi_symbol
from harness.core.types.accounting import ExecutionReport, Fill, Position
from harness.core.types.common import text, timestamp
from harness.core.types.orders import (
    ExecutionType,
    MultiLegOrder,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    SingleLegOrder,
)
from harness.venues.scripted import VenueOperation

type BrokerClock = Callable[[], datetime]
type BrokerSleep = Callable[[float], Awaitable[None]]
type QueryParams = Mapping[str, str]
type BrokerEvent = ExecutionReport | Fill | Position

_ZERO = Decimal("0")
_MAX_CLIENT_ORDER_ID_LENGTH = 48
_FILL_EXECUTION_TYPES = (ExecutionType.PARTIAL_FILL, ExecutionType.FILL)
_EVENT_OUTCOMES: Mapping[str, tuple[OrderStatus, ExecutionType]] = MappingProxyType(
    {
        "new": (OrderStatus.NEW, ExecutionType.ACKNOWLEDGED),
        "accept": (OrderStatus.ACCEPTED, ExecutionType.ACKNOWLEDGED),
        "partial_fill": (
            OrderStatus.PARTIALLY_FILLED,
            ExecutionType.PARTIAL_FILL,
        ),
        "fill": (OrderStatus.FILLED, ExecutionType.FILL),
        "canceled": (OrderStatus.CANCELED, ExecutionType.CANCELED),
        "expired": (OrderStatus.EXPIRED, ExecutionType.EXPIRED),
        "done_for_day": (OrderStatus.EXPIRED, ExecutionType.EXPIRED),
        "replaced": (OrderStatus.REPLACED, ExecutionType.REPLACED),
        "pending_replace": (OrderStatus.CANCEL_PENDING, ExecutionType.STATUS),
        "pending_cancel": (OrderStatus.CANCEL_PENDING, ExecutionType.STATUS),
        "rejected": (OrderStatus.REJECTED, ExecutionType.REJECTED),
        "calculated": (OrderStatus.FILLED, ExecutionType.STATUS),
    }
)


class AlpacaVenueError(RuntimeError):
    """Base class for live Alpaca venue command and protocol errors."""


class UnknownAlpacaOrderError(AlpacaVenueError):
    """Raised when a command refers to an order this adapter never bound."""

    def __init__(self, order_id: str) -> None:
        self.order_id = order_id
        super().__init__(f"no Alpaca identity is bound to order_id {order_id!r}")


class AlpacaVenueProtocolError(AlpacaVenueError):
    """Alpaca sent an invalid broker frame or rejected a stream operation."""


class AlpacaBrokerHTTPError(AlpacaVenueError):
    """A non-successful Alpaca broker response after retries are exhausted."""

    def __init__(self, status_code: int, url: str) -> None:
        self.status_code = status_code
        self.url = url
        super().__init__(f"Alpaca broker request failed with HTTP {status_code}: {url}")


class AsyncBrokerResponse(Protocol):
    """Small response surface required by the venue and test fakes."""

    status_code: int
    headers: Mapping[str, str]

    def json(self) -> object:
        """Decode the response body."""


class AsyncBrokerTransport(Protocol):
    """Request surface implemented by ``httpx.AsyncClient`` and test fakes."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: QueryParams,
        json: object | None,
        headers: Mapping[str, str],
        timeout: float,
    ) -> AsyncBrokerResponse:
        """Send one asynchronous HTTP request."""


def _uuid7(time_ms: int, *, rand_a: int, rand_b: int) -> UUID:
    if time_ms < 0:
        raise ValueError("uuid7 timestamp must not be negative")
    value = (time_ms & 0xFFFFFFFFFFFF) << 80
    value |= 0x7 << 76
    value |= (rand_a & 0xFFF) << 64
    value |= 0b10 << 62
    value |= rand_b & 0x3FFFFFFFFFFFFFFF
    return UUID(int=value)


def _default_time_ms() -> int:
    return time.time_ns() // 1_000_000


def _strategy_tag(strategy_id: str) -> str:
    value = text(strategy_id, "strategy_id")
    sanitized = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")
    tag = (sanitized or "unattributed")[:11]
    return tag


class AlpacaClientOrderIdSource:
    """Strategy-tagged UUID7 client order ids minted only at this boundary.

    Live broker client ids are the one identity in the platform that is
    deliberately not deterministic: they must be globally unique across
    restarts and deployments, so they derive from the wall-clock timestamp
    and cryptographically secure entropy of RFC 9562 UUID7. Both inputs are
    injectable so tests stay deterministic. Alpaca caps client order ids at
    48 characters, so the strategy tag is sanitized and bounded.
    """

    max_tag_length: ClassVar[int] = 11
    max_client_order_id_length: ClassVar[int] = _MAX_CLIENT_ORDER_ID_LENGTH

    __slots__ = ("_entropy", "_strategy_tag", "_time_ms")

    def __init__(
        self,
        *,
        strategy_id: str = "unattributed",
        time_ms: Callable[[], int] | None = None,
        entropy: Callable[[int], int] | None = None,
    ) -> None:
        self._strategy_tag = _strategy_tag(strategy_id)
        self._time_ms = _default_time_ms if time_ms is None else time_ms
        self._entropy = secrets.randbits if entropy is None else entropy

    @property
    def strategy_tag(self) -> str:
        """Return the sanitized strategy tag embedded in minted ids."""
        return self._strategy_tag

    def next_client_order_id(self) -> str:
        """Mint the next strategy-tagged UUID7 client order id."""
        identifier = _uuid7(
            int(self._time_ms()),
            rand_a=self._entropy(12),
            rand_b=self._entropy(62),
        )
        client_order_id = f"{self._strategy_tag}-{identifier}"
        if len(client_order_id) > self.max_client_order_id_length:
            raise AlpacaVenueError(
                "minted client order id exceeds the Alpaca 48-character limit"
            )
        return client_order_id


@dataclass(frozen=True, slots=True)
class AlpacaCommandReceipt:
    """Identity of one dispatched venue command; never a venue outcome."""

    operation: VenueOperation
    order_id: str | None = None
    client_order_id: str | None = None
    account_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation", VenueOperation(self.operation))
        for field_name in ("order_id", "client_order_id", "account_id"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, text(value, field_name))
        expected_fields = {
            VenueOperation.SUBMIT: (True, True, False),
            VenueOperation.CANCEL: (True, True, False),
            VenueOperation.REPLACE: (True, True, False),
            VenueOperation.POSITION_SNAPSHOT: (False, False, True),
        }
        populated_fields = (
            self.order_id is not None,
            self.client_order_id is not None,
            self.account_id is not None,
        )
        if populated_fields != expected_fields[self.operation]:
            raise ValueError(
                f"{self.operation.value} receipt has invalid order/account identifiers"
            )


@dataclass(frozen=True, slots=True)
class AlpacaOrderBinding:
    """Command-side identity of one order this adapter dispatched."""

    order_id: str
    client_order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    order_type: OrderType


class AlpacaOrderDirectory:
    """Maps kernel order ids to the Alpaca identities this adapter minted.

    The directory records only command bookkeeping created by this adapter;
    it is plumbing for identity translation, not venue-state introspection,
    and nothing here is exposed through venue commands.
    """

    __slots__ = ("_by_client", "_chains", "_client_for_order")

    def __init__(self) -> None:
        self._by_client: dict[str, AlpacaOrderBinding] = {}
        self._client_for_order: dict[str, str] = {}
        self._chains: dict[str, tuple[AlpacaOrderBinding, ...]] = {}

    def bind(self, binding: AlpacaOrderBinding) -> None:
        """Record an initial submit binding; identical rebinds are idempotent."""
        if not isinstance(binding, AlpacaOrderBinding):
            raise TypeError("binding must be an AlpacaOrderBinding")
        existing = self._by_client.get(binding.client_order_id)
        if existing is not None:
            if existing != binding:
                raise AlpacaVenueError(
                    f"client order id {binding.client_order_id!r} is already "
                    "bound to a different order"
                )
            return
        self._by_client[binding.client_order_id] = binding
        self._client_for_order[binding.order_id] = binding.client_order_id
        self._chains[binding.client_order_id] = (binding,)

    def link_replacement(
        self, previous_client_order_id: str, binding: AlpacaOrderBinding
    ) -> None:
        """Chain a replacement binding onto its previous client order id."""
        previous_client = text(previous_client_order_id, "previous_client_order_id")
        if not isinstance(binding, AlpacaOrderBinding):
            raise TypeError("binding must be an AlpacaOrderBinding")
        chain = self._chains.get(previous_client)
        if chain is None:
            raise UnknownAlpacaOrderError(previous_client)
        existing = self._by_client.get(binding.client_order_id)
        if existing is not None and existing != binding:
            raise AlpacaVenueError(
                f"client order id {binding.client_order_id!r} is already "
                "bound to a different order"
            )
        if existing is None:
            self._by_client[binding.client_order_id] = binding
        self._client_for_order[binding.order_id] = binding.client_order_id
        self._chains[binding.client_order_id] = chain + (binding,)

    def binding_for_client(self, client_order_id: str) -> AlpacaOrderBinding | None:
        """Return the binding for a client order id, if this adapter minted it."""
        return self._by_client.get(client_order_id)

    def chain_for_client(self, client_order_id: str) -> tuple[AlpacaOrderBinding, ...]:
        """Return the replacement chain ending at this client order id."""
        return self._chains.get(client_order_id, ())

    def client_order_id_for(self, order_id: str) -> str | None:
        """Return the most recent client order id bound to a kernel order."""
        return self._client_for_order.get(order_id)


class AlpacaExecutionInbox:
    """Synchronous, kernel-facing normalizer for Alpaca trade updates.

    Like :class:`~harness.data.alpaca_ws.AlpacaSipInbox`, the inbox performs
    no network, clock, sleep, or random access: the async shell supplies the
    decoded frames and the observation timestamp, and the inbox atomically
    enqueues validated kernel events with monotonic sequences.
    """

    event_source: ClassVar[str] = "alpaca-broker"

    __slots__ = (
        "_directory",
        "_events",
        "_max_event_age",
        "_max_future_skew",
        "_next_sequence",
    )

    def __init__(
        self,
        *,
        initial_sequence: int = 0,
        max_future_skew: timedelta = timedelta(seconds=5),
        max_event_age: timedelta = timedelta(minutes=5),
        directory: AlpacaOrderDirectory | None = None,
    ) -> None:
        if (
            isinstance(initial_sequence, bool)
            or not isinstance(initial_sequence, int)
            or initial_sequence < 0
        ):
            raise ValueError("initial_sequence must be a non-negative integer")
        _nonnegative_duration(max_future_skew, "max_future_skew")
        _positive_duration(max_event_age, "max_event_age")
        self._next_sequence = initial_sequence
        self._max_future_skew = max_future_skew
        self._max_event_age = max_event_age
        self._directory = AlpacaOrderDirectory() if directory is None else directory
        self._events: deque[BrokerEvent] = deque()

    @property
    def directory(self) -> AlpacaOrderDirectory:
        """Return the shared command-side identity directory."""
        return self._directory

    def __len__(self) -> int:
        return len(self._events)

    @property
    def next_sequence(self) -> int:
        """Return the sequence that the next accepted event will receive."""
        return self._next_sequence

    def pop(self) -> BrokerEvent | None:
        """Remove the oldest normalized event, if one is available."""
        return None if not self._events else self._events.popleft()

    def drain(self, limit: int | None = None) -> tuple[BrokerEvent, ...]:
        """Remove events in provider order without consulting a clock."""
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
        ):
            raise ValueError("limit must be a non-negative integer or None")
        drained: list[BrokerEvent] = []
        while self._events and (limit is None or len(drained) < limit):
            drained.append(self._events.popleft())
        return tuple(drained)

    def push(
        self, frame: Mapping[str, object], *, observed_at: datetime
    ) -> tuple[BrokerEvent, ...]:
        """Validate and atomically enqueue one decoded trade-update frame."""
        observed = timestamp(observed_at, "observed_at")
        if not isinstance(frame, Mapping):
            raise AlpacaVenueProtocolError("Alpaca broker frame must be a JSON object")
        stream = frame.get("stream")
        if stream != "trade_updates":
            raise AlpacaVenueProtocolError(
                f"unsupported Alpaca broker stream: {stream!r}"
            )
        data = frame.get("data")
        if not isinstance(data, Mapping):
            raise AlpacaVenueProtocolError("Alpaca broker frame data must be an object")
        events = self._normalize(data, observed, self._next_sequence)
        for event in events:
            self._events.append(event)
        self._next_sequence += len(events)
        return events

    def push_positions(
        self,
        rows: object,
        *,
        observed_at: datetime,
        account_id: str,
    ) -> tuple[Position, ...]:
        """Validate and atomically enqueue one decoded positions snapshot."""
        observed = timestamp(observed_at, "observed_at")
        account = text(account_id, "account_id")
        if isinstance(rows, Mapping):
            raise AlpacaVenueProtocolError("Alpaca positions response must be an array")
        if isinstance(rows, str | bytes) or not isinstance(rows, Sequence):
            raise AlpacaVenueProtocolError("Alpaca positions response must be an array")
        events: list[Position] = []
        for offset, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise AlpacaVenueProtocolError("Alpaca positions rows must be objects")
            sequence = self._next_sequence + offset
            symbol = str(Symbol(_required_text(row, "symbol")))
            events.append(
                Position(
                    event_id=self._event_id("position", symbol, observed, sequence),
                    timestamp=observed,
                    sequence=sequence,
                    account_id=account,
                    symbol=Symbol(symbol),
                    quantity=_required_decimal(row, "qty"),
                    average_price=_required_positive_decimal(row, "avg_entry_price"),
                    market_price=_optional_positive_decimal(row, "current_price"),
                    unrealized_pnl=_optional_decimal(row, "unrealized_pl"),
                )
            )
        for event in events:
            self._events.append(event)
        self._next_sequence += len(events)
        return tuple(events)

    def _normalize(
        self,
        data: Mapping[str, object],
        observed_at: datetime,
        first_sequence: int,
    ) -> tuple[BrokerEvent, ...]:
        event_name = _required_text(data, "event")
        outcome = _EVENT_OUTCOMES.get(event_name)
        if outcome is None:
            raise AlpacaVenueProtocolError(
                f"unsupported Alpaca broker trade update event: {event_name!r}"
            )
        status, execution_type = outcome
        order = _required_mapping(data, "order")
        execution = data.get("execution")
        if execution is not None and not isinstance(execution, Mapping):
            raise AlpacaVenueProtocolError("Alpaca field execution must be an object")
        event_timestamp = _update_timestamp(data, order, execution)
        self._check_timestamp(event_timestamp, observed_at)
        client_order_id = _required_text(order, "client_order_id")
        symbol = str(Symbol(_required_text(order, "symbol")))
        order_quantity = _required_positive_decimal(order, "qty")
        filled_quantity = _required_nonnegative_decimal(order, "filled_qty")
        if filled_quantity > order_quantity:
            raise AlpacaVenueProtocolError("Alpaca field filled_qty cannot exceed qty")
        average_fill_price = _optional_positive_decimal(order, "filled_avg_price")
        binding = self._directory.binding_for_client(client_order_id)
        order_id = binding.order_id if binding is not None else client_order_id

        fill_quantity = _ZERO
        fill_price: Decimal | None = None
        execution_id: str | None = None
        if execution_type in _FILL_EXECUTION_TYPES:
            if execution is None:
                raise AlpacaVenueProtocolError(
                    f"Alpaca {event_name} update requires an execution object"
                )
            execution_id = _required_text(execution, "id")
            fill_quantity = _required_positive_decimal(execution, "qty")
            fill_price = _required_positive_decimal(execution, "price")

        reports: list[BrokerEvent] = []
        if event_name == "replaced" and binding is not None:
            reports.extend(
                self._replaced_reports(
                    binding=binding,
                    event_name=event_name,
                    order_quantity=order_quantity,
                    filled_quantity=filled_quantity,
                    average_fill_price=average_fill_price,
                    event_timestamp=event_timestamp,
                    first_sequence=first_sequence,
                )
            )
        else:
            reports.append(
                self._report(
                    event_name=event_name,
                    status=status,
                    execution_type=execution_type,
                    order_id=order_id,
                    client_order_id=client_order_id,
                    order_quantity=order_quantity,
                    filled_quantity=filled_quantity,
                    average_fill_price=average_fill_price,
                    event_timestamp=event_timestamp,
                    sequence=first_sequence,
                    fill_quantity=fill_quantity,
                    fill_price=fill_price,
                )
            )
        if execution_type in _FILL_EXECUTION_TYPES:
            assert execution_id is not None
            assert fill_price is not None
            fill_sequence = first_sequence + len(reports)
            fill_id = self._event_id(
                "fill", execution_id, event_timestamp, fill_sequence
            )
            reports.append(
                Fill(
                    event_id=fill_id,
                    timestamp=event_timestamp,
                    sequence=fill_sequence,
                    fill_id=fill_id,
                    order_id=order_id,
                    symbol=Symbol(symbol),
                    side=Side(_required_text(order, "side")),
                    quantity=fill_quantity,
                    price=fill_price,
                )
            )
        return tuple(reports)

    def _replaced_reports(
        self,
        *,
        binding: AlpacaOrderBinding,
        event_name: str,
        order_quantity: Decimal,
        filled_quantity: Decimal,
        average_fill_price: Decimal | None,
        event_timestamp: datetime,
        first_sequence: int,
    ) -> tuple[BrokerEvent, ...]:
        chain = self._directory.chain_for_client(binding.client_order_id)
        reports: list[BrokerEvent] = []
        if len(chain) >= 2:
            previous = chain[-2]
            if filled_quantity > previous.quantity:
                raise AlpacaVenueProtocolError(
                    "Alpaca replaced order fills exceed the replaced quantity"
                )
            reports.append(
                self._report(
                    event_name=event_name,
                    status=OrderStatus.REPLACED,
                    execution_type=ExecutionType.REPLACED,
                    order_id=previous.order_id,
                    client_order_id=previous.client_order_id,
                    order_quantity=previous.quantity,
                    filled_quantity=filled_quantity,
                    average_fill_price=average_fill_price,
                    event_timestamp=event_timestamp,
                    sequence=first_sequence,
                    fill_quantity=_ZERO,
                    fill_price=None,
                )
            )
            acknowledged_sequence = first_sequence + 1
        else:
            reports.append(
                self._report(
                    event_name=event_name,
                    status=OrderStatus.REPLACED,
                    execution_type=ExecutionType.REPLACED,
                    order_id=binding.order_id,
                    client_order_id=binding.client_order_id,
                    order_quantity=order_quantity,
                    filled_quantity=filled_quantity,
                    average_fill_price=average_fill_price,
                    event_timestamp=event_timestamp,
                    sequence=first_sequence,
                    fill_quantity=_ZERO,
                    fill_price=None,
                )
            )
            acknowledged_sequence = -1
        if acknowledged_sequence >= 0:
            reports.append(
                self._report(
                    event_name=event_name,
                    status=OrderStatus.ACCEPTED,
                    execution_type=ExecutionType.ACKNOWLEDGED,
                    order_id=binding.order_id,
                    client_order_id=binding.client_order_id,
                    order_quantity=order_quantity,
                    filled_quantity=filled_quantity,
                    average_fill_price=average_fill_price,
                    event_timestamp=event_timestamp,
                    sequence=acknowledged_sequence,
                    fill_quantity=_ZERO,
                    fill_price=None,
                )
            )
        return tuple(reports)

    def _report(
        self,
        *,
        event_name: str,
        status: OrderStatus,
        execution_type: ExecutionType,
        order_id: str,
        client_order_id: str,
        order_quantity: Decimal,
        filled_quantity: Decimal,
        average_fill_price: Decimal | None,
        event_timestamp: datetime,
        sequence: int,
        fill_quantity: Decimal,
        fill_price: Decimal | None,
    ) -> ExecutionReport:
        report_id = self._event_id("report", client_order_id, event_timestamp, sequence)
        return ExecutionReport(
            event_id=report_id,
            timestamp=event_timestamp,
            sequence=sequence,
            report_id=report_id,
            order_id=order_id,
            status=status,
            execution_type=execution_type,
            order_quantity=order_quantity,
            filled_quantity=filled_quantity,
            remaining_quantity=order_quantity - filled_quantity,
            average_fill_price=average_fill_price,
            last_fill_quantity=fill_quantity,
            last_fill_price=fill_price,
            text=f"alpaca:{event_name}",
        )

    def _event_id(
        self, kind: str, reference: str, moment: datetime, sequence: int
    ) -> str:
        return ":".join(
            (
                self.event_source,
                kind,
                reference,
                moment.isoformat(timespec="microseconds"),
                str(sequence),
            )
        )

    def _check_timestamp(self, moment: datetime, observed_at: datetime) -> None:
        if moment > observed_at + self._max_future_skew:
            raise AlpacaVenueProtocolError(
                "Alpaca broker timestamp exceeds the allowed future skew"
            )
        if moment < observed_at - self._max_event_age:
            raise AlpacaVenueProtocolError(
                "Alpaca broker timestamp exceeds the maximum event age"
            )


class AlpacaBrokerRest:
    """Async Alpaca broker REST command client with injectable transport."""

    paper_broker_url: ClassVar[str] = "https://paper-api.alpaca.markets"
    live_broker_url: ClassVar[str] = "https://api.alpaca.markets"

    _RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})

    __slots__ = (
        "_backoff_seconds",
        "_default_client",
        "_honor_retry_after",
        "_headers",
        "_max_attempts",
        "_max_backoff_seconds",
        "_semaphore",
        "_sleep",
        "_timeout_seconds",
        "_transport",
        "base_url",
    )

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        transport: AsyncBrokerTransport | None = None,
        base_url: str = paper_broker_url,
        max_concurrency: int = 4,
        timeout_seconds: float = 30.0,
        max_attempts: int = 5,
        honor_retry_after: bool = True,
        backoff_seconds: float = 0.25,
        max_backoff_seconds: float = 30.0,
        sleep: BrokerSleep | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("api_key must not be empty")
        if not api_secret or not api_secret.strip():
            raise ValueError("api_secret must not be empty")
        if not base_url or not base_url.strip():
            raise ValueError("base_url must not be empty")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if backoff_seconds < 0 or max_backoff_seconds < 0:
            raise ValueError("backoff delays must not be negative")
        self.base_url = base_url.rstrip("/")
        self._timeout_seconds = float(timeout_seconds)
        self._max_attempts = max_attempts
        self._honor_retry_after = honor_retry_after
        self._backoff_seconds = backoff_seconds
        self._max_backoff_seconds = max_backoff_seconds
        self._sleep = asyncio.sleep if sleep is None else sleep
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._default_client: httpx.AsyncClient | None = None
        if transport is None:
            self._default_client = httpx.AsyncClient(timeout=timeout_seconds)
            self._transport: AsyncBrokerTransport = cast(
                "AsyncBrokerTransport", self._default_client
            )
        else:
            self._transport = transport
        self._headers: dict[str, str] = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": api_secret,
            "Accept": "application/json",
        }

    async def __aenter__(self) -> AlpacaBrokerRest:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the internally-created HTTP client, if there is one."""
        if self._default_client is not None:
            client = self._default_client
            self._default_client = None
            await client.aclose()

    async def submit(self, order: SingleLegOrder, *, client_order_id: str) -> None:
        """Map a submit command to ``POST /v2/orders``."""
        payload = _order_payload(order, client_order_id)
        await self._request("POST", "/v2/orders", json_body=payload)

    async def cancel(self, *, identity: str) -> None:
        """Map a cancel command to ``DELETE /v2/orders/{identity}``."""
        path = f"/v2/orders/{quote(identity, safe='')}"
        await self._request("DELETE", path)

    async def replace(
        self,
        *,
        identity: str,
        replacement: SingleLegOrder,
        client_order_id: str,
    ) -> None:
        """Map a replace command to ``PATCH /v2/orders/{identity}``."""
        path = f"/v2/orders/{quote(identity, safe='')}"
        payload = _replace_payload(replacement, client_order_id)
        await self._request("PATCH", path, json_body=payload)

    async def positions(self) -> tuple[Mapping[str, object], ...]:
        """Fetch the account positions array from ``GET /v2/positions``."""
        response = await self._request("GET", "/v2/positions")
        return _position_rows(response.json())

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: QueryParams | None = None,
        json_body: Mapping[str, str] | None = None,
    ) -> AsyncBrokerResponse:
        url = f"{self.base_url}{path}"
        query: QueryParams = {} if params is None else params
        for attempt in range(1, self._max_attempts + 1):
            try:
                async with self._semaphore:
                    response = await self._transport.request(
                        method,
                        url,
                        params=query,
                        json=json_body,
                        headers=self._headers,
                        timeout=self._timeout_seconds,
                    )
            except (httpx.TransportError, TimeoutError):
                if attempt == self._max_attempts:
                    raise
                await self._sleep(self._backoff(attempt))
                continue
            status = response.status_code
            if 200 <= status < 300:
                return response
            if status not in self._RETRYABLE_STATUS_CODES:
                raise AlpacaBrokerHTTPError(status, url)
            if attempt == self._max_attempts:
                raise AlpacaBrokerHTTPError(status, url)
            delay = self._backoff(attempt)
            if status == 429 and self._honor_retry_after:
                retry_after = _retry_after_seconds(response.headers)
                if retry_after is not None:
                    delay = min(retry_after, self._max_backoff_seconds)
            await self._sleep(delay)
        raise AssertionError("retry loop must return or raise")

    def _backoff(self, attempt: int) -> float:
        return min(
            self._backoff_seconds * (2 ** (attempt - 1)),
            self._max_backoff_seconds,
        )


class AlpacaBroker:
    """Async fire-and-forget Alpaca equity broker boundary.

    Commands map one-way to the Alpaca broker REST API and return command
    receipts. Outcomes arrive only as kernel events on the supplied
    :class:`AlpacaExecutionInbox`. The venue exposes no order book, working
    state, or venue-status introspection, keeping live adapters on the same
    structural boundary as the simulated venue.
    """

    __slots__ = ("_client_ids", "_clock", "_inbox", "_rest")

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        inbox: AlpacaExecutionInbox,
        clock: BrokerClock,
        transport: AsyncBrokerTransport | None = None,
        base_url: str = AlpacaBrokerRest.paper_broker_url,
        strategy_id: str = "unattributed",
        client_ids: AlpacaClientOrderIdSource | None = None,
        max_concurrency: int = 4,
        timeout_seconds: float = 30.0,
        max_attempts: int = 5,
        honor_retry_after: bool = True,
        backoff_seconds: float = 0.25,
        max_backoff_seconds: float = 30.0,
        sleep: BrokerSleep | None = None,
    ) -> None:
        if not isinstance(inbox, AlpacaExecutionInbox):
            raise TypeError("inbox must be an AlpacaExecutionInbox")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._inbox = inbox
        self._clock = clock
        self._client_ids = (
            AlpacaClientOrderIdSource(strategy_id=strategy_id)
            if client_ids is None
            else client_ids
        )
        self._rest = AlpacaBrokerRest(
            api_key=api_key,
            api_secret=api_secret,
            transport=transport,
            base_url=base_url,
            max_concurrency=max_concurrency,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            honor_retry_after=honor_retry_after,
            backoff_seconds=backoff_seconds,
            max_backoff_seconds=max_backoff_seconds,
            sleep=sleep,
        )

    async def __aenter__(self) -> AlpacaBroker:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Release the REST transport resources; idempotent."""
        await self._rest.aclose()

    async def submit(self, order: OrderRequest) -> AlpacaCommandReceipt:
        """Dispatch one single-leg equity submit and return its receipt."""
        single = _require_equity_single_leg(order, "order")
        client_order_id = (
            single.client_order_id
            if single.client_order_id is not None
            else self._client_ids.next_client_order_id()
        )
        self._inbox.directory.bind(
            AlpacaOrderBinding(
                order_id=single.order_id,
                client_order_id=client_order_id,
                symbol=str(single.symbol),
                side=single.side,
                quantity=single.quantity,
                order_type=single.order_type,
            )
        )
        await self._rest.submit(single, client_order_id=client_order_id)
        return AlpacaCommandReceipt(
            VenueOperation.SUBMIT,
            order_id=single.order_id,
            client_order_id=client_order_id,
        )

    async def cancel(self, order_id: str) -> AlpacaCommandReceipt:
        """Dispatch one cancel and return its receipt."""
        order_id = text(order_id, "order_id")
        client_order_id = self._inbox.directory.client_order_id_for(order_id)
        if client_order_id is None:
            raise UnknownAlpacaOrderError(order_id)
        await self._rest.cancel(identity=client_order_id)
        return AlpacaCommandReceipt(
            VenueOperation.CANCEL,
            order_id=order_id,
            client_order_id=client_order_id,
        )

    async def replace(
        self, order_id: str, replacement: OrderRequest
    ) -> AlpacaCommandReceipt:
        """Dispatch one atomic replacement and return its receipt."""
        single = _require_equity_single_leg(replacement, "replacement")
        order_id = text(order_id, "order_id")
        if single.order_id == order_id:
            raise AlpacaVenueError(
                f"replacement requires a new kernel order_id, not {order_id!r}"
            )
        directory = self._inbox.directory
        previous_client = directory.client_order_id_for(order_id)
        if previous_client is None:
            raise UnknownAlpacaOrderError(order_id)
        previous = directory.binding_for_client(previous_client)
        if previous is None:
            raise UnknownAlpacaOrderError(order_id)
        if single.symbol != previous.symbol:
            raise AlpacaVenueError("replacement cannot change symbol")
        if single.side is not previous.side:
            raise AlpacaVenueError("replacement cannot change side")
        if single.order_type is not previous.order_type:
            raise AlpacaVenueError(
                "replacement cannot change order type; cancel and submit instead"
            )
        client_order_id = (
            single.client_order_id
            if single.client_order_id is not None
            else self._client_ids.next_client_order_id()
        )
        binding = AlpacaOrderBinding(
            order_id=single.order_id,
            client_order_id=client_order_id,
            symbol=str(single.symbol),
            side=single.side,
            quantity=single.quantity,
            order_type=single.order_type,
        )
        directory.link_replacement(previous_client, binding)
        await self._rest.replace(
            identity=previous_client,
            replacement=single,
            client_order_id=client_order_id,
        )
        return AlpacaCommandReceipt(
            VenueOperation.REPLACE,
            order_id=single.order_id,
            client_order_id=client_order_id,
        )

    async def request_position_snapshot(self, account_id: str) -> AlpacaCommandReceipt:
        """Fetch positions and enqueue one Position event per row."""
        account_id = text(account_id, "account_id")
        rows = await self._rest.positions()
        self._inbox.push_positions(
            rows,
            observed_at=self._now(),
            account_id=account_id,
        )
        return AlpacaCommandReceipt(
            VenueOperation.POSITION_SNAPSHOT, account_id=account_id
        )

    def _now(self) -> datetime:
        return timestamp(self._clock(), "broker clock")


AlpacaVenue = AlpacaBroker


def _require_equity_single_leg(order: OrderRequest, field_name: str) -> SingleLegOrder:
    if isinstance(order, MultiLegOrder):
        raise AlpacaVenueError("Alpaca equities support single-leg orders only")
    if not isinstance(order, SingleLegOrder):
        raise TypeError(f"{field_name} must be an OrderRequest")
    symbol = str(order.symbol)
    if "/" in symbol:
        raise AlpacaVenueError(f"Alpaca equities do not trade crypto pairs: {symbol!r}")
    if is_osi_symbol(symbol):
        raise AlpacaVenueError(
            f"Alpaca equities do not trade option contracts: {symbol!r}"
        )
    return order


def _order_payload(order: SingleLegOrder, client_order_id: str) -> dict[str, str]:
    payload = {
        "symbol": str(order.symbol),
        "qty": str(order.quantity),
        "side": order.side.value,
        "type": order.order_type.value,
        "time_in_force": order.time_in_force.value,
        "client_order_id": client_order_id,
    }
    if order.limit_price is not None:
        payload["limit_price"] = str(order.limit_price)
    if order.stop_price is not None:
        payload["stop_price"] = str(order.stop_price)
    return payload


def _replace_payload(
    replacement: SingleLegOrder, client_order_id: str
) -> dict[str, str]:
    payload = {
        "qty": str(replacement.quantity),
        "time_in_force": replacement.time_in_force.value,
        "client_order_id": client_order_id,
    }
    if replacement.limit_price is not None:
        payload["limit_price"] = str(replacement.limit_price)
    if replacement.stop_price is not None:
        payload["stop_price"] = str(replacement.stop_price)
    return payload


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    value: str | None = None
    for key, header_value in headers.items():
        if key.lower() == "retry-after":
            value = header_value
            break
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    if seconds < 0 or seconds != seconds:
        return None
    return seconds


def _position_rows(payload: object) -> tuple[Mapping[str, object], ...]:
    if isinstance(payload, Mapping):
        raise AlpacaVenueProtocolError("Alpaca positions response must be an array")
    if isinstance(payload, str | bytes) or not isinstance(payload, Sequence):
        raise AlpacaVenueProtocolError("Alpaca positions response must be an array")
    if not all(isinstance(row, Mapping) for row in payload):
        raise AlpacaVenueProtocolError("Alpaca positions rows must be objects")
    return tuple(cast("Mapping[str, object]", row) for row in payload)


def _required(mapping: Mapping[str, object], field_name: str) -> object:
    try:
        return mapping[field_name]
    except KeyError as error:
        raise AlpacaVenueProtocolError(
            f"Alpaca broker frame is missing {field_name!r}"
        ) from error


def _required_text(mapping: Mapping[str, object], field_name: str) -> str:
    value = _required(mapping, field_name)
    if not isinstance(value, str) or not value.strip():
        raise AlpacaVenueProtocolError(
            f"Alpaca field {field_name} must be non-empty text"
        )
    return value.strip()


def _required_mapping(
    mapping: Mapping[str, object], field_name: str
) -> Mapping[str, object]:
    value = _required(mapping, field_name)
    if not isinstance(value, Mapping):
        raise AlpacaVenueProtocolError(f"Alpaca field {field_name} must be an object")
    return value


def _decimal_value(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal | str | int):
        raise AlpacaVenueProtocolError(
            f"Alpaca field {field_name} must be an exact number"
        )
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise AlpacaVenueProtocolError(
            f"Alpaca field {field_name} must be an exact number"
        ) from error
    if not result.is_finite():
        raise AlpacaVenueProtocolError(
            f"Alpaca field {field_name} must be a finite number"
        )
    return result


def _required_decimal(mapping: Mapping[str, object], field_name: str) -> Decimal:
    result = _decimal_value(_required(mapping, field_name), field_name)
    return result


def _required_positive_decimal(
    mapping: Mapping[str, object], field_name: str
) -> Decimal:
    result = _required_decimal(mapping, field_name)
    if result <= 0:
        raise AlpacaVenueProtocolError(
            f"Alpaca field {field_name} must be a positive number"
        )
    return result


def _required_nonnegative_decimal(
    mapping: Mapping[str, object], field_name: str
) -> Decimal:
    result = _required_decimal(mapping, field_name)
    if result < 0:
        raise AlpacaVenueProtocolError(
            f"Alpaca field {field_name} must not be negative"
        )
    return result


def _optional_decimal(mapping: Mapping[str, object], field_name: str) -> Decimal | None:
    value = mapping.get(field_name)
    if value is None:
        return None
    return _decimal_value(value, field_name)


def _optional_positive_decimal(
    mapping: Mapping[str, object], field_name: str
) -> Decimal | None:
    value = mapping.get(field_name)
    if value is None:
        return None
    result = _decimal_value(value, field_name)
    if result <= 0:
        raise AlpacaVenueProtocolError(
            f"Alpaca field {field_name} must be a positive number or null"
        )
    return result


def _optional_text(mapping: Mapping[str, object], field_name: str) -> str | None:
    value = mapping.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AlpacaVenueProtocolError(
            f"Alpaca field {field_name} must be non-empty text or null"
        )
    return value.strip()


def _provider_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise AlpacaVenueProtocolError(
            f"Alpaca field {field_name} must be an ISO timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return timestamp(parsed, f"Alpaca field {field_name}")
    except (TypeError, ValueError) as error:
        raise AlpacaVenueProtocolError(
            f"Alpaca field {field_name} must be a timezone-aware ISO timestamp"
        ) from error


def _update_timestamp(
    data: Mapping[str, object],
    order: Mapping[str, object],
    execution: Mapping[str, object] | None,
) -> datetime:
    candidates = (
        data.get("at"),
        None if execution is None else execution.get("timestamp"),
        order.get("updated_at"),
    )
    for candidate in candidates:
        if isinstance(candidate, str):
            try:
                return _provider_timestamp(candidate, "at")
            except AlpacaVenueProtocolError:
                continue
    raise AlpacaVenueProtocolError(
        "Alpaca broker trade update requires a usable ISO timestamp"
    )


def _nonnegative_duration(value: timedelta, field_name: str) -> None:
    if not isinstance(value, timedelta) or value < timedelta(0):
        raise ValueError(f"{field_name} must be a non-negative timedelta")


def _positive_duration(value: timedelta, field_name: str) -> None:
    if not isinstance(value, timedelta) or value <= timedelta(0):
        raise ValueError(f"{field_name} must be a positive timedelta")


__all__ = [
    "AlpacaBroker",
    "AlpacaBrokerHTTPError",
    "AlpacaBrokerRest",
    "AlpacaClientOrderIdSource",
    "AlpacaCommandReceipt",
    "AlpacaExecutionInbox",
    "AlpacaOrderBinding",
    "AlpacaOrderDirectory",
    "AlpacaVenue",
    "AlpacaVenueError",
    "AlpacaVenueProtocolError",
    "AsyncBrokerResponse",
    "AsyncBrokerTransport",
    "BrokerClock",
    "BrokerSleep",
    "BrokerEvent",
    "QueryParams",
    "UnknownAlpacaOrderError",
]
