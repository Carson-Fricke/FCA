from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from harness.core.symbols import Symbol
from harness.core.types import Side, SingleLegOrder, TradeEvent
from harness.strategy import StrategyContext
from harness.strategy.adapter import StrategyBundleAdapter


class _Position:
    quantity = Decimal("2")


class _Universe:
    symbols = ("SPY",)


class _Books:
    def position(self, symbol: str) -> _Position:
        assert symbol == "SPY"
        return _Position()


class _Clock:
    def now(self) -> datetime:
        return datetime(2024, 1, 1, tzinfo=UTC)


class _Orders:
    def __init__(self) -> None:
        self.submitted: list[object] = []

    def submit(self, order: object) -> None:
        self.submitted.append(order)


class _Context:
    def __init__(self) -> None:
        self.universe = _Universe()
        self.books = _Books()
        self.clock = _Clock()
        self.orders = _Orders()
        self.state: dict[str, object] = {}
        self.metrics: dict[str, float] = {}

    def publish_metric(self, name: str, value: float) -> None:
        self.metrics[name] = value


def test_strategy_bundle_adapter_exits_on_frozen_model_falsification() -> None:
    source = """
from harness.strategy import Strategy

class Strategy(Strategy):
    def on_start(self, context):
        self.started = True
"""
    adapter = StrategyBundleAdapter(
        strategy_source={"strategy.py": source},
        strategy_entrypoint="Strategy",
        model_monitor={
            "model_source": (
                "def fit(context):\n"
                "    raise AssertionError('fit must not run')\n\n"
                "def observe(fit, context):\n"
                "    return {'model_health': fit['model_health']}\n"
            ),
            "model_fit_entrypoint": "fit",
            "model_observe_entrypoint": "observe",
            "fit": {"model_health": "0.1"},
            "indicators": [],
        },
        exit_criteria=[
            {
                "observable": "model_health",
                "condition": "lt",
                "threshold": "0.5",
            }
        ],
    )
    context = _Context()
    adapter.on_start(cast(StrategyContext, context))
    adapter.on_trade(
        TradeEvent(
            "trade-1",
            datetime(2024, 1, 1, tzinfo=UTC),
            1,
            Symbol("SPY"),
            Decimal("100"),
            Decimal("1"),
        )
    )

    assert context.metrics == {"model_health": 0.1}
    assert context.state == {"layer_b_falsified": "model_health"}
    assert len(context.orders.submitted) == 1
    order = cast(SingleLegOrder, context.orders.submitted[0])
    assert order.side is Side.SELL
