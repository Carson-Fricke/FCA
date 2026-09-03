"""Reusable deterministic golden-run support for harness tests.

This module is deliberately an adapter for tests.  Each run owns its clock,
event bus, venue, portfolio, and strategy binding; no process-wide clock,
random source, network client, or identifier generator is used here.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType

from harness.configuration.hashing import (
    canonical_configuration_bytes,
    configuration_hash,
)
from harness.core.clock import SimClock
from harness.core.events import EventBus
from harness.core.symbols import Symbol, canonical_symbol
from harness.core.types import (
    Event,
    OrderType,
    ParquetCodec,
    QuoteEvent,
    Side,
    SingleLegOrder,
    TimeInForce,
)
from harness.core.types.common import exact_decimal
from harness.portfolio import PortfolioHub
from harness.strategy import (
    Strategy,
    StrategyBinding,
    StrategyContext,
    Subscription,
    bind_strategy,
)
from harness.venues import (
    ScriptedStep,
    ScriptedVenue,
)


def _normalized(value: object, path: str = "value") -> object:
    """Convert records to configuration-shaped values without hiding order."""
    if value is None or isinstance(value, str | bool | int | Decimal | datetime):
        return value
    if isinstance(value, float):
        raise TypeError(f"{path} must not contain binary floating point")
    if isinstance(value, Enum):
        return _normalized(value.value, path)
    if isinstance(value, Mapping):
        return {key: _normalized(item, f"{path}.{key}") for key, item in value.items()}
    if isinstance(value, set | frozenset):
        raise TypeError(f"{path} contains an unordered set")
    if isinstance(value, bytes | bytearray):
        raise TypeError(f"{path} contains binary data; provide bytes as an artifact")
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _normalized(getattr(value, field.name), f"{path}.{field.name}")
            for field in fields(value)
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [
            _normalized(item, f"{path}[{index}]") for index, item in enumerate(value)
        ]
    raise TypeError(f"{path} contains unsupported {type(value).__name__}")


def _class_name(value: object) -> str:
    value_type = value if isinstance(value, type) else type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _validate_artifact_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("artifact name must be a non-empty string")
    normalized = name.strip()
    if (
        normalized.startswith("/")
        or "\\" in normalized
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
    ):
        raise ValueError("artifact name must be a relative slash-separated path")
    return normalized


@dataclass(frozen=True, slots=True)
class SyntheticFeed:
    """Immutable, timestamped feed input published in its supplied order."""

    events: tuple[Event, ...] | Sequence[Event]

    def __post_init__(self) -> None:
        events = tuple(self.events)
        if not all(isinstance(event, Event) for event in events):
            raise TypeError("events must contain Event values")
        object.__setattr__(self, "events", events)

    def __iter__(self):
        return iter(self.events)

    @property
    def start(self) -> datetime | None:
        """Return the earliest explicit event timestamp, if any."""
        return (
            None if not self.events else min(event.timestamp for event in self.events)
        )

    def publish(self, bus: EventBus) -> None:
        """Publish every event without consulting a clock or external source."""
        if not isinstance(bus, EventBus):
            raise TypeError("bus must be an EventBus")
        bus.publish_many(self.events)


class ToyStrategy(Strategy):
    """Small quote-triggered strategy used by golden scenarios.

    All order identity and order values are explicit constructor parameters.
    The strategy submits at most one order, making scripted venue scenarios
    concise while still exercising strategy binding and the command boundary.
    """

    def __init__(
        self,
        order_id: str = "toy-order",
        symbol: str = "AAPL",
        quantity: Decimal | int | str = Decimal("1"),
        side: Side = Side.BUY,
        order_type: OrderType = OrderType.MARKET,
        limit_price: Decimal | int | str | None = None,
        time_in_force: TimeInForce = TimeInForce.DAY,
        client_order_id: str | None = None,
    ) -> None:
        self.order_id = order_id
        self.symbol = canonical_symbol(symbol)
        self.quantity = exact_decimal(quantity, "quantity")
        self.side = Side(side)
        self.order_type = OrderType(order_type)
        self.limit_price = (
            None if limit_price is None else exact_decimal(limit_price, "limit_price")
        )
        self.time_in_force = TimeInForce(time_in_force)
        self.client_order_id = client_order_id
        self.callbacks: list[str] = []
        self.events: list[Event] = []
        self.orders: list[SingleLegOrder] = []
        self._context: StrategyContext | None = None

    def on_start(self, context: StrategyContext) -> None:
        self._context = context
        self.callbacks.append("on_start")

    def on_quote(self, event: QuoteEvent) -> None:
        self.callbacks.append("on_quote")
        self.events.append(event)
        if self.orders or event.symbol != self.symbol:
            return
        order = SingleLegOrder(
            event_id=f"strategy-order-event-{self.order_id}",
            timestamp=event.timestamp,
            sequence=event.sequence,
            order_id=self.order_id,
            symbol=self.symbol,
            side=self.side,
            quantity=self.quantity,
            order_type=self.order_type,
            time_in_force=self.time_in_force,
            limit_price=self.limit_price,
            client_order_id=self.client_order_id,
        )
        context = self._context
        if context is None:
            raise RuntimeError("toy strategy has not received on_start")
        context.orders.submit(order)
        self.orders.append(order)

    def on_stop(self, context: StrategyContext) -> None:
        self.callbacks.append("on_stop")


@dataclass(frozen=True, slots=True)
class GoldenScenario:
    """Complete deterministic input for one fresh golden-harness run."""

    feed: SyntheticFeed
    venue_steps: tuple[ScriptedStep, ...] | Sequence[ScriptedStep]
    strategy_class: type[Strategy] = ToyStrategy
    strategy_parameters: Mapping[str, object] = MappingProxyType({})
    configuration: Mapping[str, object] = MappingProxyType({})
    account_id: str = "golden-account"
    book_id: str = "golden-strategy"
    initial_cash: Decimal | int | str = Decimal("100000")
    seed: int = 0
    start: datetime | None = None
    universe: tuple[str | Symbol, ...] | Sequence[str | Symbol] = ()
    subscriptions: tuple[Subscription, ...] | Sequence[Subscription] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.feed, SyntheticFeed):
            raise TypeError("feed must be a SyntheticFeed")
        steps = tuple(self.venue_steps)
        if not all(isinstance(step, ScriptedStep) for step in steps):
            raise TypeError("venue_steps must contain ScriptedStep values")
        object.__setattr__(self, "venue_steps", steps)
        if not isinstance(self.strategy_class, type) or not issubclass(
            self.strategy_class, Strategy
        ):
            raise TypeError("strategy_class must derive from Strategy")
        if not isinstance(self.strategy_parameters, Mapping):
            raise TypeError("strategy_parameters must be a mapping")
        if not isinstance(self.configuration, Mapping):
            raise TypeError("configuration must be a mapping")
        object.__setattr__(
            self,
            "strategy_parameters",
            MappingProxyType(dict(self.strategy_parameters)),
        )
        object.__setattr__(
            self, "configuration", MappingProxyType(dict(self.configuration))
        )
        normalized_universe = tuple(
            canonical_symbol(symbol) for symbol in self.universe
        )
        if not normalized_universe:
            normalized_universe = tuple(
                sorted(
                    {
                        event.symbol
                        for event in self.feed.events
                        if isinstance(event, QuoteEvent)
                    },
                    key=str,
                )
            )
        object.__setattr__(self, "universe", normalized_universe)
        if self.subscriptions is None:
            subscriptions = (Subscription(QuoteEvent),)
        else:
            subscriptions = tuple(self.subscriptions)
            if not all(isinstance(item, Subscription) for item in subscriptions):
                raise TypeError("subscriptions must contain Subscription values")
        object.__setattr__(self, "subscriptions", subscriptions)
        clock_start = self.clock_start
        if clock_start is None:
            raise ValueError("a non-empty feed or explicit start is required")
        if any(event.timestamp < clock_start for event in self.feed.events):
            raise ValueError("feed event cannot precede the scenario clock start")

    @property
    def clock_start(self) -> datetime | None:
        """Return the explicit start or the earliest feed event timestamp."""
        return self.start if self.start is not None else self.feed.start

    @property
    def configuration_for_hash(self) -> Mapping[str, object]:
        """Return portable inputs that identify this scenario configuration."""
        return {
            "scenario": self.configuration,
            "strategy": {
                "class": f"{self.strategy_class.__module__}.{self.strategy_class.__qualname__}",
                "parameters": self.strategy_parameters,
            },
            "account_id": self.account_id,
            "book_id": self.book_id,
            "initial_cash": self.initial_cash,
            "seed": self.seed,
            "start": self.clock_start,
            "universe": self.universe,
            "subscriptions": [
                {
                    "selector": (
                        item.selector.value
                        if isinstance(item.selector, Enum)
                        else _class_name(item.selector)
                    ),
                    "symbols": item.symbols,
                }
                for item in self.subscriptions or ()
            ],
        }

    def run(self) -> GoldenRun:
        """Run this scenario with a fresh deterministic runtime."""
        return run_scenario(self)


@dataclass(frozen=True, slots=True)
class GoldenArtifact:
    """One named canonical byte payload and its content digest."""

    name: str
    payload: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_artifact_name(self.name))
        if not isinstance(self.payload, bytes):
            raise TypeError("artifact payload must be bytes")

    @property
    def sha256(self) -> str:
        return _sha256(self.payload)

    @property
    def byte_length(self) -> int:
        return len(self.payload)


@dataclass(frozen=True, slots=True)
class ArtifactDigest:
    """Portable manifest entry for one artifact."""

    name: str
    sha256: str
    byte_length: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_artifact_name(self.name))
        if len(self.sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.sha256
        ):
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        if isinstance(self.byte_length, bool) or self.byte_length < 0:
            raise ValueError("byte_length must be non-negative")


class ArtifactCanonicalizer:
    """Canonicalize structured test output and homogeneous event collections."""

    @staticmethod
    def json_bytes(value: object) -> bytes:
        """Encode configuration-shaped data as compact canonical UTF-8 JSON."""
        return canonical_configuration_bytes(_normalized(value))

    @staticmethod
    def event_bytes(events: Iterable[Event]) -> bytes:
        """Encode one homogeneous event collection using the Parquet codec."""
        values = tuple(events)
        if not values:
            raise ValueError("event_bytes requires at least one event")
        if not all(isinstance(event, Event) for event in values):
            raise TypeError("events must contain Event values")
        record_type = type(values[0])
        if any(type(event) is not record_type for event in values):
            raise TypeError("event_bytes requires one exact event type")
        return ParquetCodec.encode(values, record_type)

    @classmethod
    def event_artifacts(cls, prefix: str, events: Iterable[Event]) -> dict[str, bytes]:
        """Return stable per-event-type Parquet artifacts for an event stream."""
        normalized_prefix = _validate_artifact_name(prefix).rstrip("/")
        values = tuple(events)
        groups: dict[type[Event], list[Event]] = {}
        for event in values:
            if not isinstance(event, Event):
                raise TypeError("events must contain Event values")
            groups.setdefault(type(event), []).append(event)
        if not groups:
            return {f"{normalized_prefix}/empty.json": cls.json_bytes({"events": []})}
        return {
            f"{normalized_prefix}/{_class_name(record_type)}.parquet": cls.event_bytes(
                records
            )
            for record_type, records in sorted(
                groups.items(), key=lambda item: _class_name(item[0])
            )
        }

    @classmethod
    def artifacts(cls, values: Mapping[str, object]) -> tuple[GoldenArtifact, ...]:
        """Canonicalize named bytes, event streams, or structured values."""
        artifacts: list[GoldenArtifact] = []
        for name, value in values.items():
            normalized_name = _validate_artifact_name(name)
            if isinstance(value, bytes):
                payload = value
            elif isinstance(value, bytearray):
                payload = bytes(value)
            elif isinstance(value, Event):
                payload = cls.event_bytes((value,))
            else:
                payload = cls.json_bytes(value)
            artifacts.append(GoldenArtifact(normalized_name, payload))
        return tuple(sorted(artifacts, key=lambda artifact: artifact.name))

    @staticmethod
    def manifest(
        configuration: object, artifacts: Iterable[GoldenArtifact]
    ) -> GoldenManifest:
        """Build a manifest from canonical artifact bytes and config inputs."""
        values = tuple(artifacts)
        if not all(isinstance(artifact, GoldenArtifact) for artifact in values):
            raise TypeError("artifacts must contain GoldenArtifact values")
        return GoldenManifest(
            configuration_hash=configuration_hash(_normalized(configuration)),
            artifacts=tuple(
                ArtifactDigest(artifact.name, artifact.sha256, artifact.byte_length)
                for artifact in sorted(values, key=lambda item: item.name)
            ),
        )


@dataclass(frozen=True, slots=True)
class GoldenManifest:
    """Canonical manifest containing configuration and artifact digests."""

    configuration_hash: str
    artifacts: tuple[ArtifactDigest, ...] | Sequence[ArtifactDigest]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.configuration_hash, str)
            or len(self.configuration_hash) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.configuration_hash
            )
        ):
            raise ValueError("configuration_hash must be a lowercase SHA-256 digest")
        artifacts = tuple(self.artifacts)
        if not all(isinstance(artifact, ArtifactDigest) for artifact in artifacts):
            raise TypeError("artifacts must contain ArtifactDigest values")
        if tuple(sorted(artifact.name for artifact in artifacts)) != tuple(
            artifact.name for artifact in artifacts
        ):
            raise ValueError("manifest artifacts must be sorted by name")
        if len({artifact.name for artifact in artifacts}) != len(artifacts):
            raise ValueError("manifest artifact names must be unique")
        object.__setattr__(self, "artifacts", artifacts)

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_configuration_bytes(
            {
                "schema": "fca-golden-manifest-v1",
                "configuration_hash": self.configuration_hash,
                "artifacts": [
                    {
                        "name": artifact.name,
                        "sha256": artifact.sha256,
                        "byte_length": artifact.byte_length,
                    }
                    for artifact in self.artifacts
                ],
            }
        )

    @property
    def sha256(self) -> str:
        return _sha256(self.canonical_bytes)

    @property
    def artifact_hashes(self) -> tuple[tuple[str, str], ...]:
        return tuple((artifact.name, artifact.sha256) for artifact in self.artifacts)


@dataclass(frozen=True, slots=True)
class GoldenRun:
    """Inspectable output of one isolated scenario execution."""

    manifest: GoldenManifest
    artifacts: tuple[GoldenArtifact, ...]
    dispatch_hash: str
    dispatched_count: int
    final_clock: datetime
    strategy: Strategy
    binding: StrategyBinding

    @property
    def manifest_hash(self) -> str:
        return self.manifest.sha256

    @property
    def artifact_hashes(self) -> tuple[tuple[str, str], ...]:
        return self.manifest.artifact_hashes

    def artifact(self, name: str) -> GoldenArtifact:
        normalized_name = _validate_artifact_name(name)
        for artifact in self.artifacts:
            if artifact.name == normalized_name:
                return artifact
        raise KeyError(normalized_name)


class DeterminismError(AssertionError):
    """Raised when two golden runs do not have identical canonical output."""


@dataclass(frozen=True, slots=True)
class ManifestComparison:
    """Detailed result of comparing two manifests or two complete runs."""

    differences: tuple[str, ...]

    @property
    def equal(self) -> bool:
        return not self.differences

    @property
    def matches(self) -> bool:
        return self.equal


def _manifest(value: GoldenManifest | GoldenRun) -> GoldenManifest:
    if isinstance(value, GoldenRun):
        return value.manifest
    if isinstance(value, GoldenManifest):
        return value
    raise TypeError("comparison value must be a GoldenManifest or GoldenRun")


def compare_manifests(
    left: GoldenManifest | GoldenRun, right: GoldenManifest | GoldenRun
) -> ManifestComparison:
    """Compare config and artifact hashes without relying on filesystem paths."""
    left_manifest = _manifest(left)
    right_manifest = _manifest(right)
    differences: list[str] = []
    if left_manifest.configuration_hash != right_manifest.configuration_hash:
        differences.append("configuration_hash")
    left_artifacts = {artifact.name: artifact for artifact in left_manifest.artifacts}
    right_artifacts = {artifact.name: artifact for artifact in right_manifest.artifacts}
    for name in sorted(set(left_artifacts) | set(right_artifacts)):
        left_artifact = left_artifacts.get(name)
        right_artifact = right_artifacts.get(name)
        if left_artifact is None:
            differences.append(f"artifact:{name}:missing-left")
        elif right_artifact is None:
            differences.append(f"artifact:{name}:missing-right")
        elif left_artifact.sha256 != right_artifact.sha256:
            differences.append(f"artifact:{name}:sha256")
        elif left_artifact.byte_length != right_artifact.byte_length:
            differences.append(f"artifact:{name}:byte_length")
    return ManifestComparison(tuple(differences))


def assert_manifest_equal(
    left: GoldenManifest | GoldenRun, right: GoldenManifest | GoldenRun
) -> None:
    """Raise ``DeterminismError`` when manifest hashes or entries differ."""
    comparison = compare_manifests(left, right)
    if not comparison.equal:
        raise DeterminismError(
            "golden manifests differ: " + ", ".join(comparison.differences)
        )


def assert_same_artifacts(left: GoldenRun, right: GoldenRun) -> None:
    """Assert both digest equality and byte equality for every artifact."""
    assert_manifest_equal(left, right)
    left_artifacts = {artifact.name: artifact for artifact in left.artifacts}
    right_artifacts = {artifact.name: artifact for artifact in right.artifacts}
    for name in sorted(left_artifacts):
        if left_artifacts[name].payload != right_artifacts[name].payload:
            raise DeterminismError(f"golden artifact bytes differ: {name}")


def run_scenario(scenario: GoldenScenario) -> GoldenRun:
    """Execute one scenario using only explicit deterministic dependencies."""
    if not isinstance(scenario, GoldenScenario):
        raise TypeError("scenario must be a GoldenScenario")
    start = scenario.clock_start
    assert start is not None
    clock = SimClock(start)
    bus = EventBus(clock)
    portfolio = PortfolioHub(scenario.account_id)
    portfolio.create_book(scenario.book_id, scenario.initial_cash)
    venue = ScriptedVenue(bus, scenario.venue_steps)
    context = StrategyContext(
        clock,
        portfolio=portfolio,
        venue=venue,
        bus=bus,
        seed=scenario.seed,
        universe=scenario.universe,
        subscriptions=scenario.subscriptions,
        book_id=scenario.book_id,
    )
    binding = bind_strategy(
        scenario.strategy_class,
        scenario.strategy_parameters,
        context,
    )
    binding.start()
    scenario.feed.publish(bus)
    dispatched = bus.drain()
    binding.stop()
    venue.assert_exhausted()

    strategy = binding.strategy
    strategy_values = {
        "class": _class_name(strategy),
        "callbacks": getattr(strategy, "callbacks", ()),
        "event_ids": [event.event_id for event in getattr(strategy, "events", ())],
        "submitted_order_ids": context.orders.submitted_order_ids,
        "emissions": getattr(context.emissions, "records", ()),
        "logs": getattr(context.log, "records", ()),
    }
    artifacts: dict[str, bytes] = {}
    artifacts.update(ArtifactCanonicalizer.event_artifacts("feed", scenario.feed))
    artifacts.update(ArtifactCanonicalizer.event_artifacts("dispatch", dispatched))
    artifacts["strategy/output.json"] = ArtifactCanonicalizer.json_bytes(
        strategy_values
    )
    if strategy_orders := getattr(strategy, "orders", ()):
        artifacts["strategy/orders.parquet"] = ArtifactCanonicalizer.event_bytes(
            strategy_orders
        )
    artifacts["portfolio/snapshot.json"] = ArtifactCanonicalizer.json_bytes(
        portfolio.snapshot
    )
    artifacts["venue/calls.json"] = ArtifactCanonicalizer.json_bytes(venue.calls)
    artifact_values = ArtifactCanonicalizer.artifacts(artifacts)
    manifest = ArtifactCanonicalizer.manifest(
        scenario.configuration_for_hash,
        artifact_values,
    )
    return GoldenRun(
        manifest=manifest,
        artifacts=artifact_values,
        dispatch_hash=bus.dispatch_hash,
        dispatched_count=bus.dispatched_count,
        final_clock=clock.head,
        strategy=strategy,
        binding=binding,
    )


def run_twice(scenario: GoldenScenario) -> tuple[GoldenRun, GoldenRun]:
    """Run a scenario twice, constructing an independent runtime each time."""
    return run_scenario(scenario), run_scenario(scenario)


def assert_reproducible(scenario: GoldenScenario) -> tuple[GoldenRun, GoldenRun]:
    """Run twice and assert byte-identical artifacts and identical manifests."""
    first, second = run_twice(scenario)
    assert_same_artifacts(first, second)
    if first.dispatch_hash != second.dispatch_hash:
        raise DeterminismError("golden dispatch hashes differ")
    return first, second


__all__ = [
    "ArtifactCanonicalizer",
    "ArtifactDigest",
    "DeterminismError",
    "GoldenArtifact",
    "GoldenManifest",
    "GoldenRun",
    "GoldenScenario",
    "ManifestComparison",
    "SyntheticFeed",
    "ToyStrategy",
    "assert_manifest_equal",
    "assert_reproducible",
    "assert_same_artifacts",
    "compare_manifests",
    "run_scenario",
    "run_twice",
]
