"""Fee schedule derivation from harness backtest configuration."""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from harness.configuration import FeeConfig
from harness.fills import (
    CryptoMakerTakerFeeModel,
    EquityRegulatoryFeeModel,
    FeeRounding,
    FeeSchedule,
    OptionsPerContractFeeModel,
    RoundingMode,
)
from harness.runtime.backtest.errors import _ZERO

DEFAULT_FEE_PRECISION: Final = Decimal("0.0001")


# --------------------------------------------------------------------------
# Fee schedule derivation
# --------------------------------------------------------------------------


def fee_schedule_from_config(
    fees: FeeConfig,
    *,
    precision: Decimal = DEFAULT_FEE_PRECISION,
    schedule_id: str = "harness-config",
) -> FeeSchedule:
    """Derive one :class:`FeeSchedule` from the configured fee rules.

    ``per_unit`` rules become per-share or per-contract commission, ``flat``
    rules become the commission minimum or per-contract fee, and
    ``notional_bps`` rules become the notional-basis-point component used by
    the equity regulatory and crypto maker/taker models.
    """
    if not isinstance(fees, FeeConfig):
        raise TypeError("fees must be a FeeConfig")
    rounding = FeeRounding(precision, RoundingMode(fees.rounding))
    per_unit = _sum_rates(fees, "per_unit")
    flat = _sum_rates(fees, "flat")
    notional_bps = _sum_rates(fees, "notional_bps")
    return FeeSchedule(
        schedule_id=schedule_id,
        currency=fees.currency,
        equity=EquityRegulatoryFeeModel(
            rounding=rounding,
            commission_per_share=per_unit,
            commission_minimum=flat,
            sec_rate_bps=notional_bps,
            taf_per_share=_ZERO,
            taf_maximum=_ZERO,
        ),
        option=OptionsPerContractFeeModel(
            rounding=rounding,
            commission_per_contract=per_unit,
            fee_per_contract=flat,
        ),
        crypto=CryptoMakerTakerFeeModel(
            rounding=rounding,
            maker_rate_bps=notional_bps,
            taker_rate_bps=notional_bps,
        ),
    )


def _sum_rates(fees: FeeConfig, basis: str) -> Decimal:
    return sum(
        (rule.rate for rule in fees.rules if rule.basis == basis),
        start=_ZERO,
    )
