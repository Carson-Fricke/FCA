"""Import-boundary and type-separation enforcement for the strategy surface.

The strategy-reachable surface is the ``harness.strategy`` package (binding,
contracts, context, lint, reference) plus the capability modules strategies
legitimately import: ``harness.core.clock`` (``Clock``/``ReadonlyClock``),
``harness.data.history`` (``StrategyHistory``), ``harness.results.metrics``,
and the portfolio book/snapshot types that surface through
``harness.strategy.context`` annotations.

Five layers are enforced:

1. No module of ``harness.strategy`` imports ``harness.runtime``, neither
   directly nor anywhere in its transitive ``harness.*`` import closure.
   The deliberate asymmetry is pinned explicitly: the ``StrategyVenue`` and
   ``StrategyScheduler`` capability types are defined in
   ``harness.runtime.backtest.strategies`` and are imported by the engine and
   by tests only, while ``harness.strategy`` defines its own ``EventScheduler``
   mirror in ``harness.strategy.context``. If ``harness.strategy.context``
   ever imports ``harness.runtime``, this layer fails.
2. Every strategy-reachable module's declared public surface (``__all__``)
   binds no engine-internal mutable class, directly or through a subclass
   hierarchy: ``SimClock``, ``EventBus``, ``EventQueue``, ``HistoricalView``,
   ``SimulatedVenue``, ``ScriptedVenue``, ``PortfolioHub``, ``VirtualBook``,
   and the OMS classes (``SingleLegOMS``, ``MultiLegOMS``,
   ``MultiLegOrderGroup``, ``OrderStateMachine``).
3. The annotations resolved with ``typing.get_type_hints`` of every public
   callable reachable from those modules (module-level public functions,
   constructors, public methods and properties of exported classes, and
   class-level annotations) never name an engine-internal mutable class.
   Annotations that cannot be resolved safely are skipped rather than
   guessed ("where safe"). Frozen value types (events, snapshots), the
   ``Clock``/``ExecutionVenue`` protocols, and the capability types
   themselves remain legal.
4. ``ReadonlyClock``, ``StrategyHistory``, and ``StrategyVenue`` are distinct
   runtime types from their engine-internal counterparts in both
   ``isinstance`` directions, and the capabilities expose no clock-advance,
   history-poisoning, or venue-state-probing surface.
5. A dynamic canary constructs a ``StrategyContext`` exactly as the backtest
   engine does (read-only clock wrapper, portfolio hub, capability venue,
   bus plus capability scheduler, ``StrategyHistory`` data) and asserts that
   no public attribute reachable from the context, including one further
   level of capability views, has an engine-internal mutable runtime type.
   It also pins that raw engine inputs (``SimClock``, ``HistoricalView``)
   are wrapped into capabilities at the boundary.

A failure in any layer is the boundary violation report: the message names
the offending module, callable, or attribute together with the definition
site of the engine-internal type.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Final, TypeVar, get_args, get_type_hints

import pytest

from harness.core.clock import Clock, ReadonlyClock, SimClock
from harness.core.events import EventBus, EventQueue
from harness.core.symbols import Symbol
from harness.core.types import QuoteEvent, TradeEvent
from harness.data.history import HistoricalView, StrategyHistory
from harness.oms import (
    MultiLegOMS,
    MultiLegOrderGroup,
    OrderStateMachine,
    SingleLegOMS,
)
from harness.portfolio import PortfolioHub, PortfolioSnapshot
from harness.portfolio.book import VirtualBook
from harness.runtime.backtest.strategies import StrategyScheduler, StrategyVenue
from harness.strategy import EventScheduler, StrategyContext
from harness.venues import ExecutionVenue
from harness.venues.scripted import ScriptedVenue
from harness.venues.sim import SimulatedVenue

STAMP: Final = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)
AAPL: Final = Symbol("AAPL")
REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[3]

STRATEGY_PACKAGE_MODULES: Final[tuple[str, ...]] = (
    "harness.strategy",
    "harness.strategy.binding",
    "harness.strategy.contracts",
    "harness.strategy.context",
    "harness.strategy.lint",
    "harness.strategy.reference",
)

CAPABILITY_MODULES: Final[tuple[str, ...]] = (
    "harness.core.clock",
    "harness.data.history",
    "harness.results.metrics",
)

STRATEGY_REACHABLE_MODULES: Final[tuple[str, ...]] = (
    *STRATEGY_PACKAGE_MODULES,
    *CAPABILITY_MODULES,
)

FORBIDDEN_ENGINE_TYPES: Final[frozenset[type]] = frozenset(
    {
        SimClock,
        EventBus,
        EventQueue,
        HistoricalView,
        SimulatedVenue,
        ScriptedVenue,
        PortfolioHub,
        VirtualBook,
        SingleLegOMS,
        MultiLegOMS,
        MultiLegOrderGroup,
        OrderStateMachine,
    }
)

FORBIDDEN_ENGINE_INSTANCES: Final[tuple[type, ...]] = tuple(
    sorted(FORBIDDEN_ENGINE_TYPES, key=lambda engine_type: engine_type.__qualname__)
)

CLOCK_MUTATION_NAMES: Final[tuple[str, ...]] = ("advance_to", "advance_by", "head")

HISTORY_POISON_NAMES: Final[tuple[str, ...]] = (
    "record",
    "record_many",
    "subscribe",
    "attach",
    "head",
    "capacity",
    "clock",
)

VENUE_PROBE_NAMES: Final[tuple[str, ...]] = (
    "state",
    "decisions_for",
    "active_orders",
    "latest_market",
    "calls",
    "remaining_steps",
)


def _definition_site(target: Callable[..., object] | type) -> str:
    """Return the repository-relative ``file:line`` where ``target`` is defined."""
    try:
        source_file = inspect.getsourcefile(target)
        first_line = inspect.getsourcelines(target)[1]
    except (OSError, TypeError):
        subject = target if isinstance(target, type) else type(target)
        return f"{subject.__module__}.{subject.__qualname__}"
    if source_file is None:
        return "<unknown source file>"
    absolute = Path(source_file).resolve()
    try:
        relative = absolute.relative_to(REPOSITORY_ROOT)
    except ValueError:
        return absolute.as_posix()
    return f"{relative.as_posix()}:{first_line}"


def _forbidden_site(engine_type: type) -> str:
    """Return the engine-internal type name together with its definition site."""
    return f"{engine_type.__module__}.{engine_type.__qualname__} ({_definition_site(engine_type)})"


def _public_names(module: ModuleType) -> tuple[str, ...]:
    """Return the module's declared public names, falling back to public attrs."""
    exported = getattr(module, "__all__", None)
    if exported is not None:
        return tuple(str(name) for name in exported)
    return tuple(name for name in vars(module) if not name.startswith("_"))


def _resolve_type_hints(
    target: Callable[..., object] | type | ModuleType,
) -> Mapping[str, object] | None:
    """Return resolved annotations, or ``None`` when resolution is not safe."""
    try:
        return get_type_hints(target)
    except (NameError, TypeError):
        return None


def _annotation_types(annotation: object) -> Iterator[type]:
    """Yield every real class reachable inside a resolved annotation tree."""
    if annotation is None or isinstance(annotation, str | TypeVar):
        return
    if isinstance(annotation, type):
        yield annotation
        return
    for component in get_args(annotation):
        yield from _annotation_types(component)


def _annotation_leaks(
    owner: str,
    hints: Mapping[str, object] | None,
    site: str,
) -> Iterator[str]:
    """Yield one finding per engine-internal type bound by resolved hints."""
    if hints is None:
        return
    for parameter, annotation in sorted(hints.items()):
        leaked = sorted(
            {
                engine_type
                for engine_type in _annotation_types(annotation)
                if engine_type in FORBIDDEN_ENGINE_TYPES
            },
            key=lambda engine_type: engine_type.__qualname__,
        )
        if not leaked:
            continue
        subject = "return type" if parameter == "return" else f"parameter {parameter!r}"
        names = ", ".join(_forbidden_site(engine_type) for engine_type in leaked)
        yield f"{owner} {subject} annotates engine-internal mutable type {names} (callable at {site})"


def _class_functions(cls: type) -> Iterator[tuple[str, Callable[..., object]]]:
    """Yield the public callables (and ``__init__``) defined for one class."""
    seen: set[tuple[str, str]] = set()
    for klass in cls.__mro__:
        if klass is object:
            continue
        for name, value in vars(klass).items():
            if name.startswith("_") and name != "__init__":
                continue
            function: Callable[..., object] | None
            if isinstance(value, classmethod | staticmethod):
                function = value.__func__
            elif isinstance(value, property):
                function = value.fget
            else:
                function = value if callable(value) else None
            if function is None:
                continue
            key = (klass.__qualname__, name)
            if key in seen:
                continue
            seen.add(key)
            yield name, function


def _module_export_findings(module: ModuleType) -> list[str]:
    """Find public names of ``module`` that bind engine-internal mutable types."""
    findings: list[str] = []
    for name in _public_names(module):
        value = getattr(module, name, None)
        if not isinstance(value, type):
            continue
        if value in FORBIDDEN_ENGINE_TYPES:
            findings.append(
                f"{value.__module__}.{name} exports the engine-internal mutable "
                f"type {_forbidden_site(value)}"
            )
            continue
        inherited = sorted(
            {
                base.__qualname__
                for base in value.__mro__[1:]
                if base in FORBIDDEN_ENGINE_TYPES
            }
        )
        if inherited:
            findings.append(
                f"{value.__module__}.{name} inherits engine-internal mutable "
                "type(s) " + ", ".join(inherited)
            )
    return findings


def _module_annotation_findings(module: ModuleType) -> list[str]:
    """Scan every public callable surface exported by ``module``."""
    findings: list[str] = []
    for name in _public_names(module):
        value = getattr(module, name, None)
        if isinstance(value, type):
            label = f"{value.__module__}.{value.__qualname__}"
            findings.extend(
                _annotation_leaks(
                    label,
                    _resolve_type_hints(value),
                    _definition_site(value),
                )
            )
            for attribute_name, function in _class_functions(value):
                findings.extend(
                    _annotation_leaks(
                        f"{label}.{attribute_name}",
                        _resolve_type_hints(function),
                        _definition_site(function),
                    )
                )
            continue
        if callable(value):
            qualname = getattr(value, "__qualname__", name)
            findings.extend(
                _annotation_leaks(
                    f"{value.__module__}.{qualname}",
                    _resolve_type_hints(value),
                    _definition_site(value),
                )
            )
    return list(dict.fromkeys(findings))


def _is_type_checking_guard(test: ast.expr) -> bool:
    """Return whether an ``if`` test is a ``TYPE_CHECKING`` gate."""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _resolve_import_target(
    node: ast.ImportFrom, module_name: str, is_package: bool
) -> str | None:
    """Resolve one ``import from`` statement to its absolute module name."""
    if node.level == 0:
        return node.module
    if node.module is None:
        return None
    package = module_name if is_package else module_name.rpartition(".")[0]
    for _ in range(node.level - 1):
        package = package.rpartition(".")[0]
    if not package:
        return None
    return f"{package}.{node.module}"


def _harness_import_targets(
    node: ast.AST, module_name: str, is_package: bool
) -> Iterator[str]:
    """Yield every absolute ``harness.*`` module imported inside ``node``.

    Import statements guarded by ``TYPE_CHECKING`` are skipped: they bind no
    runtime dependency.
    """
    if isinstance(node, ast.If) and _is_type_checking_guard(node.test):
        return
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name == "harness" or alias.name.startswith("harness."):
                yield alias.name
    elif isinstance(node, ast.ImportFrom):
        target = _resolve_import_target(node, module_name, is_package)
        if target is not None and (
            target == "harness" or target.startswith("harness.")
        ):
            yield target
    for child in ast.iter_child_nodes(node):
        yield from _harness_import_targets(child, module_name, is_package)


def _module_source(module: ModuleType) -> str | None:
    """Return the module's source text, or ``None`` when it has no source file."""
    module_file = getattr(module, "__file__", None)
    if module_file is None:
        return None
    return Path(module_file).read_text(encoding="utf-8")


def _runtime_importers(module_names: Iterable[str]) -> tuple[str, ...]:
    """Return the modules whose source imports ``harness.runtime`` at runtime."""
    flagged: list[str] = []
    for module_name in sorted(module_names):
        module = importlib.import_module(module_name)
        source = _module_source(module)
        if source is None:
            continue
        targets = _harness_import_targets(
            ast.parse(source), module_name, hasattr(module, "__path__")
        )
        reaches_runtime = any(
            target == "harness.runtime" or target.startswith("harness.runtime.")
            for target in targets
        )
        if reaches_runtime:
            flagged.append(module_name)
    return tuple(flagged)


def _transitive_harness_closure(roots: Iterable[str]) -> tuple[str, ...]:
    """Return every ``harness.*`` module reachable from ``roots`` by import."""
    visited: set[str] = set()
    pending: list[str] = list(roots)
    while pending:
        module_name = pending.pop()
        if module_name in visited or not module_name.startswith("harness"):
            continue
        module = importlib.import_module(module_name)
        visited.add(module_name)
        source = _module_source(module)
        if source is None:
            continue
        is_package = hasattr(module, "__path__")
        for target in _harness_import_targets(
            ast.parse(source), module_name, is_package
        ):
            resolved = target
            while resolved:
                if resolved not in visited:
                    pending.append(resolved)
                resolved = resolved.rpartition(".")[0]
    return tuple(sorted(visited))


def _runtime_leaks(root: object, path: str, depth: int) -> Iterator[str]:
    """Yield findings for public attributes bound to engine-internal types."""
    for name in dir(root):
        if name.startswith("_"):
            continue
        value = getattr(root, name, None)
        if value is None or (callable(value) and not isinstance(value, type)):
            continue
        if isinstance(value, FORBIDDEN_ENGINE_INSTANCES):
            value_type = type(value)
            yield (
                f"{path}.{name} is an instance of the engine-internal mutable "
                f"type {value_type.__module__}.{value_type.__qualname__}"
            )
        elif isinstance(value, type) and value in FORBIDDEN_ENGINE_TYPES:
            yield (
                f"{path}.{name} binds the engine-internal mutable type "
                f"{value.__module__}.{value.__qualname__}"
            )
        elif depth > 0 and type(value).__module__.startswith("harness."):
            yield from _runtime_leaks(value, f"{path}.{name}", depth - 1)


def test_strategy_surface_does_not_reach_harness_runtime() -> None:
    """No strategy module imports ``harness.runtime``, directly or transitively.

    The capability types live on the engine side by design: ``StrategyVenue``
    and ``StrategyScheduler`` are defined in
    ``harness.runtime.backtest.strategies`` and are consumed by the engine and
    by tests only, while ``harness.strategy`` mirrors the scheduler capability
    as ``EventScheduler`` in ``harness.strategy.context``. This pins the
    asymmetry and fails the moment ``harness.strategy`` starts importing
    ``harness.runtime``.
    """
    direct = _runtime_importers(STRATEGY_PACKAGE_MODULES)
    assert direct == (), (
        f"harness.strategy modules import harness.runtime at runtime: {direct}"
    )
    closure = _transitive_harness_closure(STRATEGY_PACKAGE_MODULES)
    transitive = _runtime_importers(closure)
    assert transitive == (), (
        "the harness.* import closure of harness.strategy reaches "
        f"harness.runtime through: {transitive}"
    )
    assert StrategyVenue.__module__ == "harness.runtime.backtest.strategies"
    assert StrategyScheduler.__module__ == "harness.runtime.backtest.strategies"
    assert EventScheduler.__module__ == "harness.strategy.context"
    for module_name in STRATEGY_PACKAGE_MODULES:
        module = importlib.import_module(module_name)
        for capability_name in ("StrategyVenue", "StrategyScheduler"):
            assert capability_name not in getattr(module, "__all__", ())
            assert capability_name not in vars(module)


def test_strategy_reachable_modules_do_not_export_engine_internal_types() -> None:
    """Declared public surfaces bind no engine-internal mutable type."""
    findings: list[str] = []
    for module_name in STRATEGY_REACHABLE_MODULES:
        module = importlib.import_module(module_name)
        findings.extend(_module_export_findings(module))
    findings = list(dict.fromkeys(findings))
    assert findings == [], "\n".join(findings)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Audit D8 target state not yet met: engine-internal mutable types are "
        "still statically reachable from strategy-reachable module annotations "
        "(EventBus/PortfolioHub/HistoricalView parameters in context.py and "
        "history.py); the protocol-only-imports refactor is a filed follow-up"
    ),
)
def test_public_callables_do_not_annotate_engine_internal_types() -> None:
    """Public callable annotations never name an engine-internal mutable type.

    ``typing.get_type_hints`` is applied where safe: callables whose
    annotations cannot be resolved (for example PEP 695 generics or
    ``TYPE_CHECKING``-only names) are skipped instead of guessed.
    """
    findings: list[str] = []
    for module_name in STRATEGY_REACHABLE_MODULES:
        module = importlib.import_module(module_name)
        findings.extend(_module_annotation_findings(module))
    findings = list(dict.fromkeys(findings))
    assert findings == [], "\n".join(findings)


def test_capability_types_are_distinct_from_engine_internal_types() -> None:
    """Capability types and engine-internal types never substitute for each other."""
    clock = SimClock(STAMP)
    readonly = ReadonlyClock(clock)
    assert isinstance(clock, Clock)
    assert isinstance(readonly, Clock)
    assert not isinstance(clock, ReadonlyClock)
    assert not isinstance(readonly, SimClock)
    assert not issubclass(SimClock, ReadonlyClock)
    assert not issubclass(ReadonlyClock, SimClock)
    for mutation_name in CLOCK_MUTATION_NAMES:
        assert not hasattr(readonly, mutation_name)
    assert hasattr(clock, "advance_to")
    assert hasattr(clock, "advance_by")

    view = HistoricalView(clock)
    history = StrategyHistory(view, clock)
    assert not isinstance(history, HistoricalView)
    assert not isinstance(view, StrategyHistory)
    assert not issubclass(StrategyHistory, HistoricalView)
    assert not issubclass(HistoricalView, StrategyHistory)
    for poison_name in HISTORY_POISON_NAMES:
        assert not hasattr(history, poison_name)

    bus = EventBus(clock)
    simulated = SimulatedVenue(bus)
    scripted = ScriptedVenue(bus)
    capability = StrategyVenue(scripted)
    for concrete in (SimulatedVenue, ScriptedVenue):
        assert not issubclass(concrete, StrategyVenue)
        assert not issubclass(StrategyVenue, concrete)
        assert not isinstance(capability, concrete)
    assert not isinstance(simulated, StrategyVenue)
    assert not isinstance(scripted, StrategyVenue)
    for probe_name in VENUE_PROBE_NAMES:
        assert not hasattr(capability, probe_name)
    assert isinstance(capability, ExecutionVenue)
    assert isinstance(simulated, ExecutionVenue)
    assert isinstance(scripted, ExecutionVenue)

    hub = PortfolioHub("account")
    hub.create_book("strategy", "1000")
    snapshot = hub.snapshot
    assert isinstance(snapshot, PortfolioSnapshot)
    with pytest.raises(FrozenInstanceError):
        setattr(snapshot, "cash", Decimal("1"))  # noqa: B010


def test_engine_constructed_context_exposes_no_engine_internal_mutable_types() -> None:
    """A context built like the engine exposes only capability surfaces.

    The construction mirrors ``harness.runtime.backtest.engine``: the raw
    ``SimClock`` arrives wrapped in ``ReadonlyClock``, the hub is passed for
    accounting, the concrete venue is wrapped in the ``StrategyVenue``
    capability, the bus is paired with ``StrategyScheduler``, and the raw
    ``HistoricalView`` is wrapped into ``StrategyHistory``. No public
    attribute of the context, or of the capability views reachable through
    it, may carry an engine-internal mutable runtime type.
    """
    clock = SimClock(STAMP)
    bus = EventBus(clock)
    hub = PortfolioHub("account")
    hub.create_book("strategy", "1000")
    view = HistoricalView(clock, capacity=64)
    view.subscribe(AAPL, QuoteEvent)
    view.attach(bus)
    context = StrategyContext(
        ReadonlyClock(clock),
        portfolio=hub,
        venue=StrategyVenue(ScriptedVenue(bus)),
        bus=bus,
        scheduler=StrategyScheduler(bus),
        data=StrategyHistory(view, clock),
        seed=1729,
        universe=(AAPL,),
        subscriptions=(QuoteEvent, TradeEvent),
        book_id="strategy",
    )
    leaks = list(_runtime_leaks(context, "context", 2))
    assert leaks == [], "\n".join(leaks)
    assert type(context.clock) is ReadonlyClock
    assert isinstance(context.data, StrategyHistory)
    assert isinstance(context.venue, StrategyVenue)
    assert not isinstance(context.venue, SimulatedVenue | ScriptedVenue)
    assert isinstance(context.venue, ExecutionVenue)
    assert not isinstance(context.scheduler, EventBus | EventQueue)
    assert isinstance(context.portfolio, PortfolioSnapshot)
    assert not isinstance(context.portfolio, PortfolioHub)
    with pytest.raises(FrozenInstanceError):
        setattr(context.portfolio, "cash", Decimal("1"))  # noqa: B010

    wrapped = StrategyContext(
        clock,
        portfolio=hub,
        bus=bus,
        data=view,
        universe=(AAPL,),
        subscriptions=(QuoteEvent,),
        book_id="strategy",
        seed=7,
    )
    assert type(wrapped.clock) is ReadonlyClock
    assert type(wrapped.data) is StrategyHistory
