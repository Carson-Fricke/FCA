"""Transport-neutral Alpaca WebSocket feeds for the live async shell.

The WebSocket connection and async clock are ports supplied by the deployment
shell. ``AlpacaSipInbox`` and ``AlpacaCryptoInbox`` are deliberately
synchronous: they perform no network, clock, sleep, or random access while
producing the same market event records used by replay. The SIP feed serves
XNYS-session equities, while the crypto feed normalizes fractional quotes and
trades continuously across every UTC day.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import ClassVar, Protocol, Self, cast

from harness.configuration.models import TraderConfig
from harness.core.symbols import Symbol
from harness.core.types.common import timestamp
from harness.core.types.events import QuoteEvent, TradeEvent

type AsyncClock = Callable[[], datetime]
type AsyncSleep = Callable[[float], Awaitable[None]]
type MarketEvent = QuoteEvent | TradeEvent
type StaleHandler = Callable[[tuple[str, ...]], None]


class AsyncWebSocketConnection(Protocol):
    """A decoded-timeout-capable connection implemented by a network adapter."""

    async def send(self, message: str) -> None:
        """Send one JSON text message."""

    async def receive(self, timeout_seconds: float) -> str | bytes | None:
        """Receive one frame, returning ``None`` when the timeout expires."""

    async def close(self) -> None:
        """Close the connection."""


class AsyncWebSocketTransport(Protocol):
    """Connection factory kept outside the deterministic market-data kernel."""

    async def connect(self, url: str) -> AsyncWebSocketConnection:
        """Open one WebSocket connection to ``url``."""


class AlpacaWebSocketProtocolError(RuntimeError):
    """Alpaca sent an invalid frame or rejected a stream operation."""


@dataclass(frozen=True, slots=True)
class AlpacaSipSubscription:
    """Canonical equity channels requested by one trader deployment."""

    trades: tuple[str, ...]
    quotes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "trades", _canonical_symbols(self.trades, "trades"))
        object.__setattr__(self, "quotes", _canonical_symbols(self.quotes, "quotes"))
        if not self.trades and not self.quotes:
            raise ValueError("at least one SIP trade or quote subscription is required")

    @property
    def symbols(self) -> tuple[str, ...]:
        """Return every symbol monitored by the stale watchdog."""
        return tuple(sorted(set(self.trades) | set(self.quotes)))

    @classmethod
    def from_trader_config(cls, config: TraderConfig) -> AlpacaSipSubscription:
        """Derive subscriptions from the deployed strategy symbol universes."""
        if not isinstance(config, TraderConfig):
            raise TypeError("config must be a TraderConfig")
        if config.deployment.network_access != "runtime_only":
            raise ValueError("Alpaca SIP requires runtime network access")
        if config.market_data.settings.get("feed") != "sip":
            raise ValueError("Alpaca equity WebSocket feed must be configured as sip")
        symbols = _deployment_symbols(config)
        return cls(trades=symbols, quotes=symbols)


@dataclass(frozen=True, slots=True)
class AlpacaCryptoSubscription(AlpacaSipSubscription):
    """Canonical crypto trading pairs requested by one trader deployment."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "trades", _crypto_pairs(self.trades, "trades"))
        object.__setattr__(self, "quotes", _crypto_pairs(self.quotes, "quotes"))
        if not self.trades and not self.quotes:
            raise ValueError(
                "at least one crypto trade or quote subscription is required"
            )

    @classmethod
    def from_trader_config(cls, config: TraderConfig) -> AlpacaCryptoSubscription:
        """Derive always-on pair subscriptions from the deployed strategy universes."""
        if not isinstance(config, TraderConfig):
            raise TypeError("config must be a TraderConfig")
        if config.deployment.network_access != "runtime_only":
            raise ValueError("Alpaca crypto requires runtime network access")
        if config.market_data.settings.get("feed") != "crypto":
            raise ValueError(
                "Alpaca crypto WebSocket feed must be configured as crypto"
            )
        pairs = _crypto_pairs(_deployment_symbols(config), "strategy symbols")
        return cls(trades=pairs, quotes=pairs)


class AlpacaSipInbox:
    """Synchronous, deterministic normalizer and kernel-facing event inbox."""

    event_source: ClassVar[str] = "alpaca-sip"

    __slots__ = (
        "_events",
        "_last_observed",
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
        self._events: deque[MarketEvent] = deque()
        self._last_observed: dict[str, datetime] = {}

    def __len__(self) -> int:
        return len(self._events)

    @property
    def next_sequence(self) -> int:
        """Return the sequence that the next accepted event will receive."""
        return self._next_sequence

    def push(
        self,
        payload: Mapping[str, object] | Sequence[Mapping[str, object]],
        *,
        observed_at: datetime,
    ) -> tuple[MarketEvent, ...]:
        """Validate and atomically enqueue one decoded provider frame."""
        observed = timestamp(observed_at, "observed_at")
        messages = _message_sequence(payload)
        events = tuple(
            self._normalize(message, observed, self._next_sequence + offset)
            for offset, message in enumerate(messages)
        )
        for event in events:
            self._events.append(event)
            self._last_observed[str(event.symbol)] = observed
        self._next_sequence += len(events)
        return events

    def pop(self) -> MarketEvent | None:
        """Remove the oldest normalized event, if one is available."""
        return None if not self._events else self._events.popleft()

    def drain(self, limit: int | None = None) -> tuple[MarketEvent, ...]:
        """Remove events in provider order without consulting a clock."""
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
        ):
            raise ValueError("limit must be a non-negative integer or None")
        drained: list[MarketEvent] = []
        while self._events and (limit is None or len(drained) < limit):
            drained.append(self._events.popleft())
        return tuple(drained)

    def stale_symbols(
        self,
        symbols: Sequence[str],
        *,
        observed_at: datetime,
        active_since: datetime,
        stale_after: timedelta,
    ) -> tuple[str, ...]:
        """Find stale symbols using only timestamps supplied by the async shell."""
        now = timestamp(observed_at, "observed_at")
        started = timestamp(active_since, "active_since")
        _positive_duration(stale_after, "stale_after")
        if now < started:
            raise ValueError("observed_at cannot precede active_since")
        stale: list[str] = []
        for symbol in _canonical_symbols(symbols, "symbols"):
            latest = max(started, self._last_observed.get(symbol, started))
            if now - latest >= stale_after:
                stale.append(symbol)
        return tuple(stale)

    def _event_symbol(self, raw: str) -> str:
        """Return the canonical symbol carried by one provider event."""
        return str(Symbol(raw))

    def _normalize(
        self, message: Mapping[str, object], observed_at: datetime, sequence: int
    ) -> MarketEvent:
        message_type = _required_text(message, "T")
        if message_type not in {"q", "t"}:
            raise AlpacaWebSocketProtocolError(
                f"unsupported Alpaca market-data message type: {message_type}"
            )
        symbol = self._event_symbol(_required_text(message, "S"))
        event_timestamp = _provider_timestamp(message.get("t"))
        if event_timestamp > observed_at + self._max_future_skew:
            raise AlpacaWebSocketProtocolError(
                f"Alpaca timestamp for {symbol} exceeds the allowed future skew"
            )
        if event_timestamp < observed_at - self._max_event_age:
            raise AlpacaWebSocketProtocolError(
                f"Alpaca timestamp for {symbol} exceeds the maximum event age"
            )
        event_id = ":".join(
            (
                self.event_source,
                message_type,
                symbol,
                event_timestamp.isoformat(timespec="microseconds"),
                str(sequence),
            )
        )
        if message_type == "q":
            return QuoteEvent(
                event_id=event_id,
                timestamp=event_timestamp,
                sequence=sequence,
                symbol=Symbol(symbol),
                bid_price=_required_decimal(message, "bp"),
                bid_quantity=_required_decimal(message, "bs"),
                ask_price=_required_decimal(message, "ap"),
                ask_quantity=_required_decimal(message, "as"),
                venue=_optional_text(message, "z"),
            )
        trade_id = message.get("i")
        if isinstance(trade_id, bool) or not isinstance(trade_id, str | int):
            raise AlpacaWebSocketProtocolError("Alpaca field i must be text or integer")
        return TradeEvent(
            event_id=event_id,
            timestamp=event_timestamp,
            sequence=sequence,
            symbol=Symbol(symbol),
            price=_required_decimal(message, "p"),
            quantity=_required_decimal(message, "s"),
            venue=_optional_text(message, "x"),
            trade_id=str(trade_id),
        )


class AlpacaCryptoInbox(AlpacaSipInbox):
    """Crypto normalizer for fractional quotes and trades on every UTC day.

    Event identities carry crypto provenance, so pair this inbox with
    ``AlpacaCryptoWebSocketFeed``. Crypto quotes carry no consolidated tape, and
    fractional prices and sizes stay exact ``Decimal`` values.
    """

    __slots__ = ()

    event_source: ClassVar[str] = "alpaca-crypto"

    def _event_symbol(self, raw: str) -> str:
        symbol = str(Symbol(raw))
        if not _is_crypto_pair(symbol):
            raise AlpacaWebSocketProtocolError(
                f"Alpaca field S must use BASE/QUOTE crypto pairs: {raw!r}"
            )
        return symbol


class AlpacaSipWebSocketFeed:
    """Async Alpaca shell with auth, reconnect backoff, and stale monitoring."""

    subscription_type: ClassVar[type[AlpacaSipSubscription]] = AlpacaSipSubscription
    default_websocket_url: ClassVar[str] = "wss://stream.data.alpaca.markets/v2/sip"

    __slots__ = (
        "_api_key",
        "_api_secret",
        "_backoff_seconds",
        "_clock",
        "_handshake_timeout",
        "_inbox",
        "_max_backoff_seconds",
        "_on_stale",
        "_sleep",
        "_stale_after",
        "_stale_symbols",
        "_subscription",
        "_transport",
        "_watchdog_interval",
        "_websocket_url",
        "connection_attempts",
        "last_error",
    )

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        subscription: AlpacaSipSubscription,
        transport: AsyncWebSocketTransport,
        inbox: AlpacaSipInbox,
        clock: AsyncClock,
        sleep: AsyncSleep = asyncio.sleep,
        websocket_url: str | None = None,
        backoff_seconds: float = 0.5,
        max_backoff_seconds: float = 30.0,
        watchdog_interval: float = 1.0,
        stale_after: timedelta = timedelta(seconds=30),
        handshake_timeout: timedelta = timedelta(seconds=10),
        on_stale: StaleHandler | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("api_key must not be empty")
        if not api_secret or not api_secret.strip():
            raise ValueError("api_secret must not be empty")
        if websocket_url is None:
            websocket_url = self.default_websocket_url
        if not websocket_url or not websocket_url.strip():
            raise ValueError("websocket_url must not be empty")
        if backoff_seconds < 0 or max_backoff_seconds < 0:
            raise ValueError("backoff delays must not be negative")
        if watchdog_interval <= 0:
            raise ValueError("watchdog_interval must be positive")
        _positive_duration(stale_after, "stale_after")
        _positive_duration(handshake_timeout, "handshake_timeout")
        self._api_key = api_key
        self._api_secret = api_secret
        self._subscription = subscription
        self._transport = transport
        self._inbox = inbox
        self._clock = clock
        self._sleep = sleep
        self._websocket_url = websocket_url.rstrip("/")
        self._backoff_seconds = backoff_seconds
        self._max_backoff_seconds = max_backoff_seconds
        self._watchdog_interval = watchdog_interval
        self._stale_after = stale_after
        self._handshake_timeout = handshake_timeout
        self._on_stale = on_stale
        self._stale_symbols: tuple[str, ...] = ()
        self.connection_attempts = 0
        self.last_error: Exception | None = None

    @classmethod
    def from_trader_config(
        cls,
        config: TraderConfig,
        *,
        api_key: str,
        api_secret: str,
        transport: AsyncWebSocketTransport,
        inbox: AlpacaSipInbox,
        clock: AsyncClock,
        sleep: AsyncSleep = asyncio.sleep,
        websocket_url: str | None = None,
        backoff_seconds: float = 0.5,
        max_backoff_seconds: float = 30.0,
        watchdog_interval: float = 1.0,
        stale_after: timedelta = timedelta(seconds=30),
        handshake_timeout: timedelta = timedelta(seconds=10),
        on_stale: StaleHandler | None = None,
    ) -> Self:
        """Build a feed whose channels follow the deployed strategy universes."""
        return cls(
            api_key=api_key,
            api_secret=api_secret,
            subscription=cls.subscription_type.from_trader_config(config),
            transport=transport,
            inbox=inbox,
            clock=clock,
            sleep=sleep,
            websocket_url=websocket_url,
            backoff_seconds=backoff_seconds,
            max_backoff_seconds=max_backoff_seconds,
            watchdog_interval=watchdog_interval,
            stale_after=stale_after,
            handshake_timeout=handshake_timeout,
            on_stale=on_stale,
        )

    @property
    def stale_symbols(self) -> tuple[str, ...]:
        """Return the most recent watchdog result."""
        return self._stale_symbols

    async def run(
        self,
        stop: asyncio.Event,
        *,
        max_connections: int | None = None,
    ) -> None:
        """Consume until stopped, reconnecting failures with bounded backoff."""
        if max_connections is not None and max_connections < 1:
            raise ValueError("max_connections must be at least 1 or None")
        failures = 0
        while not stop.is_set():
            if (
                max_connections is not None
                and self.connection_attempts >= max_connections
            ):
                if self.last_error is not None:
                    raise self.last_error
                return
            self.connection_attempts += 1
            try:
                await self._run_connection(stop)
            except Exception as error:
                self.last_error = error
                failures += 1
                if stop.is_set():
                    return
                if (
                    max_connections is not None
                    and self.connection_attempts >= max_connections
                ):
                    raise
                delay = min(
                    self._max_backoff_seconds,
                    self._backoff_seconds * (2 ** (failures - 1)),
                )
                await self._sleep(delay)
            else:
                return

    async def _run_connection(self, stop: asyncio.Event) -> None:
        connection = await self._transport.connect(self._websocket_url)
        try:
            if not await self._await_control(connection, stop, "connected"):
                return
            await _send_json(
                connection,
                {"action": "auth", "key": self._api_key, "secret": self._api_secret},
            )
            if not await self._await_control(connection, stop, "authenticated"):
                return
            await _send_json(
                connection,
                {
                    "action": "subscribe",
                    "trades": list(self._subscription.trades),
                    "quotes": list(self._subscription.quotes),
                },
            )
            if not await self._await_subscription(connection, stop):
                return
            active_since = self._now()
            self._stale_symbols = ()
            while not stop.is_set():
                messages = await _receive_json(connection, self._watchdog_interval)
                if messages is not None:
                    market_messages = _market_messages(messages)
                    if market_messages:
                        self._inbox.push(market_messages, observed_at=self._now())
                self._check_stale(active_since)
        finally:
            await connection.close()

    async def _await_control(
        self,
        connection: AsyncWebSocketConnection,
        stop: asyncio.Event,
        expected: str,
    ) -> bool:
        started = self._now()
        while not stop.is_set():
            messages = await _receive_json(connection, self._watchdog_interval)
            if messages is not None:
                for message in messages:
                    _raise_provider_error(message)
                    if message.get("T") == "success" and message.get("msg") == expected:
                        return True
            if self._now() - started >= self._handshake_timeout:
                raise AlpacaWebSocketProtocolError(
                    f"timed out waiting for Alpaca {expected} response"
                )
        return False

    async def _await_subscription(
        self, connection: AsyncWebSocketConnection, stop: asyncio.Event
    ) -> bool:
        started = self._now()
        while not stop.is_set():
            messages = await _receive_json(connection, self._watchdog_interval)
            if messages is not None:
                subscribed = False
                for message in messages:
                    _raise_provider_error(message)
                    if message.get("T") == "subscription":
                        trades = _control_symbols(message.get("trades"), "trades")
                        quotes = _control_symbols(message.get("quotes"), "quotes")
                        if trades != self._subscription.trades:
                            raise AlpacaWebSocketProtocolError(
                                "Alpaca acknowledged unexpected trade subscriptions"
                            )
                        if quotes != self._subscription.quotes:
                            raise AlpacaWebSocketProtocolError(
                                "Alpaca acknowledged unexpected quote subscriptions"
                            )
                        subscribed = True
                if subscribed:
                    market_messages = _market_messages(messages)
                    if market_messages:
                        self._inbox.push(market_messages, observed_at=self._now())
                    return True
            if self._now() - started >= self._handshake_timeout:
                raise AlpacaWebSocketProtocolError(
                    "timed out waiting for Alpaca subscription response"
                )
        return False

    def _check_stale(self, active_since: datetime) -> None:
        stale = self._inbox.stale_symbols(
            self._subscription.symbols,
            observed_at=self._now(),
            active_since=active_since,
            stale_after=self._stale_after,
        )
        changed = stale != self._stale_symbols
        self._stale_symbols = stale
        if changed and stale and self._on_stale is not None:
            self._on_stale(stale)

    def _now(self) -> datetime:
        return timestamp(self._clock(), "async shell clock")


class AlpacaCryptoWebSocketFeed(AlpacaSipWebSocketFeed):
    """Async Alpaca crypto shell reusing the SIP auth and reconnect policy.

    Crypto trades continuously on every UTC day, so the feed applies no session
    gating and the stale watchdog relies only on timestamps supplied by the
    async shell. Pair channels follow the deployment configuration, and pair
    this feed with ``AlpacaCryptoInbox`` for crypto event provenance.
    """

    __slots__ = ()

    subscription_type = AlpacaCryptoSubscription
    default_websocket_url = "wss://stream.data.alpaca.markets/v1beta3/crypto"


def _canonical_symbols(symbols: Sequence[str], field_name: str) -> tuple[str, ...]:
    if isinstance(symbols, str):
        raise TypeError(f"{field_name} must be a sequence of symbols")
    values = tuple(sorted({str(Symbol(symbol)) for symbol in symbols}))
    if len(values) != len(symbols):
        raise ValueError(f"{field_name} must not contain duplicate symbols")
    return values


def _deployment_symbols(config: TraderConfig) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                symbol
                for instance in config.strategy_instances
                for symbol in instance.symbols
            }
        )
    )


def _is_crypto_pair(symbol: str) -> bool:
    base, separator, quote = symbol.partition("/")
    return bool(separator and base and quote and "/" not in quote)


def _crypto_pairs(symbols: Sequence[str], field_name: str) -> tuple[str, ...]:
    canonical = _canonical_symbols(symbols, field_name)
    for symbol in canonical:
        if not _is_crypto_pair(symbol):
            raise ValueError(
                f"{field_name} must use BASE/QUOTE crypto pairs, not {symbol!r}"
            )
    return canonical


def _message_sequence(
    payload: Mapping[str, object] | Sequence[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    if isinstance(payload, Mapping):
        return (payload,)
    if isinstance(payload, str | bytes) or not isinstance(payload, Sequence):
        raise AlpacaWebSocketProtocolError("Alpaca payload must contain JSON objects")
    if not payload:
        raise AlpacaWebSocketProtocolError("Alpaca payload must not be empty")
    if not all(isinstance(item, Mapping) for item in payload):
        raise AlpacaWebSocketProtocolError("Alpaca payload must contain JSON objects")
    return tuple(payload)


def _provider_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise AlpacaWebSocketProtocolError("Alpaca field t must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return timestamp(parsed, "Alpaca field t")
    except (TypeError, ValueError) as error:
        raise AlpacaWebSocketProtocolError(
            "Alpaca field t must be a timezone-aware ISO timestamp"
        ) from error


def _required_text(message: Mapping[str, object], field: str) -> str:
    value = message.get(field)
    if not isinstance(value, str) or not value.strip():
        raise AlpacaWebSocketProtocolError(
            f"Alpaca field {field} must be non-empty text"
        )
    return value.strip()


def _optional_text(message: Mapping[str, object], field: str) -> str | None:
    value = message.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AlpacaWebSocketProtocolError(
            f"Alpaca field {field} must be non-empty text or null"
        )
    return value.strip()


def _required_decimal(message: Mapping[str, object], field: str) -> Decimal:
    value = message.get(field)
    if isinstance(value, bool) or not isinstance(value, Decimal | str | int):
        raise AlpacaWebSocketProtocolError(
            f"Alpaca field {field} must be an exact number"
        )
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise AlpacaWebSocketProtocolError(
            f"Alpaca field {field} must be an exact number"
        ) from error
    if not result.is_finite() or result <= 0:
        raise AlpacaWebSocketProtocolError(
            f"Alpaca field {field} must be a positive finite number"
        )
    return result


async def _send_json(
    connection: AsyncWebSocketConnection, message: Mapping[str, object]
) -> None:
    await connection.send(json.dumps(message, separators=(",", ":")))


async def _receive_json(
    connection: AsyncWebSocketConnection, timeout_seconds: float
) -> tuple[Mapping[str, object], ...] | None:
    frame = await connection.receive(timeout_seconds)
    if frame is None:
        return None
    if isinstance(frame, bytes):
        try:
            frame = frame.decode("utf-8")
        except UnicodeDecodeError as error:
            raise AlpacaWebSocketProtocolError(
                "Alpaca WebSocket frame is not UTF-8"
            ) from error
    if not isinstance(frame, str):
        raise AlpacaWebSocketProtocolError("Alpaca WebSocket frame must be text")
    try:
        payload = json.loads(frame, parse_float=Decimal)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise AlpacaWebSocketProtocolError(
            "Alpaca WebSocket frame contains invalid JSON"
        ) from error
    if not isinstance(payload, list) or not payload:
        raise AlpacaWebSocketProtocolError(
            "Alpaca WebSocket frame must be a non-empty JSON array"
        )
    if not all(isinstance(message, dict) for message in payload):
        raise AlpacaWebSocketProtocolError(
            "Alpaca WebSocket frame must contain JSON objects"
        )
    return tuple(cast(Mapping[str, object], message) for message in payload)


def _market_messages(
    messages: Sequence[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    market: list[Mapping[str, object]] = []
    for message in messages:
        _raise_provider_error(message)
        message_type = message.get("T")
        if message_type in {"q", "t"}:
            market.append(message)
        elif message_type not in {"success", "subscription"}:
            raise AlpacaWebSocketProtocolError(
                f"unexpected Alpaca message type: {message_type!r}"
            )
    return tuple(market)


def _raise_provider_error(message: Mapping[str, object]) -> None:
    if message.get("T") != "error":
        return
    code = message.get("code")
    detail = message.get("msg")
    raise AlpacaWebSocketProtocolError(f"Alpaca stream error {code}: {detail}")


def _control_symbols(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AlpacaWebSocketProtocolError(
            f"Alpaca subscription field {field_name} must be a symbol list"
        )
    return _canonical_symbols(cast(list[str], value), field_name)


def _positive_duration(value: timedelta, field_name: str) -> None:
    if not isinstance(value, timedelta) or value <= timedelta(0):
        raise ValueError(f"{field_name} must be a positive timedelta")


def _nonnegative_duration(value: timedelta, field_name: str) -> None:
    if not isinstance(value, timedelta) or value < timedelta(0):
        raise ValueError(f"{field_name} must be a non-negative timedelta")


__all__ = [
    "AlpacaCryptoInbox",
    "AlpacaCryptoSubscription",
    "AlpacaCryptoWebSocketFeed",
    "AlpacaSipInbox",
    "AlpacaSipSubscription",
    "AlpacaSipWebSocketFeed",
    "AlpacaWebSocketProtocolError",
    "AsyncClock",
    "AsyncSleep",
    "AsyncWebSocketConnection",
    "AsyncWebSocketTransport",
    "MarketEvent",
    "StaleHandler",
]
