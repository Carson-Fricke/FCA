"""Explicit per-data-type history valves for approved backfill scope."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from lake.configuration import EquityUniverse
from lake.contracts import DataType


@dataclass(frozen=True, slots=True)
class HistoryLimit:
    """Maximum approved liquidity rank for one trade or quote history."""

    data_type: DataType
    maximum_liquidity_rank: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_type", DataType(self.data_type))
        if self.maximum_liquidity_rank is not None and (
            isinstance(self.maximum_liquidity_rank, bool)
            or not isinstance(self.maximum_liquidity_rank, int)
            or self.maximum_liquidity_rank < 1
        ):
            raise ValueError("maximum liquidity rank must be positive or None")

    def allows(self, liquidity_rank: int | None) -> bool:
        """Return whether a symbol rank is approved for this data type."""
        if liquidity_rank is not None and (
            isinstance(liquidity_rank, bool)
            or not isinstance(liquidity_rank, int)
            or liquidity_rank < 1
        ):
            raise ValueError("liquidity rank must be positive or None")
        maximum = self.maximum_liquidity_rank
        return maximum is None or (
            liquidity_rank is not None and liquidity_rank <= maximum
        )


@dataclass(frozen=True, slots=True)
class HistoryValve:
    """Immutable trade/quote scope applied before queue dispatch."""

    trade: HistoryLimit
    quote: HistoryLimit

    def __post_init__(self) -> None:
        if self.trade.data_type is not DataType.TRADE:
            raise ValueError("trade history limit must target trade data")
        if self.quote.data_type is not DataType.QUOTE:
            raise ValueError("quote history limit must target quote data")

    @classmethod
    def from_universe(cls, universe: EquityUniverse) -> HistoryValve:
        """Translate T026/T015's explicit universe history limits."""
        return cls(
            trade=HistoryLimit(
                DataType.TRADE,
                universe.history[DataType.TRADE].maximum_liquidity_rank,
            ),
            quote=HistoryLimit(
                DataType.QUOTE,
                universe.history[DataType.QUOTE].maximum_liquidity_rank,
            ),
        )

    @classmethod
    def from_limits(
        cls,
        *,
        trade: int | None = None,
        quote: int | None = None,
    ) -> HistoryValve:
        """Build a valve from separately approved trade and quote ranks."""
        return cls(
            trade=HistoryLimit(DataType.TRADE, trade),
            quote=HistoryLimit(DataType.QUOTE, quote),
        )

    @classmethod
    def from_mapping(
        cls,
        limits: Mapping[DataType | str, int | None],
    ) -> HistoryValve:
        """Build a valve from normalized or string data-type keys."""
        normalized: dict[DataType, int | None] = {}
        for key, value in limits.items():
            data_type = DataType(key)
            if data_type in normalized:
                raise ValueError(f"duplicate history limit for {data_type.value}")
            normalized[data_type] = value
        if set(normalized) != {DataType.TRADE, DataType.QUOTE}:
            raise ValueError("history limits must define trade and quote")
        return cls.from_limits(
            trade=normalized[DataType.TRADE],
            quote=normalized[DataType.QUOTE],
        )

    def limit_for(self, data_type: DataType | str) -> HistoryLimit:
        """Return the explicit limit for one supported data type."""
        normalized = DataType(data_type)
        return self.trade if normalized is DataType.TRADE else self.quote

    def allows(
        self,
        data_type: DataType | str,
        *,
        liquidity_rank: int | None,
    ) -> bool:
        """Return whether this ranked symbol/data-type pair is approved."""
        return self.limit_for(data_type).allows(liquidity_rank)


__all__ = ["HistoryLimit", "HistoryValve"]
