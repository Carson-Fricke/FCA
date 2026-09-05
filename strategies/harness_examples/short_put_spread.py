"""Reference short put spread demonstrating the harness options workflow.

This module is the end-to-end options contract test for the public strategy
surface.  It documents and exercises every stage of the options workflow
with deterministic rules and no state outside the strategy contract:

1. Point-in-time contract selection.  Every quote callback reads the option
   universe through ``context.data.read(underlying)``, which returns the
   shell-supplied :class:`~harness.data.options_universe.OptionUniverseSnapshot`
   for the engine date.  The snapshot is the structural selection
   chokepoint: contracts that are not yet listed, already delisted, expired,
   or absent from the shell's daily front set are invisible, and DTE is
   evaluated against the engine date only, so no code path here can select
   a contract the shell had not yet selected.

2. Spread construction.  A short put spread sells the higher-strike put and
   buys the lower-strike put on one expiration.  Both legs are resolved from
   the point-in-time snapshot by strike, and the first quote that completes
   selection submits one atomic
   :class:`~harness.core.types.MultiLegOrder` through
   ``context.orders.submit`` with a net-credit limit (``min_credit``).  The
   entry fills only when the venue's net-credit rule is satisfied on a
   constituent quote update; a group still unfilled when DTE reaches
   ``cancel_dte`` is canceled through ``context.orders.cancel``.

3. Venue outcomes.  The multi-leg venue acknowledges through atomic fills:
   every leg of a group shares the group ``order_id`` and arrives as a
   :class:`~harness.core.types.Fill` after portfolio mutation.  Execution
   reports are a single-leg venue capability, so the strategy observes the
   group lifecycle through fills only and never reads venue state.

4. Deterministic exit rules.  The spread mark is the mid close cost
   ``mid(short put) - mid(long put)`` and is re-evaluated on long-leg
   quotes, by which point the short-leg quote of the same round has already
   arrived, so every mark pairs the freshest quote of each leg:

   - profit: redeem when the mark falls to ``profit_fraction`` of the entry
     credit, submitted as a net-debit limit;
   - stop: redeem when the mark rises to ``stop_multiple`` of the entry
     credit, submitted as a net-debit limit;
   - managed exit: redeem when DTE reaches ``exit_dte``, submitted as a
     net-debit limit;
   - expiry: on the expiration date, redeem at market while residual value
     remains; when the mark is zero the strategy stops trading and emits
     its expiry disposition so the shell can settle the contracts (a
     worthless expiry);
   - redeem net-debit limits admit up to ``(1 + stop_multiple)`` times the
     entry credit, so a triggered redeem stays marketable through its own
     trigger;
   - the strategy is flat, canceled, or awaiting settlement after at most
     one entry group and one redeem group.

Quote and fill prices are per-contract notional values (the per-share
quote times the contract multiplier), matching
:mod:`harness.portfolio.options_lifecycle`.  Every value is exact
:class:`~decimal.Decimal` arithmetic, time is observed only through the
context clock and the point-in-time data capability, and the strategy emits
one entry emission, one exit or expiry emission, and one stop summary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from harness.core.symbols import Symbol
from harness.core.types import (
    ExecutionReport,
    Fill,
    MultiLegOrder,
    OrderLeg,
    OrderType,
    QuoteEvent,
    Side,
    SingleLegOrder,
    TimeInForce,
)
from harness.data.options_universe import OptionUniverseSnapshot
from harness.strategy.context import StrategyContext
from harness.strategy.contracts import Strategy

if TYPE_CHECKING:
    from lake.catalog.models import ContractRecord

_ZERO: Final = Decimal("0")
_ONE: Final = Decimal("1")
_TWO: Final = Decimal("2")

_SHORT_LEG: Final = "short_put"
_LONG_LEG: Final = "long_put"
_LEGS: Final = frozenset({_SHORT_LEG, _LONG_LEG})

_ROLE_ENTRY: Final = "entry"
_ROLE_REDEEM: Final = "redeem"


class _Phase(StrEnum):
    """One deterministic episode step for the short-put-spread state machine."""

    SCANNING = "scanning"
    ENTRY_WORKING = "entry_working"
    OPEN = "open"
    REDEEM_WORKING = "redeem_working"
    FLAT = "flat"
    CANCELED_NO_FILL = "canceled_no_fill"
    AWAITING_SETTLEMENT = "awaiting_settlement"


_TERMINAL_PHASES: Final[frozenset[_Phase]] = frozenset(
    {
        _Phase.FLAT,
        _Phase.CANCELED_NO_FILL,
        _Phase.AWAITING_SETTLEMENT,
    }
)


@dataclass(slots=True)
class GroupRecord:
    """One strategy group and the leg fills the strategy observed for it."""

    order_id: str
    role: str
    status: str = "working"
    leg_prices: dict[str, Decimal] = field(default_factory=dict)


class ShortPutSpreadStrategy(Strategy):
    """Sell a put spread through the point-in-time universe and manage it out.

    The state machine is ``scanning -> entry_working -> open`` and then one
    deterministic exit: ``flat`` (redeemed), ``canceled_no_fill`` (the entry
    was canceled unfilled), or ``awaiting_settlement`` (zero-value contracts
    left for shell settlement).  See the module docstring for the documented
    selection, submission, and exit rules.
    """

    def __init__(
        self,
        *,
        underlying: str = "SPY",
        short_strike: str | Decimal = "500",
        long_strike: str | Decimal = "495",
        expiration: str = "2024-01-19",
        quantity: str | int | Decimal = "1",
        min_credit: str | Decimal = "45.00",
        profit_fraction: str | Decimal = "0.50",
        stop_multiple: str | Decimal = "2.0",
        entry_max_dte: int | None = 3,
        exit_dte: int = 0,
        cancel_dte: int = 0,
        order_prefix: str = "sps",
    ) -> None:
        self.underlying = str(underlying)
        self.short_strike = Decimal(str(short_strike))
        self.long_strike = Decimal(str(long_strike))
        if self.short_strike <= _ZERO or self.long_strike <= _ZERO:
            raise ValueError("strikes must be positive")
        if self.short_strike <= self.long_strike:
            raise ValueError("short_strike must exceed long_strike")
        self.expiration = expiration
        self._expiry = date.fromisoformat(self.expiration)
        self.quantity = Decimal(str(quantity))
        if self.quantity <= _ZERO:
            raise ValueError("quantity must be positive")
        self.min_credit = Decimal(str(min_credit))
        if self.min_credit <= _ZERO:
            raise ValueError("min_credit must be positive")
        self.profit_fraction = Decimal(str(profit_fraction))
        if not _ZERO < self.profit_fraction <= _ONE:
            raise ValueError("profit_fraction must be inside (0, 1]")
        self.stop_multiple = Decimal(str(stop_multiple))
        if self.stop_multiple <= _ONE:
            raise ValueError("stop_multiple must exceed one")
        if entry_max_dte is not None and (
            isinstance(entry_max_dte, bool) or entry_max_dte < 0
        ):
            raise ValueError("entry_max_dte must be a non-negative integer or None")
        self.entry_max_dte = entry_max_dte
        if isinstance(exit_dte, bool) or exit_dte < 0:
            raise ValueError("exit_dte must be a non-negative integer")
        self.exit_dte = exit_dte
        if isinstance(cancel_dte, bool) or cancel_dte < 0:
            raise ValueError("cancel_dte must be a non-negative integer")
        self.cancel_dte = cancel_dte
        self.order_prefix = str(order_prefix)
        if not self.order_prefix.strip():
            raise ValueError("order_prefix must be a non-empty string")

        self.context: StrategyContext | None = None
        self.callbacks: list[str] = []
        self.records: dict[str, GroupRecord] = {}
        self.phase: str = _Phase.SCANNING.value
        self.entry_id: str | None = None
        self.exit_id: str | None = None
        self.entry_credit: Decimal | None = None
        self.exit_debit: Decimal | None = None
        self.realized_pnl: Decimal | None = None
        self._phase = _Phase.SCANNING
        self._ordinal = 0
        self._short_symbol: Symbol | None = None
        self._long_symbol: Symbol | None = None
        self._multiplier: Decimal | None = None
        self._last_quotes: dict[Symbol, QuoteEvent] = {}

    def on_start(self, context: StrategyContext) -> None:
        self.callbacks.append("on_start")
        self.context = context

    def on_quote(self, event: QuoteEvent) -> None:
        self.callbacks.append("on_quote")
        context = self.context
        if context is None:
            return
        self._last_quotes[event.symbol] = event
        if self._phase in _TERMINAL_PHASES:
            return
        snapshot = self._universe_snapshot(context)
        if snapshot is None:
            return
        dte = snapshot.dte(self._expiry)
        if self._phase is _Phase.SCANNING:
            self._try_entry(context, snapshot, dte, event)
            return
        if self._phase is _Phase.ENTRY_WORKING:
            self._check_entry_cancel(context, dte)
            return
        if self._long_symbol is not None and event.symbol == self._long_symbol:
            self._manage_open(context, dte, event)

    def on_order(self, event: SingleLegOrder | MultiLegOrder) -> None:
        self.callbacks.append("on_order")

    def on_execution_report(self, event: ExecutionReport) -> None:
        self.callbacks.append("on_execution_report")

    def on_fill(self, event: Fill) -> None:
        self.callbacks.append("on_fill")
        context = self.context
        if context is None or not context.orders.owns(event.order_id):
            return
        record = self.records.get(event.order_id)
        if record is None:
            return
        leg_id = self._leg_id(event.symbol)
        if leg_id is None:
            return
        record.leg_prices[leg_id] = event.price
        if set(record.leg_prices) != _LEGS:
            return
        record.status = "filled"
        if record.role == _ROLE_ENTRY:
            self._complete_entry(record)
        else:
            self._complete_redeem(record)

    def on_stop(self, context: StrategyContext) -> None:
        self.callbacks.append("on_stop")
        filled_groups = sum(
            1 for record in self.records.values() if record.status == "filled"
        )
        context.emit(
            "short_put_spread_summary",
            phase=self._phase.value,
            orders_submitted=self._ordinal,
            filled_groups=filled_groups,
            entry_credit=self.entry_credit,
            realized_pnl=self.realized_pnl,
        )

    # -- selection and entry ------------------------------------------------

    def _universe_snapshot(
        self, context: StrategyContext
    ) -> OptionUniverseSnapshot | None:
        """Return the point-in-time universe snapshot for the underlying."""
        data = context.data
        if data is None:
            return None
        snapshot = data.read(self.underlying)
        return snapshot if isinstance(snapshot, OptionUniverseSnapshot) else None

    def _resolve_leg(
        self, snapshot: OptionUniverseSnapshot, strike: Decimal
    ) -> ContractRecord | None:
        """Return the front-set put at ``strike`` on the configured expiry."""
        for record in snapshot.at_expiry(self._expiry):
            if record.option_type == "put" and record.strike_price == strike:
                return record
        return None

    def _try_entry(
        self,
        context: StrategyContext,
        snapshot: OptionUniverseSnapshot,
        dte: int,
        event: QuoteEvent,
    ) -> None:
        """Select both legs from the snapshot and submit the entry group."""
        if self.entry_max_dte is not None and dte > self.entry_max_dte:
            return
        short_record = self._resolve_leg(snapshot, self.short_strike)
        long_record = self._resolve_leg(snapshot, self.long_strike)
        if short_record is None or long_record is None:
            return
        if short_record.symbol == long_record.symbol:
            return
        self._short_symbol = Symbol(short_record.symbol)
        self._long_symbol = Symbol(long_record.symbol)
        self._multiplier = Decimal(short_record.multiplier)
        order = self._build_group(
            context,
            role=_ROLE_ENTRY,
            short_side=Side.SELL,
            long_side=Side.BUY,
            order_type=OrderType.LIMIT,
            limit_price=self.min_credit,
            sequence=event.sequence + 1,
        )
        self.entry_id = order.order_id
        self._phase = _Phase.ENTRY_WORKING
        self.phase = self._phase.value
        context.orders.submit(order)

    def _check_entry_cancel(self, context: StrategyContext, dte: int) -> None:
        """Cancel an entry that is still unfilled inside the cancel window."""
        entry_id = self.entry_id
        if entry_id is None or dte > self.cancel_dte:
            return
        record = self.records.get(entry_id)
        if record is not None:
            record.status = "cancel_requested"
        context.orders.cancel(entry_id)
        self._phase = _Phase.CANCELED_NO_FILL
        self.phase = self._phase.value

    # -- position management -------------------------------------------------

    def _manage_open(
        self,
        context: StrategyContext,
        dte: int,
        event: QuoteEvent,
    ) -> None:
        """Apply the deterministic exit rules on one long-leg quote."""
        if self._short_symbol is None or self._long_symbol is None:
            return
        short_quote = self._last_quotes.get(self._short_symbol)
        long_quote = self._last_quotes.get(self._long_symbol)
        if short_quote is None or long_quote is None:
            return
        cost = _mid(short_quote) - _mid(long_quote)
        if dte <= 0:
            self._manage_expiry(context, event, cost)
            return
        if self._phase is not _Phase.OPEN:
            return
        credit = self.entry_credit
        if credit is None:
            return
        redeem_limit = (self.stop_multiple + _ONE) * credit
        triggered = (
            cost <= self.profit_fraction * credit
            or cost >= self.stop_multiple * credit
            or dte <= self.exit_dte
        )
        if triggered:
            self._submit_redeem(
                context,
                event=event,
                order_type=OrderType.LIMIT,
                limit_price=redeem_limit,
            )

    def _manage_expiry(
        self, context: StrategyContext, event: QuoteEvent, cost: Decimal
    ) -> None:
        """Apply the expiration-date liquidation and settlement rules."""
        if self._phase is _Phase.REDEEM_WORKING and cost <= _ZERO:
            exit_id = self.exit_id
            if exit_id is not None:
                record = self.records.get(exit_id)
                if record is not None:
                    record.status = "cancel_requested"
                context.orders.cancel(exit_id)
            self._await_settlement()
            return
        if self._phase is not _Phase.OPEN:
            return
        if cost <= _ZERO:
            self._await_settlement()
            return
        self._submit_redeem(
            context, event=event, order_type=OrderType.MARKET, limit_price=None
        )

    def _await_settlement(self) -> None:
        """Stop trading and leave the zero-value contracts to the shell."""
        self._phase = _Phase.AWAITING_SETTLEMENT
        self.phase = self._phase.value
        context = self.context
        if context is not None:
            context.emit(
                "short_put_spread_expiry",
                disposition="awaiting_settlement",
            )

    def _submit_redeem(
        self,
        context: StrategyContext,
        *,
        event: QuoteEvent | None,
        order_type: OrderType,
        limit_price: Decimal | None = None,
    ) -> None:
        """Submit the atomic redeem group that closes the spread."""
        if self._short_symbol is None or self._long_symbol is None:
            return
        sequence = 0 if event is None else event.sequence + 1
        order = self._build_group(
            context,
            role=_ROLE_REDEEM,
            short_side=Side.BUY,
            long_side=Side.SELL,
            order_type=order_type,
            limit_price=limit_price,
            sequence=sequence,
        )
        self.exit_id = order.order_id
        self._phase = _Phase.REDEEM_WORKING
        self.phase = self._phase.value
        context.orders.submit(order)

    # -- fill completion ------------------------------------------------------

    def _complete_entry(self, record: GroupRecord) -> None:
        context = self.context
        short_symbol = self._short_symbol
        long_symbol = self._long_symbol
        if context is None or short_symbol is None or long_symbol is None:
            return
        credit = (
            record.leg_prices[_SHORT_LEG] - record.leg_prices[_LONG_LEG]
        ) * self.quantity
        self.entry_credit = credit
        multiplier = self._multiplier if self._multiplier is not None else _ONE
        collateral = (
            self.short_strike - self.long_strike
        ) * multiplier * self.quantity - credit
        context.emit(
            "short_put_spread_entry",
            short_symbol=str(short_symbol),
            long_symbol=str(long_symbol),
            contracts=self.quantity,
            entry_credit=credit,
            collateral_bound=collateral,
        )
        self._phase = _Phase.OPEN
        self.phase = self._phase.value

    def _complete_redeem(self, record: GroupRecord) -> None:
        context = self.context
        credit = self.entry_credit
        if context is None or credit is None:
            return
        debit = (
            record.leg_prices[_SHORT_LEG] - record.leg_prices[_LONG_LEG]
        ) * self.quantity
        self.exit_debit = debit
        self.realized_pnl = credit - debit
        context.emit(
            "short_put_spread_exit",
            exit_debit=debit,
            realized_pnl=self.realized_pnl,
        )
        self._phase = _Phase.FLAT
        self.phase = self._phase.value

    # -- helpers --------------------------------------------------------------

    def _build_group(
        self,
        context: StrategyContext,
        *,
        role: str,
        short_side: Side,
        long_side: Side,
        order_type: OrderType,
        limit_price: Decimal | None,
        sequence: int,
    ) -> MultiLegOrder:
        """Build one atomic two-leg group and record it in the ledger."""
        if self._short_symbol is None or self._long_symbol is None:
            raise RuntimeError("group construction requires resolved legs")
        self._ordinal += 1
        order_id = f"{self.order_prefix}-{self._ordinal:04d}"
        order = MultiLegOrder(
            event_id=order_id,
            timestamp=context.clock.now(),
            sequence=sequence,
            order_id=order_id,
            legs=(
                OrderLeg(
                    self._short_symbol, short_side, self.quantity, leg_id=_SHORT_LEG
                ),
                OrderLeg(self._long_symbol, long_side, self.quantity, leg_id=_LONG_LEG),
            ),
            order_type=order_type,
            time_in_force=TimeInForce.DAY,
            limit_price=limit_price,
        )
        self.records[order_id] = GroupRecord(order_id=order_id, role=role)
        return order

    def _leg_id(self, symbol: Symbol) -> str | None:
        """Return the strategy leg identifier for one filled symbol."""
        if symbol == self._short_symbol:
            return _SHORT_LEG
        if symbol == self._long_symbol:
            return _LONG_LEG
        return None


def _mid(quote: QuoteEvent) -> Decimal:
    """Return the exact quote midpoint."""
    return (quote.bid_price + quote.ask_price) / _TWO


__all__ = ["GroupRecord", "ShortPutSpreadStrategy"]
