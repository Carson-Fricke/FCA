"""Platform adapter from historical replay events to a research model.

The native engine currently owns historical event replay. This adapter reuses
that deterministic replay without presenting workspace model code as a
strategy: model and indicator source receive only a plain data mapping, and
only this platform class sees the strategy callback/context seam.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from decimal import Decimal

from harness.core.types import QuoteEvent, TradeEvent
from harness.indicators import TickIndicator, create_indicator
from harness.research.programs import (
    evaluate_indicator,
    fit_model,
    observe_model,
)


class ResearchModelMonitor:
    """Evaluate the same no-order model from research or strategy callbacks."""

    def __init__(
        self,
        *,
        model_source: str,
        model_fit_entrypoint: str = "fit",
        model_observe_entrypoint: str = "observe",
        fit: Mapping[str, float] | None = None,
        indicators: Sequence[Mapping[str, object]] = (),
        model_id: str | None = None,
        outputs: Sequence[str] = (),
        **_extra: object,
    ) -> None:
        self._model_source = model_source
        self._model_fit_entrypoint = model_fit_entrypoint
        self._model_observe_entrypoint = model_observe_entrypoint
        self._fit = None if fit is None else dict(fit)
        self._bindings = tuple(dict(binding) for binding in indicators)
        self._model_id = model_id
        self._outputs = tuple(str(o) for o in outputs)
        self._quotes: list[dict[str, object]] = []
        self._trades: list[dict[str, object]] = []
        self._global: dict[str, TickIndicator] = {}
        for binding in self._bindings:
            if binding.get("scope") != "global":
                continue
            alias = _text(binding, "alias")
            self._global[alias] = create_indicator(
                _text(binding, "name"),
                _mapping(binding.get("parameters")),
                symbol=_text(binding, "symbol"),
            )

    def on_quote(self, event: QuoteEvent) -> None:
        self._quotes.append(
            {
                "ask_price": _number(event.ask_price),
                "ask_quantity": _number(event.ask_quantity),
                "bid_price": _number(event.bid_price),
                "bid_quantity": _number(event.bid_quantity),
                "sequence": event.sequence,
                "symbol": str(event.symbol),
                "timestamp": event.timestamp.isoformat(),
            }
        )
        self._route_global(event)

    def on_trade(self, event: TradeEvent) -> None:
        self._trades.append(
            {
                "price": _number(event.price),
                "quantity": _number(event.quantity),
                "sequence": event.sequence,
                "symbol": str(event.symbol),
                "timestamp": event.timestamp.isoformat(),
            }
        )
        self._route_global(event)

    def _context_with_indicators(self) -> dict[str, object]:
        data = self._research_context()
        values: dict[str, object] = {}
        for alias, indicator in self._global.items():
            indicator.flush()
            values[alias] = {
                "samples": [
                    {
                        "end": sample.end.isoformat(),
                        "start": sample.start.isoformat(),
                        "value": _number(sample.value),
                    }
                    for sample in indicator.samples()
                ],
                "value": None if indicator.value is None else _number(indicator.value),
            }
        for binding in self._bindings:
            if binding.get("scope") != "workspace":
                continue
            alias = _text(binding, "alias")
            values[alias] = evaluate_indicator(
                _text(binding, "source"),
                _text(binding, "entrypoint"),
                data,
                _mapping(binding.get("parameters")),
            )
        data["indicators"] = values
        return data

    def fit(self) -> dict[str, float]:
        """Fit once on the accepted historical events."""
        if self._fit is None:
            self._fit = fit_model(
                self._model_source,
                self._model_fit_entrypoint,
                self._context_with_indicators(),
            )
        return dict(self._fit)

    def observe(self) -> dict[str, float]:
        """Observe current events using the frozen fit; never refit."""
        fit = self._fit
        if fit is None:
            raise ValueError("research model monitor requires a frozen fit")
        return observe_model(
            self._model_source,
            self._model_observe_entrypoint,
            fit,
            self._context_with_indicators(),
        )

    def _route_global(self, event: QuoteEvent | TradeEvent) -> None:
        for indicator in self._global.values():
            if str(indicator.symbol) == str(event.symbol):
                indicator.on_event(event)

    def _research_context(self) -> dict[str, object]:
        prices: dict[str, list[float]] = defaultdict(list)
        for trade in self._trades:
            prices[str(trade["symbol"])].append(_stored_number(trade["price"]))
        for quote in self._quotes:
            symbol = str(quote["symbol"])
            if symbol not in prices:
                mid = (
                    _stored_number(quote["bid_price"])
                    + _stored_number(quote["ask_price"])
                ) / 2.0
                prices[symbol].append(mid)
        returns: dict[str, list[float]] = {}
        for symbol, values in prices.items():
            returns[symbol] = [
                (current / previous) - 1.0
                for previous, current in zip(values, values[1:], strict=False)
                if previous != 0.0
            ]
        sessions: list[int] = []
        current_session = 0
        last_date = None
        for event_dict in self._trades or self._quotes:
            ts_str = str(event_dict.get("timestamp", ""))
            date_str = ts_str.split("T")[0] if "T" in ts_str else ts_str[:10]
            if last_date is not None and date_str != last_date:
                current_session += 1
            last_date = date_str
            sessions.append(current_session)
        return {
            "quotes": list(self._quotes),
            "returns": returns,
            "session_ids": sessions,
            "trade_prices": dict(prices),
            "trades": list(self._trades),
        }


def _mapping(value: object) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("indicator parameters must be a mapping")
    return dict(value)


def _text(value: Mapping[str, object], key: str) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate.strip():
        raise ValueError(f"indicator binding requires non-empty {key}")
    return candidate.strip()


def _number(value: Decimal | int | float | str) -> float:
    return float(value)


def _stored_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("stored research event value must be numeric")
    return float(value)


__all__ = ["ResearchModelMonitor"]
