# Strategy Authoring and Architecture Guide

Status: retired. This guide describes the embedded-Python authoring surface
(`harness.strategy`, `module:Class` implementations, and the Python sandbox
lint), which was deleted in the native-authoring migration. The authoring
contract is now Rust against the `harness-strategy-sdk` crate with JIT
compilation through `harness-strategy-jit`; see
`.github/architecture/rust-native-authoring-plan.md` for the current plan
and `.github/architecture/repository.md` (`Program Authoring Contract`) for
the implemented contract. This document is retained only as historical
reference until the Rust authoring guide replaces it.

The text below describes the retired Python surface and no longer matches
the repository.

---

The guide below was audited against the former `docs/hardening`
tree and described the strategy contract exactly as the embedded-Python host
implemented it. It is written for an independent author who wants to add a
strategy that passes CI and runs in the harness without changing engine or
harness code.

The single rule that makes that independence possible: a strategy is a plain
Python class that imports only the public plugin surface
(`harness.strategy`, `harness.core.types`, `harness.core.symbols`) and reaches
the engine only through the constrained context object it receives. It never
imports an engine module, never reads a wall clock, never draws unseeded
randomness, and never opens a socket.

## 1. The contract at a glance

| Piece | Implementation | Where |
| --- | --- | --- |
| Base class | `harness.strategy.contracts.Strategy` | `src/harness/strategy/contracts.py:127` |
| Callback order constant | `CALLBACK_ORDER` (11 names) | `src/harness/strategy/contracts.py:35` |
| Context protocol | `harness.strategy.contracts.StrategyContext` | `src/harness/strategy/contracts.py:50` |
| Sandbox lint | `harness.strategy.lint` (AST rules) | `src/harness/strategy/lint.py:277` |
| Reference fixture | `harness.strategy.reference.ReferenceStrategy` | `src/harness/strategy/reference.py:21` |
| Loader | config `implementation: module:Class` | `src/harness/host/src/backtest.rs:44` |
| Native context | `PyEngineContext` (PyO3) | `src/harness/host/src/context.rs:568` |
| Native runtime trait | `StrategyRuntime` | `src/harness/backtest/src/backtest/engine_core.rs:799` |
| Example strategies | `strategies/harness_examples/*` | shipped |
| Config entry | `strategy_instances[]` | `config/harness/backtest.yaml:19` |

The plugin surface is deliberately protocol-only:

- `harness.strategy` must not import any deleted engine module. This boundary is
  pinned by `tests/harness/strategy/test_plugin_surface.py`.
- The native host hands the strategy a context over PyO3, so the same class runs
  unmodified against the Rust engine. The reference strategy records every
  callback to pin the documented order.

## 2. Loading and instantiation

A strategy instance is named in configuration as `module:Class`, for example:

```yaml
strategy_instances:
  - instance_id: spy-momentum
    implementation: strategies.harness_examples.intraday_momentum:IntradayMomentumStrategy
    bundle_hash: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    capital_fraction: 1.0
    symbols: [SPY]
    parameters:
      lookback: 4
      threshold: "0.05"
```

Loading works as follows:

1. The host splits `implementation` on the last `:` into `(module, class)`
   (`src/harness/host/src/backtest.rs:48`). A path without `:` is rejected with
   "strategy implementation must use module:Class syntax".
2. The host prepends the source-checkout search paths to `sys.path`: the
   repository root, `<root>/src`, and `<root>/.venv/Lib/site-packages`
   (`src/harness/host/src/python.rs:87`). Because the repository root is on the
   path, `strategies.*` resolves without installation from a source checkout.
3. The class is imported and constructed with the `parameters` mapping as
   keyword arguments. If that call raises, the host retries once passing the
   mapping as `parameters=<dict>` (`src/harness/host/src/backtest.rs:104`; the
   same fallback exists in `src/harness/host/src/strategy.rs:115`). A
   configuration with parameters therefore needs either explicit constructor
   keywords or a `**parameters`/`parameters` accepted argument.
4. `on_start(context)` is invoked once with the native context
   (`src/harness/host/src/backtest.rs:114`).
5. `on_stop(context)` is invoked once at the end
   (`src/harness/host/src/backtest.rs:206`).

Only the constructor and the callbacks are part of the contract; the host never
inspects strategy attributes.

## 3. Lifecycle and which callbacks actually dispatch

The documented order is fixed (`src/harness/strategy/contracts.py:35`,
mirrored in Rust at `src/harness/host/src/contracts.rs:12`):

```text
on_start, on_quote, on_trade, on_order, on_execution_report, on_fill,
on_position, on_book, on_account, on_corporate_action, on_stop
```

Subclassing `Strategy` gives every callback a no-op default, so an author
implements only the events they need. The v1 native backtest adapter, however,
dispatches a subset. Know the difference before you rely on a hook:

| Callback | Declared | Dispatched by the native backtest adapter | Notes |
| --- | --- | --- | --- |
| `on_start(context)` | yes | yes | once, before the first event |
| `on_quote(QuoteEvent)` | yes | yes | `src/harness/host/src/backtest.rs:157` |
| `on_trade(TradeEvent)` | yes | yes | `src/harness/host/src/backtest.rs:157` |
| `on_fill(Fill)` | yes | yes | after portfolio mutation (`:190`) |
| `on_stop(context)` | yes | yes | once, after the last event |
| `on_order` | yes | no | reserved |
| `on_execution_report` | yes | no | reserved |
| `on_position` | yes | no | reserved |
| `on_book` | yes | no | reserved |
| `on_account` | yes | no | reserved |
| `on_corporate_action` | yes | no | engine-delivered in the contract docstring; not yet wired natively |

The Rust `StrategyBinding` can route every event kind
(`src/harness/host/src/binding.rs:106`), but the backtest `StrategyRuntime`
trait exposes only `on_start`, `on_market`, `on_fill`, and `on_stop`
(`src/harness/backtest/src/backtest/engine_core.rs:799`). Do not build a
strategy that depends on the reserved hooks until they are dispatched.

## 4. Event payloads

Market events are constructed by the host from the native engine and handed to
the callback as the real `harness.core.types` record, with every price and
quantity an exact `decimal.Decimal`:

- `QuoteEvent` — `event_id`, `timestamp`, `sequence`, `symbol` (a `Symbol`),
  `bid_price`, `bid_quantity`, `ask_price`, `ask_quantity`, `venue`
  (`src/harness/core/types/events.py:43`).
- `TradeEvent` — `event_id`, `timestamp`, `sequence`, `symbol`, `price`,
  `quantity`, `venue`, `trade_id` (`src/harness/core/types/events.py:78`).
- `Fill` — `event_id`, `timestamp`, `sequence`, `fill_id`, `order_id`, `symbol`,
  `side`, `quantity`, `price`, `fee`, `liquidity`
  (`src/harness/core/types/accounting.py:81`).

The host sets `event_id`, `sequence`, `symbol`, prices, and quantities from the
native event (`src/harness/host/src/backtest.rs:219`,
`src/harness/host/src/backtest.rs:274`). `context.clock` is advanced to the
event timestamp before the callback runs (`:151`), and strategy books are
marked to the event price before dispatch (`:152`). `on_fill` runs only after
the fill has been applied to the strategy book (`:182`), so `on_fill` sees the
already-updated position.

## 5. The context capabilities

`on_start` and `on_stop` receive the native `PyEngineContext`
(`src/harness/host/src/context.rs:568`). These are the capabilities the v1
backtest adapter actually supplies:

| Attribute | Type | What it gives a strategy |
| --- | --- | --- |
| `book_id` | `str` | The strategy's virtual book identifier |
| `clock.now()` | `datetime` | The engine head as a timezone-aware UTC `datetime` |
| `universe` | object | `.symbols` (list of `str`), `.contains(sym)`, `in`, `len()` |
| `books` | object | Book/account view (see below) |
| `orders` | object | `submit`, `cancel`, `replace`, `owns` |
| `state` | mapping | Deterministic strategy-local mutable store |
| `random` | `random.Random` | Per-context seeded generator; alias `rng` |
| `log` | object | `debug`/`info`/`warning`/`error` and `.records`; alias `logger` |
| `emissions` | object | Structured emission sink; `.records` |
| `emit(name, fields=None, **values)` | method | Emit one structured signal |

`books` exposes `book_id`, `book_ids`, `cash`, `equity`, `fill_count`,
`initial_cash`, `current`, `position(symbol)` (an object with `.symbol` and
`.quantity`), and `book(book_id)` (`src/harness/host/src/context.rs:253`).
`cash` and `equity` are exact `Decimal` values; `equity` is `None` until a
position is marked.

`log` and `emissions` are deterministic in-memory sinks: they record structured
records in insertion order rather than writing to a wall-clock log file. A
strategy can inspect `context.log.records` or `context.emissions.records` in
`on_stop` for observability.

Two capabilities are declared in the `StrategyContext` protocol but are **not**
supplied by the native host today, and a strategy must not depend on them:

- `context.data` / `context.history` (`src/harness/strategy/contracts.py:87`).
  The point-in-time data capability is used by the options example
  (`strategies/harness_examples/short_put_spread.py:279`) but the native context
  does not expose it yet.
- `context.scheduler` / `context.subscriptions`, named by the sandbox lint's
  remediation text (`src/harness/strategy/lint.py:67`).

Documenting these as residuals is itself part of this task (see section 10).

## 6. Orders

Submit orders through `context.orders.submit(order)`. Build them from
`harness.core.types`:

```python
SingleLegOrder(
    event_id=order_id,
    timestamp=context.clock.now(),
    sequence=event.sequence,
    order_id=order_id,
    symbol=Symbol(symbol),
    side=Side.BUY,
    quantity=Decimal("20"),
    order_type=OrderType.MARKET,
    time_in_force=TimeInForce.DAY,
)
```

Contract rules enforced by the records themselves:

- `SingleLegOrder` requires `limit_price` for `LIMIT`, `stop_price` for `STOP`,
  both for `STOP_LIMIT`, and neither for `MARKET`
  (`src/harness/core/types/orders.py:119`).
- `MultiLegOrder` needs at least two `OrderLeg` values, supports only `MARKET`
  and `LIMIT`, and requires `limit_price` for `LIMIT`
  (`src/harness/core/types/orders.py:148`).
- `order_id` must be unique and deterministic. The shipped examples use a
  prefix plus a zero-padded ordinal (for example `mom-0001`), which keeps a
  restart reproducible.
- Use exact decimal values. Never pass a binary `float` into a price or
  quantity; the contract stores decimals exactly.

Two v1 adapter behaviors matter:

- `orders.owns(order_id)` returns whether this strategy submitted the order;
  use it to ignore sibling strategies' fills.
- `orders.cancel(order_id)` is currently a no-op in the native context
  (`src/harness/host/src/context.rs:152`), and `orders.replace(order_id,
  replacement)` records the replacement as a new pending submission rather
  than cancelling the original (`src/harness/host/src/context.rs:157`).
  Cancel-and-replace state machines written against the richer contract will
  not behave as written until this is wired.

## 7. Determinism and the sandbox lint

Every strategy is statically checked by `harness.strategy.lint`, an AST pass
that never executes strategy code. It rejects wall-clock reads, sleeping,
networking, subprocesses, unseeded randomness, async syntax, and concurrency
(`src/harness/strategy/lint.py:71`). Use these replacements:

| Instead of | Use |
| --- | --- |
| `time.time()`, `datetime.now()` | `context.clock.now()` |
| `random.random()`, `secrets`, `uuid4` | `context.random.random()` |
| `time.sleep()` | schedule work from the next event |
| `requests`, `httpx`, sockets | point-in-time data through the context |
| `subprocess`, `threading`, `asyncio` | the synchronous callback model |

The gate runs in CI as a sweep over `strategies/`
(`.github/workflows/python-bicep-ci.yml:73`) and is unit-tested from both
directions by `tests/strategy_lint/`. Local check:

```powershell
uv run --extra dev python -c "from harness.strategy.lint import lint_strategy_directory; report = lint_strategy_directory('strategies'); print(report.render()); report.raise_for_status()"
```

## 8. Worked example

The steps below add a complete, deterministic strategy. It is illustrative
only: the file is shown inline so this guide stays self-contained, but an
independent author would create it verbatim under `strategies/harness_examples/`.

### 8.1 The strategy class

`strategies/harness_examples/session_breakout.py`:

```python
"""Session breakout reference strategy: one entry per symbol per session."""

from __future__ import annotations

from decimal import Decimal

from harness.core.symbols import Symbol
from harness.core.types import (
    Fill,
    OrderType,
    QuoteEvent,
    Side,
    SingleLegOrder,
    TimeInForce,
)
from harness.strategy.contracts import Strategy, StrategyContext


class SessionBreakoutStrategy(Strategy):
    """Enter once per session when price breaks the session opening midpoint."""

    def __init__(
        self,
        *,
        threshold: str | Decimal = "0.05",
        quantity: str | int | Decimal = 20,
        order_prefix: str = "breakout",
    ) -> None:
        self.threshold = Decimal(str(threshold))
        self.quantity = Decimal(str(quantity))
        if self.threshold <= 0 or self.quantity <= 0:
            raise ValueError("threshold and quantity must be positive")
        self.order_prefix = str(order_prefix)
        self.context: StrategyContext | None = None
        self._reference: dict[tuple[str, str], Decimal] = {}
        self._position: dict[str, Decimal] = {}
        self._ordinal = 0

    def on_start(self, context: StrategyContext) -> None:
        self.context = context
        for symbol in context.universe.symbols:
            self._position[str(symbol)] = Decimal("0")

    def on_quote(self, event: QuoteEvent) -> None:
        context = self.context
        if context is None:
            return
        symbol = str(event.symbol)
        session = context.clock.now().date().isoformat()
        entry_key = f"{self.order_prefix}:{symbol}:{session}"
        if context.state.get(entry_key):
            return
        mid = (event.bid_price + event.ask_price) / Decimal("2")
        reference = self._reference.setdefault((symbol, session), mid)
        deviation = mid - reference
        if deviation >= self.threshold:
            self._submit(context, symbol, Side.BUY, event.sequence, entry_key)
        elif deviation <= -self.threshold:
            self._submit(context, symbol, Side.SELL, event.sequence, entry_key)

    def on_fill(self, event: Fill) -> None:
        context = self.context
        if context is None or not context.orders.owns(event.order_id):
            return
        symbol = str(event.symbol)
        signed = event.quantity if event.side is Side.BUY else -event.quantity
        self._position[symbol] = self._position.get(symbol, Decimal("0")) + signed

    def on_stop(self, context: StrategyContext) -> None:
        context.emit(
            "session_breakout_summary",
            positions={
                symbol: str(quantity) for symbol, quantity in self._position.items()
            },
        )

    def _submit(
        self,
        context: StrategyContext,
        symbol: str,
        side: Side,
        sequence: int,
        entry_key: str,
    ) -> None:
        self._ordinal += 1
        order_id = f"{self.order_prefix}-{self._ordinal:04d}"
        context.orders.submit(
            SingleLegOrder(
                event_id=order_id,
                timestamp=context.clock.now(),
                sequence=sequence,
                order_id=order_id,
                symbol=Symbol(symbol),
                side=side,
                quantity=self.quantity,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
            )
        )
        context.state[entry_key] = True
        context.emit("session_breakout_entry", symbol=symbol, side=side.value)
```

The example stays inside the sandbox: it uses `context.clock.now()`, exact
decimals, deterministic order ids, and no prohibited imports. It tracks
per-session entries in `context.state`, which is the deterministic snapshot
surface a resumed shell rehydrates.

### 8.2 Files an independent author adds

| File | Change | Owner |
| --- | --- | --- |
| `strategies/harness_examples/session_breakout.py` | the strategy class | strategy author |
| `config/harness/backtest.yaml` | one `strategy_instances[]` entry | config owner |
| `tests/harness/strategy/test_session_breakout.py` | focused behavior test | strategy author |
| `strategies/harness_examples/__init__.py` | optional re-export | strategy author |

No engine, host, or harness file changes are required. If a change does appear
necessary, the contract is missing a capability; raise it as a harness task
rather than patching the engine from strategy code.

### 8.3 Configuration

Add one instance, pointing at the new class and passing constructor keywords:

```yaml
strategy_instances:
  - instance_id: spy-breakout
    implementation: strategies.harness_examples.session_breakout:SessionBreakoutStrategy
    bundle_hash: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    capital_fraction: 1.0
    symbols: [SPY]
    parameters:
      threshold: "0.05"
      quantity: 20
      order_prefix: breakout-spy
```

### 8.4 Focused tests

Mirror the approach of the shipped examples: drive the class with a fake
context or the native backtest fixture. At minimum assert the deterministic
properties a reviewer checks:

- no order is submitted before the reference midpoint exists and the deviation
  crosses the threshold;
- at most one entry per symbol per session;
- the same inputs produce the same `order_id` sequence;
- `on_fill` updates the internal position only for owned orders.

### 8.5 Local verification, then CI

Run these before opening a change. The lint, format, sandbox, type, and test
commands mirror the CI gates (`.github/workflows/python-bicep-ci.yml`), scoped
to the new file; the `backtest-job` run is the local end-to-end check.

```powershell
uv run --extra dev ruff check strategies/harness_examples/session_breakout.py
uv run --extra dev ruff format --check strategies/harness_examples/session_breakout.py
uv run --extra dev python -c "from harness.strategy.lint import lint_strategy; report = lint_strategy('strategies/harness_examples/session_breakout.py'); print(report.render()); report.raise_for_status()"
uv run --extra dev ty check strategies/harness_examples/session_breakout.py
uv run --extra dev pytest -p no:cacheprovider tests/harness/strategy tests/strategy_lint
uv run backtest-job --config config/harness/backtest.yaml
```

CI expectations in full: the Python job runs `ruff check .`,
`ruff format --check .`, the `strategies/` sandbox sweep, `ty check`, and
`pytest`; the Rust job runs `cargo fmt --all --check`,
`cargo clippy --workspace --all-targets -- -D warnings`, and
`cargo test --workspace`; the parity job runs the determinism oracle. Adding a
file under `strategies/**` triggers the Python job because the workflow watches
`strategies/**` and `**/*.py`.

## 9. Running in every shell

The contract is mode-independent by construction: the class observes a
protocol-shaped context and never imports a shell. The Rust host owns the
context, so once a shell's runner is wired, the same class is handed that
shell's context without source changes.

Current shell wiring, stated precisely:

| Shell | Status |
| --- | --- |
| Backtest | Fully wired: `backtest-job` loads `strategy_instances[]` and drives each class through `PyStrategyRuntime`. |
| Paper / Shadow | Same plugin surface and `StrategyHost`; the configured supervision runner is deferred (see `.github/architecture/repository.md`, "Deferred: trader"). |
| Live | Same plugin surface; live execution is capability-gated and the supervision runner is deferred. |

An independent author therefore writes one class and one config entry. No
strategy change is needed when the deferred shells finish wiring; the engine,
not the strategy, owns the mode difference.

## 10. Implemented-versus-declared matrix

The precise gap between the promised contract and what the v1 native adapter
implements:

| Surface | Declared | Implemented natively | Author guidance |
| --- | --- | --- | --- |
| `on_start`, `on_stop` | yes | yes | safe to rely on |
| `on_quote`, `on_trade`, `on_fill` | yes | yes | safe to rely on |
| `on_order`, `on_execution_report`, `on_position`, `on_book`, `on_account` | yes | no | do not depend on them yet |
| `on_corporate_action` | yes | no | splits/dividends are not modeled; see `docs/post-v1-realism-spikes.md` |
| `context.clock`, `universe`, `books`, `orders`, `state`, `random`, `log`, `emissions`, `emit`, `book_id` | yes | yes | safe to rely on |
| `context.data`, `context.history`, `context.scheduler`, `context.subscriptions` | yes | no | do not depend on them yet |
| `orders.submit`, `orders.owns` | yes | yes | safe to rely on |
| `orders.cancel`, `orders.replace` | yes | no-op / new submit | do not write cancel-replace logic yet |

## 11. Independent-author acceptance checklist

1. The class subclasses `Strategy` and imports only the public plugin surface.
2. `on_start`, `on_stop`, and the needed market/fill callbacks are implemented.
3. Every price and quantity is built from `str`/`int`/`Decimal`, never `float`.
4. Time comes only from `context.clock.now()`; randomness only from
   `context.random`; no wall-clock, network, subprocess, async, or thread use.
5. `order_id` values are deterministic and unique for the instance.
6. `context.orders.owns(...)` gates fill handling so sibling strategies are
   ignored.
7. A `strategy_instances[]` entry names `module:Class` and passes every
   constructor parameter.
8. The sandbox sweep, Ruff, ty, and focused tests pass locally.
9. The class is listed in `strategies/**` so the CI sweep covers it.
10. No engine, host, or harness file was changed to make the strategy work.

## 12. Change discipline

When a strategy-facing contract changes, update the harness/strategy sections of
`.github/architecture/repository.md` in the same change and keep the
determinism golden test green. Sandbox lint is a CI gate, not a run-path check;
a strategy that fails the sweep fails the build even if it runs. Treat the
host loader, the context surface, and the callback dispatch set as the
authoritative contract, and update this guide whenever they move.
