"""Live Alpaca options multi-leg execution venue for the fire-and-forget boundary.

This adapter extends the Alpaca broker boundary of
:mod:`harness.venues.alpaca` to option spreads. It mirrors the same split: a
synchronous, kernel-facing :class:`AlpacaOptionsExecutionInbox` normalizes
broker trade-update frames into the kernel
:class:`~harness.core.types.accounting.ExecutionReport` and
:class:`~harness.core.types.accounting.Fill` records used by replay, while
:class:`AlpacaOptionsBroker` owns the async REST command transport and the
strategy-tagged UUID7 client-order-id policy. Command receipts reuse
:class:`~harness.venues.alpaca.AlpacaCommandReceipt` and identity translation
mirrors the equity :class:`~harness.venues.alpaca.AlpacaOrderDirectory`
through :class:`AlpacaOptionsOrderDirectory`. Alpaca cannot modify multi-leg
orders, so the directory records no replacement chains and every replace
command is rejected explicitly.

Multi-leg wire mapping (Alpaca ``order_class="mleg"``):

- The kernel :class:`~harness.core.types.orders.MultiLegOrder` maps to one
  ``POST /v2/orders`` body with ``order_class="mleg"``, ``qty`` set to the
  common spread count (each leg quantity divided by its ratio), and one wire
  leg per kernel leg carrying ``symbol``, integral ``ratio_qty``, ``side``,
  and opening ``position_intent``. The group-level ``side`` is required by
  the wire schema but carries no direction; per-leg sides govern.
- Market groups omit ``limit_price``. Limit groups map the kernel's positive
  ``limit_price`` to a signed net price: a positive wire value for net-debit
  groups and a negative wire value for net-credit groups. Group direction is
  classified by the first leg's side (``SELL`` means net credit), matching
  the reference options-venue adapter convention.
- Every leg must be an OSI option contract on one underlying, ratios and the
  spread count must be positive integers, and IOC time in force is not
  supported. Alpaca cannot modify multi-leg orders, so replacement is always
  rejected explicitly; cancel and resubmit instead.

Leg report mapping:

- Group-level trade updates (``new``, ``canceled``, ``rejected``, ...) become
  one :class:`~harness.core.types.accounting.ExecutionReport` for the group
  whose quantity convention is the spread count. A group-level net-credit
  average price has no positive kernel representation, so a negative wire
  ``filled_avg_price`` maps to ``average_fill_price=None``; the credit sign
  is carried by the per-leg fill sides instead of being silently negated.
- Leg-level updates carry an ``execution`` whose ``symbol`` selects exactly
  one frame leg, with the execution ``qty`` interpreted as that leg's
  cumulative filled quantity. They become one leg-scoped
  :class:`~harness.core.types.accounting.ExecutionReport` (leg quantities,
  leg symbol in ``text``) plus one
  :class:`~harness.core.types.accounting.Fill` carrying the kernel group
  ``order_id``, the leg symbol, side, quantity, and price, so each
  virtual-book leg updates exactly once per leg fill.
- Frames for groups this adapter never submitted surface under their wire
  ``client_order_id`` so late reports after a restart stay visible.

Capability matrix and rejection taxonomy:

- The versioned in-code matrix lives in
  :class:`AlpacaOptionsCapabilityMatrix` (options) and
  :class:`AlpacaCapabilityMatrix` (the consolidation across the equities
  adapter, the crypto venue, and this adapter). The rendered document is
  ``src/harness/venues/CAPABILITY_MATRIX.md`` and must carry the same
  :data:`CAPABILITY_MATRIX_VERSION`.
- Unsupported features are rejected with
  :class:`UnsupportedAlpacaOptionsFeatureError` before broker submission:
  extended-hours flags, bracket/OTO/OCO/attachment order classes, unsupported
  order types and time-in-force values, leg-count and leg-shape violations,
  fractional ratios or spread counts, and multi-leg replacement. Unknown
  feature keys are rejected too; nothing degrades silently.
- Position snapshots for option contracts are served by the shared equity
  broker path (``GET /v2/positions`` rows already carry OSI symbols), so
  this adapter deliberately exposes no position command.

Determinism: the inbox performs no network, clock, sleep, or random access;
the async shell supplies decoded frames and observation timestamps. The only
deliberately non-deterministic identity remains the minted client order id,
injectable for tests exactly like the equity adapter.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import ClassVar, Final, cast
from urllib.parse import quote

import httpx

from harness.core.symbols import OsiSymbol, Symbol, is_osi_symbol
from harness.core.types.accounting import ExecutionReport, Fill
from harness.core.types.common import text, timestamp
from harness.core.types.orders import (
    ExecutionType,
    MultiLegOrder,
    OrderLeg,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    SingleLegOrder,
    TimeInForce,
)
from harness.venues.alpaca import (
    AlpacaBrokerHTTPError,
    AlpacaBrokerRest,
    AlpacaClientOrderIdSource,
    AlpacaCommandReceipt,
    AlpacaVenueError,
    AlpacaVenueProtocolError,
    AsyncBrokerResponse,
    AsyncBrokerTransport,
    BrokerClock,
    BrokerSleep,
    UnknownAlpacaOrderError,
)
from harness.venues.alpaca_crypto import is_crypto_pair
from harness.venues.scripted import VenueOperation

type QueryParams = Mapping[str, str]
type OptionsBrokerEvent = ExecutionReport | Fill

CAPABILITY_MATRIX_VERSION: Final = "2026-09.1"
CAPABILITY_MATRIX_PATH: Final = "src/harness/venues/CAPABILITY_MATRIX.md"

_ZERO: Final = Decimal("0")
_FILL_EXECUTION_TYPES: Final = (ExecutionType.PARTIAL_FILL, ExecutionType.FILL)
_ATTACHMENT_FEATURE_KEYS: Final = frozenset(
    {"take_profit", "stop_loss", "trailing_stop", "trail_price"}
)
_CRYPTO_ORDER_TYPES: Final = frozenset({OrderType.MARKET, OrderType.LIMIT})

_MLEG_EVENT_OUTCOMES: Final[Mapping[str, tuple[OrderStatus, ExecutionType]]] = (
    MappingProxyType(
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
)


class UnsupportedFeatureReason(StrEnum):
    """Explicit rejection taxonomy for unsupported Alpaca options features."""

    EXTENDED_HOURS = "extended_hours"
    ORDER_CLASS = "order_class"
    ORDER_TYPE = "order_type"
    TIME_IN_FORCE = "time_in_force"
    LEG_COUNT = "leg_count"
    LEG_RATIO = "leg_ratio"
    LEG_STRUCTURE = "leg_structure"
    QUANTITY = "quantity"
    ORDER_MODIFICATION = "order_modification"
    ASSET_CLASS = "asset_class"
    UNKNOWN_FEATURE = "unknown_feature"


@dataclass(frozen=True, slots=True)
class CapabilityRejection:
    """One explicit unsupported-capability decision."""

    reason: UnsupportedFeatureReason
    feature: str
    detail: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", UnsupportedFeatureReason(self.reason))
        object.__setattr__(self, "feature", text(self.feature, "feature"))
        object.__setattr__(self, "detail", text(self.detail, "detail"))


class AlpacaOptionsVenueError(AlpacaVenueError):
    """Base class for live Alpaca options command and protocol errors."""


class UnsupportedAlpacaOptionsFeatureError(AlpacaOptionsVenueError):
    """Raised when an order requests a capability the venue does not support.

    The error is constructed from one or more :class:`CapabilityRejection`
    values collected by the capability matrix so one submission surfaces the
    full set of unsupported combinations instead of failing one at a time.
    """

    def __init__(self, rejections: Sequence[CapabilityRejection]) -> None:
        normalized = tuple(rejections)
        if not normalized:
            raise ValueError("rejections must not be empty")
        self.rejections: tuple[CapabilityRejection, ...] = normalized
        self.reason = normalized[0].reason
        self.feature = normalized[0].feature
        summary = "; ".join(
            f"[{item.reason.value}] {item.feature}: {item.detail}"
            for item in normalized
        )
        super().__init__(f"unsupported Alpaca options capability: {summary}")


class VenueCapabilitySupport(StrEnum):
    """Support level of one capability row in the venue capability matrix."""

    SUPPORTED = "supported"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class VenueCapability:
    """One documented capability row for the consolidated venue matrix."""

    feature: str
    support: VenueCapabilitySupport
    detail: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "feature", text(self.feature, "feature"))
        object.__setattr__(self, "support", VenueCapabilitySupport(self.support))
        object.__setattr__(self, "detail", text(self.detail, "detail"))


@dataclass(frozen=True, slots=True)
class AlpacaOptionsCapabilityMatrix:
    """Versioned capability policy for Alpaca options multi-leg orders.

    ``check`` returns every unsupported combination for an order as explicit
    :class:`CapabilityRejection` values and ``require`` raises them as one
    :class:`UnsupportedAlpacaOptionsFeatureError`, always before any broker
    submission.
    """

    version: str
    min_legs: int = 2
    max_legs: int = 4
    order_types: frozenset[OrderType] = frozenset({OrderType.MARKET, OrderType.LIMIT})
    time_in_force: frozenset[TimeInForce] = frozenset(
        {TimeInForce.DAY, TimeInForce.GTC}
    )
    extended_hours: bool = False
    order_classes: frozenset[str] = frozenset({"mleg"})

    def __post_init__(self) -> None:
        object.__setattr__(self, "version", text(self.version, "version"))
        if isinstance(self.min_legs, bool) or not isinstance(self.min_legs, int):
            raise ValueError("min_legs must be an integer")
        if isinstance(self.max_legs, bool) or not isinstance(self.max_legs, int):
            raise ValueError("max_legs must be an integer")
        if self.min_legs < 2:
            raise ValueError("min_legs must be at least 2")
        if self.max_legs < self.min_legs:
            raise ValueError("max_legs must not be below min_legs")

    @classmethod
    def alpaca_options_default(cls) -> AlpacaOptionsCapabilityMatrix:
        """Return the shipped Alpaca options capability matrix."""
        return cls(version=CAPABILITY_MATRIX_VERSION)

    def check(
        self,
        order: OrderRequest,
        features: Mapping[str, object] | None = None,
    ) -> tuple[CapabilityRejection, ...]:
        """Return every unsupported combination for one options order."""
        if not isinstance(order, MultiLegOrder):
            raise TypeError("order must be a MultiLegOrder")
        rejections: list[CapabilityRejection] = []
        rejections.extend(self._feature_rejections(features))
        if order.order_type not in self.order_types:
            rejections.append(
                CapabilityRejection(
                    UnsupportedFeatureReason.ORDER_TYPE,
                    "order_type",
                    f"order type {order.order_type.value!r} is not supported; "
                    f"supported types are {_sorted_values(self.order_types)}",
                )
            )
        if order.time_in_force not in self.time_in_force:
            rejections.append(
                CapabilityRejection(
                    UnsupportedFeatureReason.TIME_IN_FORCE,
                    "time_in_force",
                    f"time in force {order.time_in_force.value!r} is not "
                    "supported for Alpaca options multi-leg orders; supported "
                    f"values are {_sorted_values(self.time_in_force)}",
                )
            )
        legs = tuple(order.legs)
        if len(legs) < self.min_legs or len(legs) > self.max_legs:
            rejections.append(
                CapabilityRejection(
                    UnsupportedFeatureReason.LEG_COUNT,
                    "legs",
                    f"multi-leg options orders require between {self.min_legs} "
                    f"and {self.max_legs} legs, got {len(legs)}",
                )
            )
        if self._leg_structure_rejections(legs, rejections):
            self._ratio_rejections(legs, rejections)
            self._quantity_rejections(legs, rejections)
        return tuple(rejections)

    def require(
        self,
        order: OrderRequest,
        features: Mapping[str, object] | None = None,
    ) -> None:
        """Raise when the order requests any unsupported capability."""
        rejections = self.check(order, features)
        if rejections:
            raise UnsupportedAlpacaOptionsFeatureError(rejections)

    def to_capabilities(self) -> tuple[VenueCapability, ...]:
        """Render the matrix as consolidated capability rows."""
        return (
            VenueCapability(
                "options_multi_leg_submit",
                VenueCapabilitySupport.SUPPORTED,
                f"mleg orders with {self.min_legs} to {self.max_legs} legs on "
                "one underlying",
            ),
            VenueCapability(
                "options_order_types",
                VenueCapabilitySupport.SUPPORTED,
                f"{_sorted_values(self.order_types)} orders; net-debit limits "
                "map to a positive wire limit_price and net-credit limits to "
                "a negative wire limit_price",
            ),
            VenueCapability(
                "options_time_in_force",
                VenueCapabilitySupport.SUPPORTED,
                _sorted_values(self.time_in_force),
            ),
            VenueCapability(
                "options_time_in_force_ioc",
                VenueCapabilitySupport.REJECTED,
                "ioc is rejected before broker submission",
            ),
            VenueCapability(
                "options_cancel",
                VenueCapabilitySupport.SUPPORTED,
                "group-level cancel through the bound parent client order id",
            ),
            VenueCapability(
                "options_replace",
                VenueCapabilitySupport.REJECTED,
                "Alpaca cannot modify multi-leg orders; cancel and resubmit instead",
            ),
            VenueCapability(
                "options_extended_hours",
                VenueCapabilitySupport.REJECTED
                if not self.extended_hours
                else VenueCapabilitySupport.SUPPORTED,
                "extended-hours flags are rejected before broker submission",
            ),
            VenueCapability(
                "options_brackets",
                VenueCapabilitySupport.REJECTED,
                "bracket, oto, oco, take-profit, and stop-loss attachments are "
                "rejected before broker submission",
            ),
            VenueCapability(
                "options_position_snapshot",
                VenueCapabilitySupport.SUPPORTED,
                "served by the shared AlpacaBroker equity positions path",
            ),
        )

    def _feature_rejections(
        self, features: Mapping[str, object] | None
    ) -> tuple[CapabilityRejection, ...]:
        if features is None:
            return ()
        if not isinstance(features, Mapping):
            raise TypeError("features must be a mapping")
        if not all(isinstance(key, str) for key in features):
            raise TypeError("feature keys must be strings")
        rejections: list[CapabilityRejection] = []
        for key in sorted(features):
            value = features[key]
            if key == "order_class":
                requested = value if isinstance(value, str) else repr(value)
                if requested in self.order_classes:
                    continue
                rejections.append(
                    CapabilityRejection(
                        UnsupportedFeatureReason.ORDER_CLASS,
                        key,
                        f"order class {requested!r} is not supported; Alpaca "
                        "options support the mleg class only",
                    )
                )
            elif key == "extended_hours":
                rejections.append(
                    CapabilityRejection(
                        UnsupportedFeatureReason.EXTENDED_HOURS,
                        key,
                        "extended-hours flags are not supported for Alpaca "
                        "options multi-leg orders",
                    )
                )
            elif key in _ATTACHMENT_FEATURE_KEYS:
                rejections.append(
                    CapabilityRejection(
                        UnsupportedFeatureReason.ORDER_CLASS,
                        key,
                        f"{key!r} attachments are not supported; Alpaca "
                        "options multi-leg orders cannot carry bracket legs",
                    )
                )
            else:
                rejections.append(
                    CapabilityRejection(
                        UnsupportedFeatureReason.UNKNOWN_FEATURE,
                        key,
                        f"feature {key!r} is not a known Alpaca options capability",
                    )
                )
        return tuple(rejections)

    @staticmethod
    def _leg_structure_rejections(
        legs: tuple[OrderLeg, ...], rejections: list[CapabilityRejection]
    ) -> bool:
        structure_ok = True
        roots: set[str] = set()
        symbols: set[str] = set()
        for leg in legs:
            symbol_text = str(leg.symbol)
            if not is_osi_symbol(symbol_text):
                rejections.append(
                    CapabilityRejection(
                        UnsupportedFeatureReason.LEG_STRUCTURE,
                        "legs",
                        f"leg {symbol_text!r} is not an OSI option contract",
                    )
                )
                structure_ok = False
                continue
            symbol = OsiSymbol(symbol_text)
            roots.add(symbol.root)
            if symbol_text in symbols:
                rejections.append(
                    CapabilityRejection(
                        UnsupportedFeatureReason.LEG_STRUCTURE,
                        "legs",
                        f"contract {symbol_text!r} appears more than once",
                    )
                )
                structure_ok = False
            symbols.add(symbol_text)
        if len(roots) > 1:
            rejections.append(
                CapabilityRejection(
                    UnsupportedFeatureReason.LEG_STRUCTURE,
                    "legs",
                    "multi-leg options orders must share one underlying, got "
                    f"{sorted(roots)}",
                )
            )
            structure_ok = False
        return structure_ok

    @staticmethod
    def _ratio_rejections(
        legs: tuple[OrderLeg, ...], rejections: list[CapabilityRejection]
    ) -> None:
        for leg in legs:
            if leg.ratio != leg.ratio.to_integral_value():
                rejections.append(
                    CapabilityRejection(
                        UnsupportedFeatureReason.LEG_RATIO,
                        "ratio",
                        f"leg ratio {leg.ratio} for {str(leg.symbol)!r} must "
                        "be a positive integer",
                    )
                )

    @staticmethod
    def _quantity_rejections(
        legs: tuple[OrderLeg, ...], rejections: list[CapabilityRejection]
    ) -> None:
        counts: set[Decimal] = set()
        for leg in legs:
            count = leg.quantity / leg.ratio
            if count != count.to_integral_value():
                rejections.append(
                    CapabilityRejection(
                        UnsupportedFeatureReason.QUANTITY,
                        "quantity",
                        f"leg quantity {leg.quantity} with ratio {leg.ratio} "
                        f"for {str(leg.symbol)!r} does not form an integral "
                        "spread count",
                    )
                )
            counts.add(count)
        if len(counts) > 1:
            rejections.append(
                CapabilityRejection(
                    UnsupportedFeatureReason.LEG_STRUCTURE,
                    "legs",
                    f"legs must share one common spread count, got {sorted(counts)}",
                )
            )


_EQUITY_CAPABILITIES: Final[tuple[VenueCapability, ...]] = (
    VenueCapability(
        "equity_single_leg_submit",
        VenueCapabilitySupport.SUPPORTED,
        "market, limit, stop, and stop-limit orders on one equity symbol",
    ),
    VenueCapability(
        "equity_time_in_force",
        VenueCapabilitySupport.SUPPORTED,
        "day, gtc, and ioc time in force",
    ),
    VenueCapability(
        "equity_cancel",
        VenueCapabilitySupport.SUPPORTED,
        "resolved through the order directory to the bound client order id",
    ),
    VenueCapability(
        "equity_replace",
        VenueCapabilitySupport.SUPPORTED,
        "quantity, time in force, and prices; symbol, side, and order type "
        "are fixed by Alpaca replace",
    ),
    VenueCapability(
        "equity_position_snapshot",
        VenueCapabilitySupport.SUPPORTED,
        "GET /v2/positions rows become kernel Position events",
    ),
    VenueCapability(
        "equity_multi_leg",
        VenueCapabilitySupport.REJECTED,
        "multi-leg orders submit through the options adapter only",
    ),
    VenueCapability(
        "equity_option_contracts",
        VenueCapabilitySupport.REJECTED,
        "option contracts submit through the options adapter as multi-leg spreads",
    ),
    VenueCapability(
        "equity_crypto_pairs",
        VenueCapabilitySupport.REJECTED,
        "crypto pairs route to the deterministic AlpacaCryptoVenue",
    ),
    VenueCapability(
        "equity_extended_hours",
        VenueCapabilitySupport.REJECTED,
        "extended-hours flags are rejected before broker submission",
    ),
    VenueCapability(
        "equity_brackets",
        VenueCapabilitySupport.REJECTED,
        "bracket, oto, oco, take-profit, and stop-loss attachments are "
        "rejected before broker submission",
    ),
)

_CRYPTO_CAPABILITIES: Final[tuple[VenueCapability, ...]] = (
    VenueCapability(
        "crypto_fractional_submit",
        VenueCapabilitySupport.SUPPORTED,
        "fractional market and limit orders on BASE/QUOTE pairs, continuous "
        "across every UTC day",
    ),
    VenueCapability(
        "crypto_replace",
        VenueCapabilitySupport.SUPPORTED,
        "atomic replace with symbol and side fixed",
    ),
    VenueCapability(
        "crypto_fee_reconciliation",
        VenueCapabilitySupport.SUPPORTED,
        "every fill reconciles the local maker-taker model against the venue "
        "fee schedule",
    ),
    VenueCapability(
        "crypto_extended_hours",
        VenueCapabilitySupport.REJECTED,
        "crypto trades continuously; extended-hours flags do not apply",
    ),
    VenueCapability(
        "crypto_position_snapshot",
        VenueCapabilitySupport.REJECTED,
        "positions belong to the accounting hub; the venue validates the "
        "protocol command only",
    ),
)


@dataclass(frozen=True, slots=True)
class AlpacaCapabilityMatrix:
    """Versioned capability matrix consolidating the Alpaca venue family.

    The family spans the live equity broker
    (:mod:`harness.venues.alpaca`), the deterministic crypto venue
    (:mod:`harness.venues.alpaca_crypto`), and the live options multi-leg
    broker (:mod:`harness.venues.alpaca_options`). ``check`` routes one
    order request to the capability policy of the adapter that would carry
    it and returns every unsupported combination explicitly.
    """

    version: str
    options: AlpacaOptionsCapabilityMatrix = field(
        default_factory=AlpacaOptionsCapabilityMatrix.alpaca_options_default
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "version", text(self.version, "version"))
        if not isinstance(self.options, AlpacaOptionsCapabilityMatrix):
            raise TypeError("options must be an AlpacaOptionsCapabilityMatrix")

    @classmethod
    def alpaca_default(cls) -> AlpacaCapabilityMatrix:
        """Return the shipped consolidated Alpaca capability matrix."""
        return cls(version=CAPABILITY_MATRIX_VERSION)

    def check(
        self,
        order: OrderRequest,
        features: Mapping[str, object] | None = None,
    ) -> tuple[CapabilityRejection, ...]:
        """Return every unsupported combination for one Alpaca order."""
        if isinstance(order, MultiLegOrder):
            return self.options.check(order, features)
        if not isinstance(order, SingleLegOrder):
            raise TypeError("order must be an OrderRequest")
        rejections = _single_leg_feature_rejections(features)
        symbol = str(order.symbol)
        if is_osi_symbol(symbol):
            rejections.append(
                CapabilityRejection(
                    UnsupportedFeatureReason.LEG_COUNT,
                    "legs",
                    "single-leg option orders are not supported; option "
                    "contracts submit as multi-leg spreads with at least two "
                    "legs",
                )
            )
        elif is_crypto_pair(symbol):
            rejections.append(
                CapabilityRejection(
                    UnsupportedFeatureReason.ASSET_CLASS,
                    "symbol",
                    f"crypto pair {symbol!r} routes to the deterministic "
                    "AlpacaCryptoVenue, not the Alpaca broker",
                )
            )
            if order.order_type not in _CRYPTO_ORDER_TYPES:
                rejections.append(
                    CapabilityRejection(
                        UnsupportedFeatureReason.ORDER_TYPE,
                        "order_type",
                        f"crypto orders support market and limit types only, "
                        f"not {order.order_type.value!r}",
                    )
                )
        return tuple(rejections)

    def require(
        self,
        order: OrderRequest,
        features: Mapping[str, object] | None = None,
    ) -> None:
        """Raise when the order requests any unsupported capability."""
        rejections = self.check(order, features)
        if rejections:
            raise UnsupportedAlpacaOptionsFeatureError(rejections)

    def to_capabilities(self) -> tuple[VenueCapability, ...]:
        """Return the consolidated capability rows in document order."""
        return (
            _EQUITY_CAPABILITIES + _CRYPTO_CAPABILITIES + self.options.to_capabilities()
        )

    def to_markdown(self) -> str:
        """Render the consolidated matrix as the capability document."""
        rows = "\n".join(
            f"| {capability.feature} | {capability.support.value} | "
            f"{capability.detail} |"
            for capability in self.to_capabilities()
        )
        return (
            "# Alpaca venue capability matrix\n"
            "\n"
            f"Version: {self.version}\n"
            "\n"
            "Consolidated capability policy for the Alpaca venue family: the\n"
            "live equity broker (`harness.venues.alpaca`), the deterministic\n"
            "crypto venue (`harness.venues.alpaca_crypto`), and the live\n"
            "options multi-leg broker (`harness.venues.alpaca_options`).\n"
            "Unsupported combinations are rejected explicitly with\n"
            "`UnsupportedAlpacaOptionsFeatureError` before broker submission;\n"
            "nothing degrades silently. This document is rendered from\n"
            "`AlpacaCapabilityMatrix.to_markdown()` and must carry the\n"
            f"`CAPABILITY_MATRIX_VERSION` {self.version}.\n"
            "\n"
            "| Feature | Support | Detail |\n"
            "| --- | --- | --- |\n"
            f"{rows}\n"
        )


def _single_leg_feature_rejections(
    features: Mapping[str, object] | None,
) -> list[CapabilityRejection]:
    if features is None:
        return []
    if not isinstance(features, Mapping):
        raise TypeError("features must be a mapping")
    if not all(isinstance(key, str) for key in features):
        raise TypeError("feature keys must be strings")
    rejections: list[CapabilityRejection] = []
    for key in sorted(features):
        value = features[key]
        if key == "order_class":
            requested = value if isinstance(value, str) else repr(value)
            if requested == "simple":
                continue
            rejections.append(
                CapabilityRejection(
                    UnsupportedFeatureReason.ORDER_CLASS,
                    key,
                    f"order class {requested!r} is not supported for "
                    "single-leg Alpaca broker orders",
                )
            )
        elif key == "extended_hours":
            rejections.append(
                CapabilityRejection(
                    UnsupportedFeatureReason.EXTENDED_HOURS,
                    key,
                    "extended-hours flags are not supported on Alpaca broker commands",
                )
            )
        elif key in _ATTACHMENT_FEATURE_KEYS:
            rejections.append(
                CapabilityRejection(
                    UnsupportedFeatureReason.ORDER_CLASS,
                    key,
                    f"{key!r} attachments are not supported; bracket exits "
                    "require cancel-and-resubmit orchestration above the "
                    "venue",
                )
            )
        else:
            rejections.append(
                CapabilityRejection(
                    UnsupportedFeatureReason.UNKNOWN_FEATURE,
                    key,
                    f"feature {key!r} is not a known Alpaca capability",
                )
            )
    return rejections


@dataclass(frozen=True, slots=True)
class AlpacaOptionsOrderBinding:
    """Command-side identity of one multi-leg group this adapter dispatched."""

    order_id: str
    client_order_id: str
    spread_count: Decimal
    legs: tuple[OrderLeg, ...]


class AlpacaOptionsOrderDirectory:
    """Maps kernel group order ids to the Alpaca identities this adapter minted.

    The directory records only command bookkeeping created by this adapter;
    it is plumbing for identity translation, not venue-state introspection.
    Alpaca cannot modify multi-leg orders, so unlike the equity directory
    there are no replacement chains to link.
    """

    __slots__ = ("_by_client", "_client_for_order")

    def __init__(self) -> None:
        self._by_client: dict[str, AlpacaOptionsOrderBinding] = {}
        self._client_for_order: dict[str, str] = {}

    def bind(self, binding: AlpacaOptionsOrderBinding) -> None:
        """Record an initial submit binding; identical rebinds are idempotent."""
        if not isinstance(binding, AlpacaOptionsOrderBinding):
            raise TypeError("binding must be an AlpacaOptionsOrderBinding")
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

    def binding_for_client(
        self, client_order_id: str
    ) -> AlpacaOptionsOrderBinding | None:
        """Return the binding for a client order id, if this adapter minted it."""
        return self._by_client.get(client_order_id)

    def client_order_id_for(self, order_id: str) -> str | None:
        """Return the client order id bound to a kernel group order id."""
        return self._client_for_order.get(order_id)


def _spread_count(legs: Sequence[OrderLeg]) -> Decimal:
    """Return the common integral spread count of one validated leg tuple."""
    counts: set[Decimal] = set()
    for leg in legs:
        if leg.ratio != leg.ratio.to_integral_value():
            raise AlpacaOptionsVenueError(
                f"leg ratio {leg.ratio} for {str(leg.symbol)!r} is not an integer"
            )
        count = leg.quantity / leg.ratio
        if count != count.to_integral_value():
            raise AlpacaOptionsVenueError(
                f"leg quantity {leg.quantity} with ratio {leg.ratio} for "
                f"{str(leg.symbol)!r} does not form an integral spread count"
            )
        counts.add(count)
    if len(counts) != 1:
        raise AlpacaOptionsVenueError(
            f"legs do not share one common spread count: {sorted(counts)}"
        )
    return next(iter(counts))


def _mleg_order_payload(
    order: MultiLegOrder, client_order_id: str
) -> dict[str, object]:
    legs = tuple(order.legs)
    spread_count = _spread_count(legs)
    first_side = legs[0].side
    payload: dict[str, object] = {
        "qty": str(spread_count),
        "side": "buy" if first_side is Side.BUY else "sell",
        "type": order.order_type.value,
        "time_in_force": order.time_in_force.value,
        "order_class": "mleg",
        "client_order_id": client_order_id,
        "legs": [
            {
                "symbol": str(leg.symbol),
                "ratio_qty": int(leg.ratio),
                "side": leg.side.value,
                "position_intent": (
                    "buy_to_open" if leg.side is Side.BUY else "sell_to_open"
                ),
            }
            for leg in legs
        ],
    }
    if order.limit_price is not None:
        wire_limit = order.limit_price if first_side is Side.BUY else -order.limit_price
        payload["limit_price"] = str(wire_limit)
    return payload


class AlpacaOptionsBrokerRest:
    """Async Alpaca options multi-leg REST command client, equity-patterned."""

    paper_broker_url: ClassVar[str] = AlpacaBrokerRest.paper_broker_url
    live_broker_url: ClassVar[str] = AlpacaBrokerRest.live_broker_url

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

    async def __aenter__(self) -> AlpacaOptionsBrokerRest:
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

    async def submit(self, order: MultiLegOrder, *, client_order_id: str) -> None:
        """Map a multi-leg submit command to ``POST /v2/orders``."""
        payload = _mleg_order_payload(order, client_order_id)
        await self._request("POST", "/v2/orders", json_body=payload)

    async def cancel(self, *, identity: str) -> None:
        """Map a group cancel command to ``DELETE /v2/orders/{identity}``."""
        path = f"/v2/orders/{quote(identity, safe='')}"
        await self._request("DELETE", path)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: QueryParams | None = None,
        json_body: Mapping[str, object] | None = None,
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


@dataclass(frozen=True, slots=True)
class _FrameLeg:
    """One leg identity available for leg-level report selection."""

    symbol: Symbol
    side: Side
    quantity: Decimal


class AlpacaOptionsExecutionInbox:
    """Synchronous, kernel-facing normalizer for Alpaca options trade updates.

    The inbox performs no network, clock, sleep, or random access: the async
    shell supplies the decoded frames and the observation timestamp, and the
    inbox atomically enqueues validated kernel events with monotonic
    sequences. Group-level frames become group
    :class:`~harness.core.types.accounting.ExecutionReport` records in spread
    counts; leg-level frames become leg-scoped reports plus per-leg
    :class:`~harness.core.types.accounting.Fill` records so each virtual-book
    leg updates exactly once per leg fill.
    """

    event_source: ClassVar[str] = "alpaca-options-broker"

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
        directory: AlpacaOptionsOrderDirectory | None = None,
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
        self._directory = (
            AlpacaOptionsOrderDirectory() if directory is None else directory
        )
        self._events: deque[OptionsBrokerEvent] = deque()

    @property
    def directory(self) -> AlpacaOptionsOrderDirectory:
        """Return the shared command-side identity directory."""
        return self._directory

    def __len__(self) -> int:
        return len(self._events)

    @property
    def next_sequence(self) -> int:
        """Return the sequence that the next accepted event will receive."""
        return self._next_sequence

    def pop(self) -> OptionsBrokerEvent | None:
        """Remove the oldest normalized event, if one is available."""
        return None if not self._events else self._events.popleft()

    def drain(self, limit: int | None = None) -> tuple[OptionsBrokerEvent, ...]:
        """Remove events in provider order without consulting a clock."""
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
        ):
            raise ValueError("limit must be a non-negative integer or None")
        drained: list[OptionsBrokerEvent] = []
        while self._events and (limit is None or len(drained) < limit):
            drained.append(self._events.popleft())
        return tuple(drained)

    def push(
        self, frame: Mapping[str, object], *, observed_at: datetime
    ) -> tuple[OptionsBrokerEvent, ...]:
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
        return tuple(events)

    def _normalize(
        self,
        data: Mapping[str, object],
        observed_at: datetime,
        first_sequence: int,
    ) -> tuple[OptionsBrokerEvent, ...]:
        event_name = _required_text(data, "event")
        outcome = _MLEG_EVENT_OUTCOMES.get(event_name)
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
        binding = self._directory.binding_for_client(client_order_id)
        order_id = binding.order_id if binding is not None else client_order_id
        legs = _frame_legs(order, binding)

        reports: list[OptionsBrokerEvent] = []
        if execution is None:
            if execution_type in _FILL_EXECUTION_TYPES:
                raise AlpacaVenueProtocolError(
                    f"Alpaca {event_name} update requires an execution object"
                )
            reports.append(
                self._group_report(
                    event_name=event_name,
                    status=status,
                    execution_type=execution_type,
                    order_id=order_id,
                    client_order_id=client_order_id,
                    order=order,
                    event_timestamp=event_timestamp,
                    sequence=first_sequence,
                )
            )
            return tuple(reports)

        execution_id = _required_text(execution, "id")
        leg = _select_frame_leg(execution, legs)
        fill_quantity = _required_positive_decimal(execution, "qty")
        if fill_quantity > leg.quantity:
            raise AlpacaVenueProtocolError(
                f"Alpaca execution qty exceeds the {leg.symbol} leg quantity"
            )
        fill_price = _required_positive_decimal(execution, "price")
        average_fill_price = _optional_positive_decimal(execution, "avg_price")
        reports.append(
            self._leg_report(
                event_name=event_name,
                status=status,
                execution_type=execution_type,
                order_id=order_id,
                client_order_id=client_order_id,
                leg=leg,
                filled_quantity=fill_quantity,
                average_fill_price=average_fill_price,
                last_fill_quantity=fill_quantity,
                last_fill_price=fill_price,
                event_timestamp=event_timestamp,
                sequence=first_sequence,
            )
        )
        if execution_type in _FILL_EXECUTION_TYPES:
            fill_sequence = first_sequence + 1
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
                    symbol=leg.symbol,
                    side=leg.side,
                    quantity=fill_quantity,
                    price=fill_price,
                )
            )
        return tuple(reports)

    def _group_report(
        self,
        *,
        event_name: str,
        status: OrderStatus,
        execution_type: ExecutionType,
        order_id: str,
        client_order_id: str,
        order: Mapping[str, object],
        event_timestamp: datetime,
        sequence: int,
    ) -> ExecutionReport:
        order_quantity = _required_positive_decimal(order, "qty")
        filled_quantity = _required_nonnegative_decimal(order, "filled_qty")
        if filled_quantity > order_quantity:
            raise AlpacaVenueProtocolError("Alpaca field filled_qty cannot exceed qty")
        average_fill_price = _optional_net_average(order, "filled_avg_price")
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
            last_fill_quantity=_ZERO,
            last_fill_price=None,
            text=f"alpaca:{event_name}",
        )

    def _leg_report(
        self,
        *,
        event_name: str,
        status: OrderStatus,
        execution_type: ExecutionType,
        order_id: str,
        client_order_id: str,
        leg: _FrameLeg,
        filled_quantity: Decimal,
        average_fill_price: Decimal | None,
        last_fill_quantity: Decimal,
        last_fill_price: Decimal | None,
        event_timestamp: datetime,
        sequence: int,
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
            order_quantity=leg.quantity,
            filled_quantity=filled_quantity,
            remaining_quantity=leg.quantity - filled_quantity,
            average_fill_price=average_fill_price,
            last_fill_quantity=last_fill_quantity,
            last_fill_price=last_fill_price,
            text=f"alpaca:{event_name}:{leg.symbol}",
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


class AlpacaOptionsBroker:
    """Async fire-and-forget Alpaca options multi-leg broker boundary.

    Commands map one-way to the Alpaca broker REST API and return command
    receipts. Outcomes arrive only as kernel events on the supplied
    :class:`AlpacaOptionsExecutionInbox`. The venue exposes no order book,
    working state, venue-status introspection, or position command; option
    positions are served by the shared equity broker positions path.
    """

    __slots__ = ("_client_ids", "_clock", "_inbox", "_matrix", "_rest")

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        inbox: AlpacaOptionsExecutionInbox,
        clock: BrokerClock,
        transport: AsyncBrokerTransport | None = None,
        base_url: str = AlpacaOptionsBrokerRest.paper_broker_url,
        strategy_id: str = "unattributed",
        client_ids: AlpacaClientOrderIdSource | None = None,
        matrix: AlpacaOptionsCapabilityMatrix | None = None,
        max_concurrency: int = 4,
        timeout_seconds: float = 30.0,
        max_attempts: int = 5,
        honor_retry_after: bool = True,
        backoff_seconds: float = 0.25,
        max_backoff_seconds: float = 30.0,
        sleep: BrokerSleep | None = None,
    ) -> None:
        if not isinstance(inbox, AlpacaOptionsExecutionInbox):
            raise TypeError("inbox must be an AlpacaOptionsExecutionInbox")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._inbox = inbox
        self._clock = clock
        self._matrix = (
            AlpacaOptionsCapabilityMatrix.alpaca_options_default()
            if matrix is None
            else matrix
        )
        self._client_ids = (
            AlpacaClientOrderIdSource(strategy_id=strategy_id)
            if client_ids is None
            else client_ids
        )
        self._rest = AlpacaOptionsBrokerRest(
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

    async def __aenter__(self) -> AlpacaOptionsBroker:
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

    async def submit(
        self,
        order: OrderRequest,
        *,
        features: Mapping[str, object] | None = None,
    ) -> AlpacaCommandReceipt:
        """Dispatch one multi-leg submit and return its receipt."""
        if not isinstance(order, MultiLegOrder):
            raise TypeError("order must be a MultiLegOrder")
        self._matrix.require(order, features)
        client_order_id = (
            order.client_order_id
            if order.client_order_id is not None
            else self._client_ids.next_client_order_id()
        )
        self._inbox.directory.bind(
            AlpacaOptionsOrderBinding(
                order_id=order.order_id,
                client_order_id=client_order_id,
                spread_count=_spread_count(order.legs),
                legs=tuple(order.legs),
            )
        )
        await self._rest.submit(order, client_order_id=client_order_id)
        return AlpacaCommandReceipt(
            VenueOperation.SUBMIT,
            order_id=order.order_id,
            client_order_id=client_order_id,
        )

    async def cancel(self, order_id: str) -> AlpacaCommandReceipt:
        """Dispatch one group cancel and return its receipt."""
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
        """Reject replacement; Alpaca cannot modify multi-leg orders."""
        raise UnsupportedAlpacaOptionsFeatureError(
            (
                CapabilityRejection(
                    UnsupportedFeatureReason.ORDER_MODIFICATION,
                    "replace",
                    "Alpaca cannot modify multi-leg orders; cancel and "
                    "resubmit instead",
                ),
            )
        )


AlpacaOptionsVenue = AlpacaOptionsBroker


def _frame_legs(
    order: Mapping[str, object], binding: AlpacaOptionsOrderBinding | None
) -> tuple[_FrameLeg, ...]:
    if binding is not None:
        legs = tuple(
            _FrameLeg(leg.symbol, leg.side, leg.quantity) for leg in binding.legs
        )
        raw_legs = order.get("legs")
        if raw_legs is not None:
            _validate_frame_leg_symbols(raw_legs, legs)
        return legs
    raw_legs = order.get("legs")
    if raw_legs is None:
        return ()
    return _parse_frame_legs(raw_legs)


def _parse_frame_legs(raw_legs: object) -> tuple[_FrameLeg, ...]:
    if isinstance(raw_legs, Mapping) or isinstance(raw_legs, str | bytes):
        raise AlpacaVenueProtocolError("Alpaca order legs must be an array")
    if not isinstance(raw_legs, Sequence):
        raise AlpacaVenueProtocolError("Alpaca order legs must be an array")
    legs: list[_FrameLeg] = []
    symbols: set[str] = set()
    for raw_leg in raw_legs:
        if not isinstance(raw_leg, Mapping):
            raise AlpacaVenueProtocolError("Alpaca order legs must be objects")
        symbol_text = _required_text(raw_leg, "symbol")
        if not is_osi_symbol(symbol_text):
            raise AlpacaVenueProtocolError(
                f"Alpaca leg symbol {symbol_text!r} is not an OSI contract"
            )
        if symbol_text in symbols:
            raise AlpacaVenueProtocolError(
                f"Alpaca order repeats leg symbol {symbol_text!r}"
            )
        symbols.add(symbol_text)
        side_value = _required_text(raw_leg, "side")
        try:
            side = Side(side_value)
        except ValueError as error:
            raise AlpacaVenueProtocolError(
                f"Alpaca leg side {side_value!r} is invalid"
            ) from error
        legs.append(
            _FrameLeg(
                symbol=Symbol(symbol_text),
                side=side,
                quantity=_required_positive_decimal(raw_leg, "qty"),
            )
        )
    return tuple(legs)


def _validate_frame_leg_symbols(raw_legs: object, legs: tuple[_FrameLeg, ...]) -> None:
    if isinstance(raw_legs, Mapping) or isinstance(raw_legs, str | bytes):
        raise AlpacaVenueProtocolError("Alpaca order legs must be an array")
    if not isinstance(raw_legs, Sequence):
        raise AlpacaVenueProtocolError("Alpaca order legs must be an array")
    if len(raw_legs) != len(legs):
        raise AlpacaVenueProtocolError(
            "Alpaca order legs do not match the bound group legs"
        )
    bound_symbols = {str(leg.symbol) for leg in legs}
    for raw_leg in raw_legs:
        if not isinstance(raw_leg, Mapping):
            raise AlpacaVenueProtocolError("Alpaca order legs must be objects")
        symbol_text = _required_text(raw_leg, "symbol")
        if symbol_text not in bound_symbols:
            raise AlpacaVenueProtocolError(
                f"Alpaca leg symbol {symbol_text!r} is not part of the bound group"
            )


def _select_frame_leg(
    execution: Mapping[str, object], legs: tuple[_FrameLeg, ...]
) -> _FrameLeg:
    symbol_text = _required_text(execution, "symbol")
    matches = [leg for leg in legs if str(leg.symbol) == symbol_text]
    if not matches:
        raise AlpacaVenueProtocolError(
            f"Alpaca execution references contract {symbol_text!r} which is "
            "not a leg of the order"
        )
    return matches[0]


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
    return _decimal_value(_required(mapping, field_name), field_name)


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


def _optional_net_average(
    mapping: Mapping[str, object], field_name: str
) -> Decimal | None:
    value = mapping.get(field_name)
    if value is None:
        return None
    result = _decimal_value(value, field_name)
    return result if result > 0 else None


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


def _sorted_values(values: frozenset[TimeInForce | OrderType]) -> str:
    return ", ".join(sorted(item.value for item in values))


__all__ = [
    "AlpacaCapabilityMatrix",
    "AlpacaOptionsBroker",
    "AlpacaOptionsBrokerRest",
    "AlpacaOptionsCapabilityMatrix",
    "AlpacaOptionsExecutionInbox",
    "AlpacaOptionsOrderBinding",
    "AlpacaOptionsOrderDirectory",
    "AlpacaOptionsVenue",
    "AlpacaOptionsVenueError",
    "CAPABILITY_MATRIX_PATH",
    "CAPABILITY_MATRIX_VERSION",
    "CapabilityRejection",
    "OptionsBrokerEvent",
    "QueryParams",
    "UnsupportedAlpacaOptionsFeatureError",
    "UnsupportedFeatureReason",
    "VenueCapability",
    "VenueCapabilitySupport",
]
