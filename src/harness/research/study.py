"""Platform study program: a no-order replay program for research models.

A study program is the platform-side counterpart of a strategy. It receives
market events, owns a frozen research model monitor, and on stop fits the model
once and publishes its observations as metrics. It deliberately has no order,
position, sizing, portfolio, or venue capability: its only context interaction
is ``publish_metric``. The workspace model source it runs receives plain data
mappings and never a strategy or order surface.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from harness.core.types import QuoteEvent, TradeEvent
from harness.research.adapter import ResearchModelMonitor


class MetricPublisher(Protocol):
    """The only context capability a study program may use."""

    def publish_metric(self, name: str, value: float) -> None: ...


class ResearchModelProgram:
    """Run one research model through a study replay with no order execution."""

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
        **extra: object,
    ) -> None:
        del extra
        self._monitor = ResearchModelMonitor(
            model_source=model_source,
            model_fit_entrypoint=model_fit_entrypoint,
            model_observe_entrypoint=model_observe_entrypoint,
            fit=fit,
            indicators=indicators,
            model_id=model_id,
            outputs=outputs,
        )
        self._outputs = tuple(str(name) for name in outputs)

    def on_quote(self, event: QuoteEvent) -> None:
        self._monitor.on_quote(event)

    def on_trade(self, event: TradeEvent) -> None:
        self._monitor.on_trade(event)

    def on_stop(self, context: MetricPublisher) -> None:
        publish = context.publish_metric
        try:
            fitted = self._monitor.fit()
        except Exception:  # noqa: BLE001 - a fit failure is recorded as evidence
            publish("sample_valid", 0.0)
            publish("fit.sample_valid", 0.0)
            publish("data_ok", 0.0)
            publish("fit.data_ok", 0.0)
            publish("fit.failed", 1.0)
            for name in self._outputs:
                if name not in ("sample_valid", "data_ok"):
                    publish(name, 0.0)
            return
        for name, value in fitted.items():
            publish(f"fit.{name}", value)
        try:
            observations = self._monitor.observe()
        except Exception:  # noqa: BLE001 - an observe failure is recorded as evidence
            publish("observe.failed", 1.0)
            for name in self._outputs:
                if name not in fitted:
                    publish(name, 0.0)
            return
        for name, value in observations.items():
            publish(name, value)


__all__ = ["MetricPublisher", "ResearchModelProgram"]
