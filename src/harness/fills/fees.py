"""Exact, configuration-driven fee calculation and aggregation."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from harness.configuration.loader import load_yaml_mapping
from harness.core.types.accounting import Fill
from harness.core.types.common import exact_decimal, text
from harness.core.types.orders import Side

_ZERO: Final = Decimal("0")
_BASIS_POINTS: Final = Decimal("10000")
_SCHEDULE_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_CURRENCY: Final = re.compile(r"^[A-Z]{3}$")


class AssetClass(StrEnum):
    """Asset classes with distinct fee calculation rules."""

    EQUITY = "equity"
    OPTION = "option"
    CRYPTO = "crypto"


class LiquidityRole(StrEnum):
    """Whether a crypto fill added or removed resting liquidity."""

    MAKER = "maker"
    TAKER = "taker"


class RoundingMode(StrEnum):
    """Supported deterministic decimal rounding modes."""

    HALF_EVEN = "half_even"
    HALF_UP = "half_up"
    CEILING = "ceiling"


@dataclass(frozen=True, slots=True)
class FeeRounding:
    """Currency precision and rounding mode applied to each fee component."""

    precision: Decimal
    mode: RoundingMode = RoundingMode.HALF_EVEN

    def __post_init__(self) -> None:
        precision = _positive_decimal(self.precision, "rounding.precision")
        if precision.normalize().as_tuple().digits != (1,):
            raise ValueError("rounding.precision must be a power of ten")
        object.__setattr__(self, "precision", precision)
        object.__setattr__(self, "mode", RoundingMode(self.mode))

    def apply(self, amount: Decimal) -> Decimal:
        """Round one exact amount without introducing binary floating point."""
        modes = {
            RoundingMode.HALF_EVEN: ROUND_HALF_EVEN,
            RoundingMode.HALF_UP: ROUND_HALF_UP,
            RoundingMode.CEILING: ROUND_CEILING,
        }
        return amount.quantize(self.precision, rounding=modes[self.mode])


@dataclass(frozen=True, slots=True)
class FeeContext:
    """Engine-owned attribution supplied independently of strategy logic."""

    run_id: str
    strategy_id: str
    asset_class: AssetClass

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", text(self.run_id, "run_id"))
        object.__setattr__(self, "strategy_id", text(self.strategy_id, "strategy_id"))
        object.__setattr__(self, "asset_class", AssetClass(self.asset_class))


@dataclass(frozen=True, slots=True)
class FeeComponent:
    """One named, rounded part of a fill's total fee."""

    fee_id: str
    amount: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "fee_id", _identifier(self.fee_id, "fee_id"))
        object.__setattr__(self, "amount", exact_decimal(self.amount, "amount"))


@dataclass(frozen=True, slots=True)
class FeeAssessment:
    """Itemized fees and attribution for one fill."""

    fill_id: str
    schedule_id: str
    currency: str
    context: FeeContext
    components: tuple[FeeComponent, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "fill_id", text(self.fill_id, "fill_id"))
        object.__setattr__(
            self, "schedule_id", _identifier(self.schedule_id, "schedule_id")
        )
        object.__setattr__(self, "currency", _currency(self.currency))
        if not isinstance(self.context, FeeContext):
            raise TypeError("context must be a FeeContext")
        components = tuple(self.components)
        if not all(isinstance(component, FeeComponent) for component in components):
            raise TypeError("components must contain FeeComponent values")
        fee_ids = [component.fee_id for component in components]
        if len(fee_ids) != len(set(fee_ids)):
            raise ValueError("fee component identifiers must be unique")
        object.__setattr__(self, "components", components)

    @property
    def total(self) -> Decimal:
        """Return the exact sum of already-rounded components."""
        return sum((component.amount for component in self.components), start=_ZERO)

    @property
    def run_id(self) -> str:
        """Return the run attribution used by aggregation."""
        return self.context.run_id

    @property
    def strategy_id(self) -> str:
        """Return the strategy-instance attribution used by aggregation."""
        return self.context.strategy_id

    @property
    def asset_class(self) -> AssetClass:
        """Return the asset class used to select the fee model."""
        return self.context.asset_class


class FeeModel(Protocol):
    """Structural contract implemented by each asset-class fee model."""

    def calculate(self, fill: Fill) -> tuple[FeeComponent, ...]:
        """Calculate rounded components for one fill."""


@dataclass(frozen=True, slots=True)
class EquityRegulatoryFeeModel:
    """Per-share commission plus sell-side SEC and FINRA TAF charges."""

    rounding: FeeRounding
    commission_per_share: Decimal
    commission_minimum: Decimal
    sec_rate_bps: Decimal
    taf_per_share: Decimal
    taf_maximum: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.rounding, FeeRounding):
            raise TypeError("rounding must be FeeRounding")
        for field_name in (
            "commission_per_share",
            "commission_minimum",
            "sec_rate_bps",
            "taf_per_share",
            "taf_maximum",
        ):
            object.__setattr__(
                self,
                field_name,
                _nonnegative_decimal(getattr(self, field_name), field_name),
            )

    def calculate(self, fill: Fill) -> tuple[FeeComponent, ...]:
        """Calculate commission and applicable sell-side regulatory fees."""
        _require_fill(fill)
        commission = max(
            fill.quantity * self.commission_per_share,
            self.commission_minimum,
        )
        components = [
            FeeComponent("commission", self.rounding.apply(commission)),
        ]
        if fill.side is Side.SELL:
            sec_fee = fill.quantity * fill.price * self.sec_rate_bps / _BASIS_POINTS
            taf_fee = min(fill.quantity * self.taf_per_share, self.taf_maximum)
            components.extend(
                (
                    FeeComponent("sec-regulatory", self.rounding.apply(sec_fee)),
                    FeeComponent("finra-taf", self.rounding.apply(taf_fee)),
                )
            )
        return tuple(components)


@dataclass(frozen=True, slots=True)
class OptionsPerContractFeeModel:
    """Commission and exchange/regulatory charges assessed per contract."""

    rounding: FeeRounding
    commission_per_contract: Decimal
    fee_per_contract: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.rounding, FeeRounding):
            raise TypeError("rounding must be FeeRounding")
        object.__setattr__(
            self,
            "commission_per_contract",
            _nonnegative_decimal(
                self.commission_per_contract, "commission_per_contract"
            ),
        )
        object.__setattr__(
            self,
            "fee_per_contract",
            _nonnegative_decimal(self.fee_per_contract, "fee_per_contract"),
        )

    def calculate(self, fill: Fill) -> tuple[FeeComponent, ...]:
        """Calculate exact per-contract charges for an integral fill quantity."""
        _require_fill(fill)
        if fill.quantity != fill.quantity.to_integral_value():
            raise ValueError("option fill quantity must be a whole number of contracts")
        return (
            FeeComponent(
                "contract-commission",
                self.rounding.apply(fill.quantity * self.commission_per_contract),
            ),
            FeeComponent(
                "contract-fee",
                self.rounding.apply(fill.quantity * self.fee_per_contract),
            ),
        )


@dataclass(frozen=True, slots=True)
class CryptoMakerTakerFeeModel:
    """Notional-basis-point fees selected from the fill's liquidity role."""

    rounding: FeeRounding
    maker_rate_bps: Decimal
    taker_rate_bps: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.rounding, FeeRounding):
            raise TypeError("rounding must be FeeRounding")
        object.__setattr__(
            self,
            "maker_rate_bps",
            exact_decimal(self.maker_rate_bps, "maker_rate_bps"),
        )
        object.__setattr__(
            self,
            "taker_rate_bps",
            _nonnegative_decimal(self.taker_rate_bps, "taker_rate_bps"),
        )

    def calculate(self, fill: Fill) -> tuple[FeeComponent, ...]:
        """Calculate a maker charge/rebate or taker charge from fill notional."""
        _require_fill(fill)
        try:
            liquidity = LiquidityRole(fill.liquidity)
        except (TypeError, ValueError) as error:
            raise ValueError("crypto fills require maker or taker liquidity") from error
        rate = (
            self.maker_rate_bps
            if liquidity is LiquidityRole.MAKER
            else self.taker_rate_bps
        )
        amount = fill.quantity * fill.price * rate / _BASIS_POINTS
        return (FeeComponent(f"{liquidity.value}-fee", self.rounding.apply(amount)),)


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """A YAML-selected set of models covering every supported asset class."""

    schedule_id: str
    currency: str
    equity: EquityRegulatoryFeeModel
    option: OptionsPerContractFeeModel
    crypto: CryptoMakerTakerFeeModel

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "schedule_id", _identifier(self.schedule_id, "schedule_id")
        )
        object.__setattr__(self, "currency", _currency(self.currency))
        expected = (
            (self.equity, EquityRegulatoryFeeModel, "equity"),
            (self.option, OptionsPerContractFeeModel, "option"),
            (self.crypto, CryptoMakerTakerFeeModel, "crypto"),
        )
        for model, model_type, name in expected:
            if not isinstance(model, model_type):
                raise TypeError(f"{name} must be {model_type.__name__}")

    def calculate(self, fill: Fill, context: FeeContext) -> FeeAssessment:
        """Select a model by explicit asset class and assess one fill."""
        _require_fill(fill)
        if not isinstance(context, FeeContext):
            raise TypeError("context must be a FeeContext")
        models: dict[AssetClass, FeeModel] = {
            AssetClass.EQUITY: self.equity,
            AssetClass.OPTION: self.option,
            AssetClass.CRYPTO: self.crypto,
        }
        return FeeAssessment(
            fill_id=fill.fill_id,
            schedule_id=self.schedule_id,
            currency=self.currency,
            context=context,
            components=models[context.asset_class].calculate(fill),
        )

    def apply(self, fill: Fill, context: FeeContext) -> tuple[Fill, FeeAssessment]:
        """Return a new fill carrying the configured fee and its audit detail."""
        assessment = self.calculate(fill, context)
        return replace(fill, fee=assessment.total), assessment


@dataclass(frozen=True, slots=True)
class FeeAggregate:
    """One exact currency total for an aggregation key."""

    key: str
    currency: str
    amount: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", text(self.key, "key"))
        object.__setattr__(self, "currency", _currency(self.currency))
        object.__setattr__(self, "amount", exact_decimal(self.amount, "amount"))


@dataclass(frozen=True, slots=True)
class FeeAggregation:
    """Deterministic fee totals across all required attribution dimensions."""

    by_run: tuple[FeeAggregate, ...]
    by_strategy: tuple[FeeAggregate, ...]
    by_asset_class: tuple[FeeAggregate, ...]


def aggregate_fees(assessments: Iterable[FeeAssessment]) -> FeeAggregation:
    """Aggregate assessments by run, strategy, and asset class and currency."""
    by_run: dict[tuple[str, str], Decimal] = {}
    by_strategy: dict[tuple[str, str], Decimal] = {}
    by_asset_class: dict[tuple[str, str], Decimal] = {}
    seen_fills: set[tuple[str, str]] = set()
    for assessment in assessments:
        if not isinstance(assessment, FeeAssessment):
            raise TypeError("assessments must contain FeeAssessment values")
        fill_key = (assessment.run_id, assessment.fill_id)
        if fill_key in seen_fills:
            raise ValueError(
                f"duplicate fee assessment for run/fill {fill_key[0]!r}/{fill_key[1]!r}"
            )
        seen_fills.add(fill_key)
        _add_total(by_run, assessment.run_id, assessment)
        _add_total(by_strategy, assessment.strategy_id, assessment)
        _add_total(by_asset_class, assessment.asset_class.value, assessment)
    return FeeAggregation(
        by_run=_aggregates(by_run),
        by_strategy=_aggregates(by_strategy),
        by_asset_class=_aggregates(by_asset_class),
    )


def fees_config_dir(root: str | Path | None = None) -> Path:
    """Resolve ``config/fees`` from a repository root or direct config path."""
    root_path = Path(__file__).resolve().parents[3] if root is None else Path(root)
    if root_path.name == "fees" and root_path.parent.name == "config":
        return root_path
    return root_path / "config" / "fees"


def load_fee_schedule(
    selection: str,
    root: str | Path | None = None,
) -> FeeSchedule:
    """Safely load ``config/fees/<selection>.yaml`` with strict validation."""
    schedule_id = _identifier(selection, "fee schedule selection")
    values = load_yaml_mapping(fees_config_dir(root) / f"{schedule_id}.yaml")
    _keys(
        values,
        {"schema_version", "schedule_id", "currency", "rounding", "models"},
        "fee schedule",
    )
    if values["schema_version"] != 1 or isinstance(values["schema_version"], bool):
        raise ValueError("fee schedule schema_version must be 1")
    configured_id = _identifier(values["schedule_id"], "schedule_id")
    if configured_id != schedule_id:
        raise ValueError("configured schedule_id must match the selected file")
    rounding_values = _mapping(values["rounding"], "rounding")
    _keys(rounding_values, {"precision", "mode"}, "rounding")
    rounding = FeeRounding(
        precision=_decimal_value(rounding_values["precision"], "rounding.precision"),
        mode=_rounding_mode(rounding_values["mode"]),
    )
    models = _mapping(values["models"], "models")
    _keys(models, {"equity", "option", "crypto"}, "models")
    return FeeSchedule(
        schedule_id=configured_id,
        currency=_currency(values["currency"]),
        equity=_equity_model(models["equity"], rounding),
        option=_option_model(models["option"], rounding),
        crypto=_crypto_model(models["crypto"], rounding),
    )


def _equity_model(value: object, rounding: FeeRounding) -> EquityRegulatoryFeeModel:
    values = _mapping(value, "models.equity")
    fields = {
        "model",
        "commission_per_share",
        "commission_minimum",
        "sec_rate_bps",
        "taf_per_share",
        "taf_maximum",
    }
    _keys(values, fields, "models.equity")
    _model_name(values["model"], "equity_regulatory", "models.equity.model")
    return EquityRegulatoryFeeModel(
        rounding=rounding,
        commission_per_share=_decimal_value(
            values["commission_per_share"], "commission_per_share"
        ),
        commission_minimum=_decimal_value(
            values["commission_minimum"], "commission_minimum"
        ),
        sec_rate_bps=_decimal_value(values["sec_rate_bps"], "sec_rate_bps"),
        taf_per_share=_decimal_value(values["taf_per_share"], "taf_per_share"),
        taf_maximum=_decimal_value(values["taf_maximum"], "taf_maximum"),
    )


def _option_model(value: object, rounding: FeeRounding) -> OptionsPerContractFeeModel:
    values = _mapping(value, "models.option")
    fields = {"model", "commission_per_contract", "fee_per_contract"}
    _keys(values, fields, "models.option")
    _model_name(values["model"], "options_per_contract", "models.option.model")
    return OptionsPerContractFeeModel(
        rounding=rounding,
        commission_per_contract=_decimal_value(
            values["commission_per_contract"], "commission_per_contract"
        ),
        fee_per_contract=_decimal_value(values["fee_per_contract"], "fee_per_contract"),
    )


def _crypto_model(value: object, rounding: FeeRounding) -> CryptoMakerTakerFeeModel:
    values = _mapping(value, "models.crypto")
    fields = {"model", "maker_rate_bps", "taker_rate_bps"}
    _keys(values, fields, "models.crypto")
    _model_name(values["model"], "crypto_maker_taker", "models.crypto.model")
    return CryptoMakerTakerFeeModel(
        rounding=rounding,
        maker_rate_bps=_decimal_value(values["maker_rate_bps"], "maker_rate_bps"),
        taker_rate_bps=_decimal_value(values["taker_rate_bps"], "taker_rate_bps"),
    )


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    if not all(isinstance(key, str) for key in value):
        raise TypeError(f"{field_name} keys must be strings")
    return value


def _keys(values: Mapping[str, object], expected: set[str], field_name: str) -> None:
    actual = set(values)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details = []
        if missing:
            details.append(f"missing {missing}")
        if extra:
            details.append(f"unexpected {extra}")
        raise ValueError(f"{field_name} fields are invalid: {', '.join(details)}")


def _model_name(value: object, expected: str, field_name: str) -> None:
    if value != expected:
        raise ValueError(f"{field_name} must be {expected!r}")


def _rounding_mode(value: object) -> RoundingMode:
    if not isinstance(value, str):
        raise TypeError("rounding.mode must be a string")
    return RoundingMode(value)


def _decimal_value(value: object, field_name: str) -> Decimal:
    if not isinstance(value, Decimal | int) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be an exact YAML decimal")
    return exact_decimal(value, field_name)


def _positive_decimal(value: object, field_name: str) -> Decimal:
    result = exact_decimal(value, field_name)
    if result <= _ZERO:
        raise ValueError(f"{field_name} must be positive")
    return result


def _nonnegative_decimal(value: object, field_name: str) -> Decimal:
    result = exact_decimal(value, field_name)
    if result < _ZERO:
        raise ValueError(f"{field_name} must not be negative")
    return result


def _identifier(value: object, field_name: str) -> str:
    result = text(value, field_name)
    if _SCHEDULE_ID.fullmatch(result) is None:
        raise ValueError(f"{field_name} contains invalid characters")
    return result


def _currency(value: object) -> str:
    if not isinstance(value, str) or _CURRENCY.fullmatch(value) is None:
        raise ValueError("currency must be a three-letter uppercase code")
    return value


def _require_fill(fill: object) -> None:
    if not isinstance(fill, Fill):
        raise TypeError("fill must be a Fill")


def _add_total(
    totals: dict[tuple[str, str], Decimal],
    key: str,
    assessment: FeeAssessment,
) -> None:
    total_key = (key, assessment.currency)
    totals[total_key] = totals.get(total_key, _ZERO) + assessment.total


def _aggregates(totals: Mapping[tuple[str, str], Decimal]) -> tuple[FeeAggregate, ...]:
    return tuple(
        FeeAggregate(key=key, currency=currency, amount=amount)
        for (key, currency), amount in sorted(totals.items())
    )


__all__ = [
    "AssetClass",
    "CryptoMakerTakerFeeModel",
    "EquityRegulatoryFeeModel",
    "FeeAggregate",
    "FeeAggregation",
    "FeeAssessment",
    "FeeComponent",
    "FeeContext",
    "FeeModel",
    "FeeRounding",
    "FeeSchedule",
    "LiquidityRole",
    "OptionsPerContractFeeModel",
    "RoundingMode",
    "aggregate_fees",
    "fees_config_dir",
    "load_fee_schedule",
]
