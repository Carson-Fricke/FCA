"""Transport-neutral Alpaca OPRA WebSocket feed for live option markets.

The WebSocket connection and async clock are ports supplied by the deployment
shell, exactly as for the SIP and crypto feeds. ``AlpacaOpraInbox`` is
deliberately synchronous: it performs no network, clock, sleep, or random
access while producing the same ``QuoteEvent`` and ``TradeEvent`` records used
by replay, keyed by canonical OSI option symbols.

Contract sets come from point-in-time ``OptionUniverseSnapshot`` selections
(:mod:`harness.data.options_universe`): the snapshot's look-ahead clipping
guarantees that a subscription or rotation never names a contract that has
expired, delisted, or not listed yet on the engine date. Unlike the crypto
feed, OPRA option markets follow the XNYS option sessions instead of trading
continuously on every UTC day, so the feed applies no day rotation; the
reconnect backoff and stale watchdog are inherited unchanged from the SIP
machinery and rely only on timestamps supplied by the async shell. The
``underlyings`` view of a subscription is the join key used to align option
events with the equity SIP streams of the same deployment.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import ClassVar, Self, cast

from harness.core.symbols import OsiSymbol, canonical_osi_symbol
from harness.data.alpaca_ws import (
    AlpacaSipInbox,
    AlpacaSipSubscription,
    AlpacaSipWebSocketFeed,
    AlpacaWebSocketProtocolError,
    AsyncClock,
    AsyncSleep,
    AsyncWebSocketConnection,
    AsyncWebSocketTransport,
    MarketEvent,
    StaleHandler,
    _send_json,
)
from harness.data.options_universe import OptionUniverseSnapshot


def _osi_symbols(symbols: Sequence[str], field_name: str) -> tuple[str, ...]:
    """Canonicalize OSI option channels, rejecting duplicates."""
    if isinstance(symbols, str):
        raise TypeError(f"{field_name} must be a sequence of OSI symbols")
    values = tuple(sorted({str(canonical_osi_symbol(symbol)) for symbol in symbols}))
    if len(values) != len(symbols):
        raise ValueError(f"{field_name} must not contain duplicate OSI symbols")
    return values


@dataclass(frozen=True, slots=True)
class AlpacaOpraSubscription(AlpacaSipSubscription):
    """Canonical OPRA channels for one point-in-time contract selection."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "trades", _osi_symbols(self.trades, "trades"))
        object.__setattr__(self, "quotes", _osi_symbols(self.quotes, "quotes"))
        if not self.trades and not self.quotes:
            raise ValueError(
                "at least one OPRA trade or quote subscription is required"
            )

    @property
    def underlyings(self) -> tuple[str, ...]:
        """Return distinct underlying roots: the join key to equity streams."""
        return tuple(sorted({OsiSymbol(symbol).root for symbol in self.symbols}))

    @classmethod
    def from_snapshot(
        cls,
        snapshot: OptionUniverseSnapshot,
        *,
        include_trades: bool = True,
        include_quotes: bool = True,
    ) -> AlpacaOpraSubscription:
        """Derive channels from a point-in-time snapshot's selected contracts.

        The snapshot construction is the look-ahead chokepoint: contracts
        expired before, delisted before, or not yet listed on the snapshot's
        ``as_of`` date are already clipped, so no future contract can reach a
        channel set through this factory.
        """
        if not isinstance(snapshot, OptionUniverseSnapshot):
            raise TypeError("snapshot must be an OptionUniverseSnapshot")
        symbols = snapshot.symbols
        return cls(
            trades=symbols if include_trades else (),
            quotes=symbols if include_quotes else (),
        )


@dataclass(frozen=True, slots=True)
class OpraRotationDelta:
    """Channel changes that move one live OPRA subscription to another."""

    unsubscribe_trades: tuple[str, ...] = ()
    unsubscribe_quotes: tuple[str, ...] = ()
    subscribe_trades: tuple[str, ...] = ()
    subscribe_quotes: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        """Return whether the rotation requires no provider messages."""
        return not (
            self.unsubscribe_trades
            or self.unsubscribe_quotes
            or self.subscribe_trades
            or self.subscribe_quotes
        )


def rotation_delta(
    current: AlpacaOpraSubscription, target: AlpacaOpraSubscription
) -> OpraRotationDelta:
    """Return the exact unsubscribe and subscribe sets between two selections."""
    if not isinstance(current, AlpacaOpraSubscription):
        raise TypeError("current must be an AlpacaOpraSubscription")
    if not isinstance(target, AlpacaOpraSubscription):
        raise TypeError("target must be an AlpacaOpraSubscription")
    return OpraRotationDelta(
        unsubscribe_trades=tuple(sorted(set(current.trades) - set(target.trades))),
        unsubscribe_quotes=tuple(sorted(set(current.quotes) - set(target.quotes))),
        subscribe_trades=tuple(sorted(set(target.trades) - set(current.trades))),
        subscribe_quotes=tuple(sorted(set(target.quotes) - set(current.quotes))),
    )


class AlpacaOpraInbox(AlpacaSipInbox):
    """Synchronous, deterministic OPRA normalizer reusing the SIP inbox.

    Event symbols must be canonical OSI option contracts. OPRA quotes carry
    the listing exchange in ``x`` instead of the SIP consolidated-tape field
    ``z``, so the normalizer remaps the venue before delegating to the shared
    quote and trade pipeline, keeping timestamp sanity, sequencing, and
    staleness tracking identical to the equity inbox. Event identities carry
    ``alpaca-opra`` provenance, so pair this inbox with
    ``AlpacaOpraWebSocketFeed``.
    """

    __slots__ = ()

    event_source: ClassVar[str] = "alpaca-opra"

    def _event_symbol(self, raw: str) -> str:
        try:
            osi = OsiSymbol(raw)
        except ValueError as error:
            raise AlpacaWebSocketProtocolError(
                f"Alpaca field S must be an OSI option symbol: {raw!r}"
            ) from error
        return str(osi)

    def _normalize(
        self, message: Mapping[str, object], observed_at: datetime, sequence: int
    ) -> MarketEvent:
        if message.get("T") == "q" and "z" not in message and "x" in message:
            remapped = dict(message)
            remapped["z"] = remapped.pop("x")
            message = remapped
        return super()._normalize(message, observed_at, sequence)


class _TrackingTransport:
    """Transport wrapper remembering the most recent live OPRA connection."""

    __slots__ = ("_connections", "_inner")

    def __init__(
        self,
        inner: AsyncWebSocketTransport,
        connections: list[AsyncWebSocketConnection],
    ) -> None:
        self._inner = inner
        self._connections = connections

    async def connect(self, url: str) -> AsyncWebSocketConnection:
        connection = await self._inner.connect(url)
        self._connections.append(connection)
        return connection


class AlpacaOpraWebSocketFeed(AlpacaSipWebSocketFeed):
    """Async Alpaca OPRA shell reusing the SIP auth, backoff, and watchdog.

    The feed subscribes quote and trade channels for the contracts selected by
    a point-in-time ``OptionUniverseSnapshot`` and rotates the live channel
    sets with :meth:`rotate` when the universe rolls, without reconnecting.
    Rotation never subscribes a contract outside the target snapshot, and the
    inherited watchdog follows the rotated channel sets immediately, so
    retired contracts leave staleness monitoring as soon as they leave the
    subscription.
    """

    __slots__ = ("_connections",)

    subscription_type = AlpacaOpraSubscription
    default_websocket_url = "wss://stream.data.alpaca.markets/v1beta1/opra"

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
        self._connections: list[AsyncWebSocketConnection] = []
        super().__init__(
            api_key=api_key,
            api_secret=api_secret,
            subscription=subscription,
            transport=_TrackingTransport(transport, self._connections),
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

    @classmethod
    def from_universe(
        cls,
        snapshot: OptionUniverseSnapshot,
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
        """Build a feed subscribed to the snapshot's point-in-time contracts."""
        return cls(
            api_key=api_key,
            api_secret=api_secret,
            subscription=AlpacaOpraSubscription.from_snapshot(snapshot),
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

    @classmethod
    def from_trader_config(cls, *args: object, **kwargs: object) -> Self:
        """Refuse trader-config construction; OPRA sets come from snapshots."""
        raise NotImplementedError(
            "Alpaca OPRA subscriptions rotate from point-in-time universe"
            " snapshots; build the feed with from_universe"
        )

    @property
    def live_connection(self) -> AsyncWebSocketConnection | None:
        """Return the most recent connection, if one has been established."""
        return self._connections[-1] if self._connections else None

    async def rotate(self, subscription: AlpacaOpraSubscription) -> OpraRotationDelta:
        """Swap the live channel sets to ``subscription`` without reconnecting.

        Sends one ``unsubscribe`` message for removed channels and one
        ``subscribe`` message for added channels; the provider's updated
        subscription echo is tolerated by the shared receive loop, and the
        inherited watchdog monitors the new channel sets immediately. Must be
        awaited while :meth:`run` streams on an established connection.
        """
        if not isinstance(subscription, AlpacaOpraSubscription):
            raise TypeError("subscription must be an AlpacaOpraSubscription")
        connection = self.live_connection
        if connection is None:
            raise AlpacaWebSocketProtocolError(
                "cannot rotate an OPRA subscription before connecting"
            )
        delta = rotation_delta(
            cast("AlpacaOpraSubscription", self._subscription), subscription
        )
        if not delta.is_empty:
            if delta.unsubscribe_trades or delta.unsubscribe_quotes:
                await _send_json(
                    connection,
                    {
                        "action": "unsubscribe",
                        "trades": list(delta.unsubscribe_trades),
                        "quotes": list(delta.unsubscribe_quotes),
                    },
                )
            if delta.subscribe_trades or delta.subscribe_quotes:
                await _send_json(
                    connection,
                    {
                        "action": "subscribe",
                        "trades": list(delta.subscribe_trades),
                        "quotes": list(delta.subscribe_quotes),
                    },
                )
        self._subscription = subscription
        return delta

    async def rotate_to_snapshot(
        self,
        snapshot: OptionUniverseSnapshot,
        *,
        include_trades: bool = True,
        include_quotes: bool = True,
    ) -> OpraRotationDelta:
        """Rotate the live channels to a new point-in-time snapshot."""
        return await self.rotate(
            AlpacaOpraSubscription.from_snapshot(
                snapshot,
                include_trades=include_trades,
                include_quotes=include_quotes,
            )
        )


__all__ = [
    "AlpacaOpraInbox",
    "AlpacaOpraSubscription",
    "AlpacaOpraWebSocketFeed",
    "OpraRotationDelta",
    "rotation_delta",
]
