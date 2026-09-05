"""Exact-decimal option collateral rules as pre-trade risk rules.

Collateral is derived from the candidate order plus the point-in-time
portfolio snapshot only: contract terms come from canonical OSI symbols,
coverage comes from projected long option and underlying-share positions,
and no mark price, clock, catalog, or randomness is ever consulted.  Each
``(root, expiration)`` contract group is evaluated exactly at its strike
breakpoints, so a vertical spread reserves exactly its strike width, an
iron condor reserves exactly the wider of its put and call widths (both
shorts can never finish in the money together), a cash-secured short put
reserves its strike notional, and a share-covered call reserves nothing.

Nakedness is an explicit decision, never a silent pass:

- Any group whose projected worst case is unlimited (uncovered short calls,
  ratio writes, partially share-covered calls) is rejected outright; an
  unbounded worst case is not collateralizable and has no bypass.
- Short puts uncovered by long puts are naked cash-secured exposure and are
  rejected by :class:`NoNakedShortOptions` unless ``allow_naked_short_put``
  is explicitly configured; the exact strike-notional requirement then
  remains enforceable through :class:`OptionCollateralLimit`.
- Orders that only reduce existing naked exposure pass; the rule compares
  per-group naked quantities before and after the projected order.
- Underlying shares cover short calls only, are consumed in expiration
  order, and a partial share cover leaves the remainder exactly as
  unlimited as it truly is.

There is deliberately no reduction path: scaling a multi-leg order changes
per-contract coverage nonlinearly, so these rules allow or reject and leave
sizing to the strategy.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from harness.core.symbols import OsiSymbol, Symbol, canonical_symbol
from harness.core.types.common import DecimalInput, exact_decimal
from harness.core.types.orders import OrderRequest, Side, SingleLegOrder
from harness.risk.limits import ConfiguredRiskRule
from harness.risk.models import RiskContext, RuleDecision

_ZERO: Final = Decimal("0")
_DEFAULT_MULTIPLIER: Final = Decimal("100")


@dataclass(frozen=True, slots=True)
class ContractCollateral:
    """Per-contract projected collateral classification.

    ``projected_contracts`` is the signed position after the candidate
    order; ``covered_by_shares`` is the contract quantity removed from the
    short side by the root's underlying shares; ``covered_by_long_option``
    reports whether same-root, same-expiry, same-right long contracts make
    the group a spread; and ``naked_contracts`` is the short quantity left
    with no covering instrument.
    """

    symbol: str
    root: str
    expiration: date
    right: str
    projected_contracts: Decimal
    covered_by_shares: Decimal
    covered_by_long_option: bool
    naked_contracts: Decimal


@dataclass(frozen=True, slots=True)
class OptionGroupCollateral:
    """Exact collateral requirement for one root and expiration group.

    ``requirement`` is the worst-case additional cash implied by the group's
    projected option positions and any covering shares; it is ``None`` when
    the group's worst case is unlimited.  ``naked_put_contracts`` and
    ``naked_call_contracts`` count short contracts with no covering
    instrument of the same right.
    """

    root: str
    expiration: date
    contracts: tuple[ContractCollateral, ...]
    requirement: Decimal | None
    unlimited: bool
    naked_put_contracts: Decimal
    naked_call_contracts: Decimal


@dataclass(frozen=True, slots=True)
class OptionCollateralReport:
    """Complete per-contract collateral classification for one evaluation."""

    multiplier: Decimal
    groups: tuple[OptionGroupCollateral, ...]

    @property
    def total_collateral(self) -> Decimal | None:
        """Return the summed group requirement, or ``None`` if unlimited."""
        total = _ZERO
        for group in self.groups:
            if group.requirement is None:
                return None
            total += group.requirement
        return total

    @property
    def unlimited(self) -> bool:
        """Whether any group has an unlimited worst case."""
        return any(group.unlimited for group in self.groups)

    @property
    def naked_put_contracts(self) -> Decimal:
        """Return total short puts with no covering instrument."""
        return sum((group.naked_put_contracts for group in self.groups), start=_ZERO)

    @property
    def naked_call_contracts(self) -> Decimal:
        """Return total short calls with no covering instrument."""
        return sum((group.naked_call_contracts for group in self.groups), start=_ZERO)

    @property
    def naked(self) -> bool:
        """Whether any naked short option exposure remains."""
        return self.naked_put_contracts > _ZERO or self.naked_call_contracts > _ZERO


@dataclass(frozen=True, slots=True)
class _OptionPosition:
    """One projected OSI contract position inside a group."""

    contract: OsiSymbol
    contracts: Decimal


def _as_osi(symbol: Symbol) -> OsiSymbol | None:
    try:
        return OsiSymbol(str(symbol))
    except ValueError:
        return None


def _order_legs(
    order: OrderRequest,
) -> tuple[tuple[Symbol, Side, Decimal], ...]:
    if isinstance(order, SingleLegOrder):
        return ((order.symbol, order.side, order.quantity),)
    return tuple((leg.symbol, leg.side, leg.quantity) for leg in order.legs)


def _projected_quantities(
    order: OrderRequest, context: RiskContext
) -> tuple[dict[Symbol, Decimal], dict[Symbol, Decimal]]:
    """Return current and projected signed quantities by symbol."""
    current: dict[Symbol, Decimal] = {}
    for position in context.portfolio.positions:
        current[position.symbol] = position.quantity
    projected = dict(current)
    for symbol, side, quantity in _order_legs(order):
        signed = quantity if side is Side.BUY else -quantity
        key = canonical_symbol(symbol)
        projected[key] = projected.get(key, _ZERO) + signed
    return current, projected


def _scenario_requirement(
    calls: Sequence[tuple[Decimal, Decimal]],
    puts: Sequence[tuple[Decimal, Decimal]],
) -> Decimal | None:
    """Return the exact worst-case additional cash, or ``None`` if unlimited.

    Values are in share units (contracts times multiplier) times dollars per
    share.  The payoff is piecewise linear with breakpoints at the group's
    strikes, so the minimum over all terminal prices is attained at zero or
    at a strike; the slope beyond the highest strike detects unlimited
    short-call exposure.
    """
    slope = sum((signed for _, signed in calls), start=_ZERO)
    if slope < _ZERO:
        return None
    strikes = {strike for strike, _ in calls}
    strikes.update(strike for strike, _ in puts)
    worst = _ZERO
    at_zero = _ZERO
    for strike, signed in puts:
        at_zero += signed * strike
    worst = min(worst, at_zero)
    for breakpoint in sorted(strikes):
        value = _ZERO
        for strike, signed in calls:
            if breakpoint > strike:
                value += signed * (breakpoint - strike)
        for strike, signed in puts:
            if breakpoint < strike:
                value += signed * (strike - breakpoint)
        worst = min(worst, value)
    return -worst if worst < _ZERO else _ZERO


def _build_groups(
    quantities: Mapping[Symbol, Decimal],
    multiplier: Decimal,
) -> tuple[OptionGroupCollateral, ...]:
    """Build deterministic collateral groups from signed symbol quantities.

    Underlying shares cover short calls only and are consumed in expiration
    order; inside one group, shorter-strike shorts consume the pool first.
    """
    shares: dict[str, Decimal] = {}
    calls: dict[tuple[str, date], list[_OptionPosition]] = {}
    puts: dict[tuple[str, date], list[_OptionPosition]] = {}
    for symbol, quantity in sorted(quantities.items(), key=lambda item: str(item[0])):
        if quantity == _ZERO:
            continue
        contract = _as_osi(symbol)
        if contract is None:
            shares[str(symbol)] = quantity
            continue
        target = calls if contract.right == "C" else puts
        target.setdefault((contract.root, contract.expiration_date), []).append(
            _OptionPosition(contract, quantity)
        )
    pools = {
        root: max(quantity, _ZERO) / multiplier for root, quantity in shares.items()
    }
    roots = {root for root, _ in calls} | {root for root, _ in puts} | set(shares)
    groups: list[OptionGroupCollateral] = []
    for root in sorted(roots):
        pool = pools.get(root, _ZERO)
        expiries = sorted(
            {expiry for group_root, expiry in calls if group_root == root}
            | {expiry for group_root, expiry in puts if group_root == root}
        )
        for expiry in expiries:
            call_positions = calls.get((root, expiry), ())
            put_positions = puts.get((root, expiry), ())
            covered_by_shares: dict[str, Decimal] = {}
            for position in sorted(
                (item for item in call_positions if item.contracts < _ZERO),
                key=lambda item: (item.contract.strike, str(item.contract)),
            ):
                short = -position.contracts
                covered = pool if pool < short else short
                if covered > _ZERO:
                    pool -= covered
                    covered_by_shares[str(position.contract)] = covered
            has_long_calls = any(item.contracts > _ZERO for item in call_positions)
            has_long_puts = any(item.contracts > _ZERO for item in put_positions)
            records: list[ContractCollateral] = []
            naked_call_contracts = _ZERO
            naked_put_contracts = _ZERO
            adjusted_calls: list[tuple[Decimal, Decimal]] = []
            adjusted_puts: list[tuple[Decimal, Decimal]] = []
            for position in sorted(
                (*call_positions, *put_positions),
                key=lambda item: (
                    item.contract.right,
                    item.contract.strike,
                    str(item.contract),
                ),
            ):
                is_call = position.contract.right == "C"
                covered = covered_by_shares.get(str(position.contract), _ZERO)
                adjusted = position.contracts + covered
                short_remaining = max(-adjusted, _ZERO)
                has_long = has_long_calls if is_call else has_long_puts
                naked = (
                    _ZERO
                    if position.contracts >= _ZERO or has_long
                    else short_remaining
                )
                if is_call:
                    naked_call_contracts += naked
                    adjusted_calls.append(
                        (position.contract.strike, adjusted * multiplier)
                    )
                else:
                    naked_put_contracts += naked
                    adjusted_puts.append(
                        (position.contract.strike, adjusted * multiplier)
                    )
                records.append(
                    ContractCollateral(
                        symbol=str(position.contract),
                        root=root,
                        expiration=expiry,
                        right=position.contract.right,
                        projected_contracts=position.contracts,
                        covered_by_shares=covered,
                        covered_by_long_option=(
                            position.contracts < _ZERO and has_long
                        ),
                        naked_contracts=naked,
                    )
                )
            requirement = _scenario_requirement(adjusted_calls, adjusted_puts)
            groups.append(
                OptionGroupCollateral(
                    root=root,
                    expiration=expiry,
                    contracts=tuple(records),
                    requirement=requirement,
                    unlimited=requirement is None,
                    naked_put_contracts=naked_put_contracts,
                    naked_call_contracts=naked_call_contracts,
                )
            )
    return tuple(groups)


def option_collateral_report(
    order: OrderRequest,
    context: RiskContext,
    *,
    multiplier: DecimalInput = _DEFAULT_MULTIPLIER,
) -> OptionCollateralReport:
    """Return the exact projected option collateral for one candidate order.

    The projection combines the point-in-time portfolio snapshot with the
    order's signed legs; contract terms come from the OSI symbols alone.
    """
    scale = exact_decimal(multiplier, "multiplier")
    if scale <= _ZERO:
        raise ValueError("multiplier must be positive")
    _, projected = _projected_quantities(order, context)
    return OptionCollateralReport(scale, _build_groups(projected, scale))


def _portfolio_report(
    context: RiskContext, multiplier: Decimal
) -> OptionCollateralReport:
    """Return the collateral classification of the portfolio alone."""
    current: dict[Symbol, Decimal] = {}
    for position in context.portfolio.positions:
        current[position.symbol] = position.quantity
    return OptionCollateralReport(multiplier, _build_groups(current, multiplier))


def _limit_parameter(parameters: Mapping[str, object]) -> Decimal:
    for name in ("limit", "max_collateral", "max_option_collateral"):
        if name in parameters:
            value = exact_decimal(parameters[name], name)
            if value < _ZERO:
                raise ValueError(f"risk rule parameter {name!r} must be non-negative")
            return value
    raise ValueError("risk rule parameter 'limit' is required")


def _multiplier_parameter(parameters: Mapping[str, object]) -> Decimal:
    value = parameters.get("multiplier", _DEFAULT_MULTIPLIER)
    multiplier = exact_decimal(value, "multiplier")
    if multiplier <= _ZERO:
        raise ValueError("risk rule parameter 'multiplier' must be positive")
    return multiplier


def _group_metrics(group: OptionGroupCollateral) -> dict[str, object]:
    return {
        "requirement": group.requirement,
        "unlimited": group.unlimited,
        "naked_put_contracts": group.naked_put_contracts,
        "naked_call_contracts": group.naked_call_contracts,
        "contracts": {
            record.symbol: {
                "projected_contracts": record.projected_contracts,
                "covered_by_shares": record.covered_by_shares,
                "covered_by_long_option": record.covered_by_long_option,
                "naked_contracts": record.naked_contracts,
            }
            for record in group.contracts
        },
    }


def _collateral_metrics(
    report: OptionCollateralReport, **extra: object
) -> dict[str, object]:
    metrics: dict[str, object] = {
        "multiplier": report.multiplier,
        "total_collateral": report.total_collateral,
        "unlimited": report.unlimited,
        "naked_put_contracts": report.naked_put_contracts,
        "naked_call_contracts": report.naked_call_contracts,
        "groups": {
            f"{group.root} {group.expiration.isoformat()}": _group_metrics(group)
            for group in report.groups
        },
    }
    metrics.update(extra)
    return metrics


class NoNakedShortOptions(ConfiguredRiskRule):
    """Reject orders that create or increase naked short option exposure.

    Projected groups with an unlimited worst case always reject; there is no
    configuration bypass.  Short puts uncovered by long puts are naked
    cash-secured exposure: they reject unless ``allow_naked_short_put`` is
    configured, and even then only orders that do not increase any group's
    naked quantity pass, so buy-backs and spread repairs stay admissible.
    """

    def evaluate(self, order: OrderRequest, context: RiskContext) -> RuleDecision:
        parameters = self.parameters_for(context)
        multiplier = _multiplier_parameter(parameters)
        allow_naked_puts = parameters.get("allow_naked_short_put", False)
        if not isinstance(allow_naked_puts, bool):
            raise ValueError(
                "risk rule parameter 'allow_naked_short_put' must be a boolean"
            )
        report = option_collateral_report(order, context, multiplier=multiplier)
        before = _portfolio_report(context, multiplier)
        metrics = _collateral_metrics(
            report,
            naked_put_contracts_before=before.naked_put_contracts,
            allow_naked_short_put=allow_naked_puts,
        )
        if report.unlimited:
            return RuleDecision.reject(
                order,
                "no_naked_short_options leaves unlimited short option exposure",
                metrics=metrics,
            )
        before_by_key = {
            (group.root, group.expiration): group for group in before.groups
        }
        for group in report.groups:
            before_group = before_by_key.get((group.root, group.expiration))
            before_naked = (
                _ZERO if before_group is None else before_group.naked_put_contracts
            )
            if group.naked_put_contracts > before_naked and not allow_naked_puts:
                return RuleDecision.reject(
                    order,
                    "no_naked_short_options increases naked short put exposure",
                    metrics=metrics,
                )
        return RuleDecision.allow(order, metrics=metrics)


class OptionCollateralLimit(ConfiguredRiskRule):
    """Cap the exact worst-case option collateral after the candidate order.

    The requirement is the sum over contract groups of the exact worst-case
    additional cash implied by the projected positions.  Unlimited short-
    call exposure rejects outright; otherwise the projected total must stay
    at or below the configured limit.
    """

    def evaluate(self, order: OrderRequest, context: RiskContext) -> RuleDecision:
        parameters = self.parameters_for(context)
        limit = _limit_parameter(parameters)
        multiplier = _multiplier_parameter(parameters)
        report = option_collateral_report(order, context, multiplier=multiplier)
        metrics = _collateral_metrics(report, limit=limit)
        if report.unlimited:
            return RuleDecision.reject(
                order,
                "option_collateral_limit has unlimited short option exposure",
                metrics=metrics,
            )
        total = report.total_collateral
        if total is None or total <= limit:
            return RuleDecision.allow(order, metrics=metrics)
        return RuleDecision.reject(
            order, "option_collateral_limit exceeded", metrics=metrics
        )


__all__ = [
    "ContractCollateral",
    "NoNakedShortOptions",
    "OptionCollateralLimit",
    "OptionCollateralReport",
    "OptionGroupCollateral",
    "option_collateral_report",
]
