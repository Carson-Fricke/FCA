# T190 — Structural no-look-ahead audit of the harness backtest engine

RESEARCH-ONLY report. No code was modified. Evidence cites `file:line` as of this audit.

---

## 1. Summary verdict

**Current verdict: the engine's no-look-ahead posture is predominantly RUNTIME-DETECTED (advisory), with a few genuinely structural islands (frozen events, the fire-and-forget venue protocol, kernel queue monotonicity, frozen portfolio snapshots, subscriber-order as-of semantics). Future information is not yet *unrepresentable*: the strategy API hands strategies (a) the raw mutable engine clock, (b) a re-queryable timestamp-parameterized history API whose guard is a runtime exception and a config toggle, (c) a scheduler that can inject arbitrary market-data events, and (d) in the golden/test path, the raw concrete venue. The one dedicated runtime guard (`LookaheadError` → `LookaheadViolationError`) is effective when enabled but is a toggle (`BacktestOptions.lookahead_guard`, default True) and detects rather than prevents. The sandbox lint — described in `repository.md` as rejecting patterns "before a strategy can run" — is never invoked anywhere in the run path (`load_strategy_class` does not lint; the CLI does not lint); it exists only as a test utility. No exploit found that yields future *data* to a correctly-behaving strategy: all observed leak paths are (i) time-travel queries (caught at runtime when the guard is on), (ii) mutation/fabrication channels (clock advance, event injection, history poisoning, venue introspection in the golden path), or (iii) cross-strategy same-time leakage (all-books snapshot). The kernel's event ordering, the venue's fire-and-forget boundary, and fill/portfolio as-of ordering are sound and must be preserved unchanged.**

Structural (impossible by construction) today:

| Protection | Why structural |
| --- | --- |
| Delivered events are frozen `@dataclass(frozen=True, slots=True)` records | Values cannot be mutated after receipt (`core/types/events.py:27-98`) |
| `ExecutionVenue` protocol is 4 void commands, no queries, outcomes only as events (`venues/base.py:10-33`) | The return type carries no outcome; the engine injects the `_RiskVenue` capability wrapper (`runtime/backtest/strategies.py:173-237`) |
| `EventQueue` rejects events older than the clock head or last-dispatched key (`core/events.py:69-96`) | Kernel invariant, holds regardless of caller |
| `SimClock` moves only on dispatch (`EventQueue.pop`, `core/events.py:108-123`) | Engine semantics, not strategy-visible |
| `PortfolioSnapshot` / `BookSnapshot` / `OHLCVBar` frozen value snapshots (`portfolio/hub.py:61-72`, `data/bars.py:19-60`) | Immutable values |
| Fill accounting and hub marking happen before strategy callbacks (subscriber registration order: engine `_on_fill`/`_on_market` before bindings, `engine.py:227-234`; binding applies fills pre-callback, `strategy/binding.py:107-119`) | Dispatch-order invariant, not a check |
| Venue fill decisions consume only market events already dispatched; fills are scheduled at `head + latency` (`venues/sim.py:312-338, 400-446, 563-570`) | Data-flow ordering |
| Per-context seeded `Random`; freeze-on-write `StateStore` rejecting floats (`strategy/context.py:180-229, 646`) | Constructed inside the context |

Advisory (runtime checks, toggles, or unused tooling) — detailed in §2 with exploit sketches.

---

## 2. Surface-by-surface findings

Legend: **S** = structural, **A** = advisory/runtime, **A-** = advisory and weak (toggleable or bypassable), **S-** = structural with a documented caveat.

| # | Surface | Current mechanism | Class | Evidence | Exploit sketch if advisory |
| --- | --- | --- | --- | --- | --- |
| 1 | Strategy contract callbacks | Frozen event dataclasses handed by reference; callbacks typed to immutable event records | **S** | `strategy/contracts.py:23-76`; `core/types/events.py:27-98` | — (values immutable; no future-bearing fields; `sequence`/`timestamp` are past-stamped at dispatch) |
| 2 | Binding dispatch: fill applied to book *before* `on_fill`; hub marked *before* `on_quote/on_trade` | Subscriber registration order (engine observers registered before strategy bindings) | **S** | `engine.py:205-234` (venue→view→engine→bindings order); `strategy/binding.py:107-119`; `engine.py:461-484` | — |
| 3 | `context.clock` returns the raw injected `SimClock` | `StrategyContext.clock` property returns the engine's `SimClock`, which implements `AdvanceableClock.advance_to/advance_by` | **A-** | `strategy/context.py:655-658, 600-601`; `engine.py:319-320`; `core/clock.py:21-60` | Strategy calls `context.clock.advance_to(now + timedelta(days=1))` mid-callback. Consequences: subsequent feed events with earlier timestamps are rejected by `EventQueue.enqueue` (`core/events.py:78-81`) → run aborts (DoS); `_clip`/`_ensure_future` now use a corrupted head; the run's determinism guarantee is intact but its integrity is strategy-controlled. No guard exists for this at all. |
| 4 | `context.data` / `context.history`: `PointInTimeData.read(timestamp, symbol, kind)` | Explicit caller-supplied timestamp; guard is a runtime `LookaheadError` raised in `_clip` (fail-loud, *not* a silent clip despite doc wording); engine injects the concrete `HistoricalView` | **A** | `strategy/context.py:33-49, 686-693`; `data/history.py:566-592, 622-631`; `engine.py:220-228, 324` | (a) API shape invites future timestamps — representable, rejected only at runtime. (b) The whole mechanism is off when `lookahead_guard=False` (`engine.py:116, 219-228` — `context.data is None`; asserted by `tests/harness/runtime/test_backtest_runner.py:404-415`). (c) The strategy receives the full concrete `HistoricalView`: `view.record(...)` (`history.py:385-395`) lets it inject fabricated ≤head events into the *shared* history (poisoning other strategies' `read/window/aggregate/bars`); `view.subscribe(...)` (`history.py:343-373`) lets it mutate engine-owned buffer capacity; `view.head` exposes the clock head directly. |
| 5 | `context.schedule(event)` / `context.scheduler` | `EventScheduler` forwards any `Event` to `EventBus.schedule` → `EventQueue.enqueue`; only the kernel monotonicity invariant applies | **A-** | `strategy/context.py:86-98, 749-751, 696-698`; `core/events.py:69-97, 195-197` | Strategy fabricates `QuoteEvent(timestamp=head, sequence=chosen)` and schedules it: at dispatch the clock equals its timestamp so the history guard passes, the **SimulatedVenue matches resting orders against it** (`venues/sim.py:312-338`) and the fill is scheduled at `head + latency` — a strategy can fill its own resting orders at a self-chosen price and pollute every other subscriber (including other strategies and the shared history). Strategy-chosen `sequence` also lets same-timestamp events jump the feed order (`events.py:82-86` compares keys; feed order is only the tiebreak for engine-published events). Past timestamps are correctly rejected (`events.py:78-81`). |
| 6 | Venue boundary in the backtest path | `_RiskVenue` capability wrapper exposes exactly 4 commands; risk decisions and rejections are published as events; `SimulatedVenue` introspection (`latest_market`, `active_orders`, `state`, `decisions_for`) is not reachable because strategies get `_RiskVenue` | **S** (engine path) | `venues/base.py:10-33`; `runtime/backtest/strategies.py:173-237`; `venues/sim.py:196-211, 297-310` | — in the engine path. **A** in every other shell: `StrategyContext.venue` (`context.py:681-683`) and `OrdersView.venue` (`context.py:516-519`) return whatever object was injected, typed only as a `runtime_checkable` protocol — the golden harness injects the **raw `ScriptedVenue`** (`testing/golden.py:578-588`), which exposes `calls` and `remaining_steps` (`venues/scripted.py:98-105`); a custom shell passing `SimulatedVenue` leaks venue-side pending-fill state via `state()`/`decisions_for()` before reports arrive. Python typing cannot prevent a downcast/attr call. |
| 7 | Venue/fill data-flow timing | Fill decisions made at market-event dispatch using as-of-now state; fills/reports scheduled at `head + latency` with engine-assigned sequence | **S** | `venues/sim.py:213-247, 400-496, 558-570`; `core/events.py:108-123` | — (venue never consults future state; ordering enforced by the queue) |
| 8 | Portfolio views | `BooksView`/`AccountView` are live read-only views over the hub; snapshots are frozen values built at call time (= engine now) | **S** (immutability) / **A** (scope) | `strategy/context.py:404-497`; `portfolio/hub.py:148-172`; `dataclass(frozen=True)` snapshots `hub.py:32-72, 122-127` | Same-time as-of semantics are correct (fills applied pre-callback, marks applied pre-callback — surface 2). Scope leak: `BooksView.book(book_id)` resolves **any** book in the shared account and `PortfolioSnapshot.books` carries all constituent books (`strategy/context.py:444-454`; `portfolio/hub.py:154-172`) → strategy A mirrors strategy B's positions same-tick (cross-strategy info leak; integrity/fairness, not future-data). |
| 9 | Risk pipeline | Invoked engine-side inside `_RiskVenue._admit` with engine-now `RiskContext`; decisions/rejections recorded and published as events | **S** | `runtime/backtest/strategies.py:203-236`; `engine.py:278-304` | — |
| 10 | OMS | Not in the backtest strategy path at all (only `venues/options_matching.py` imports it); OMS state changes only by applying validated execution reports | **S** | grep: `harness.oms` imported only by `oms/*` and `venues/options_matching.py:38` | — |
| 11 | Recorder/results | Engine-owned; nothing strategy-facing imports it | **S** | `results/` imports only internal; engine writes results (`engine.py:488-514`) | — |
| 12 | Sandbox lint | Static AST checker (wall-clock/sleep/network/subprocess/unseeded-random/asyncio/concurrency) exported from `harness.strategy`; **never called by the loader, engine, or CLI** — only by tests | **A-** (as shipped) | `strategy/lint.py` (whole module); loader `runtime/backtest/strategies.py:38-61` has no lint call; CLI grep: no lint; only tests call it (`tests/strategy_lint/test_strategy_lint.py`, `tests/integration/strategies/test_reference_strategies.py:302`) | A strategy using `time.time()`/`random.random()` runs in a backtest undetected today (determinism hazard; also `datetime.now()` is a wall-clock read that a lint-only defense would catch but nothing currently does). Repository.md:129-131 overstates: "strategy sandbox lint rejects ... before a strategy can run". |
| 13 | Look-ahead runtime guard in the engine | `_GuardedBinding` captures `LookaheadError` per strategy; `_settle` → `_lookahead` → abort with `LookaheadViolationError`; recorded in `BacktestResult.lookahead_violations` | **A** (detection, correctly fail-loud) | `engine.py:367-398`; `runtime/backtest/strategies.py:151-171`; `runtime/backtest/errors.py:44-45`; `tests/harness/runtime/test_backtest_runner.py:393-400` | Toggle `BacktestOptions.lookahead_guard: bool = True` (`engine.py:116`) removes the view entirely rather than downgrading — acceptable as a knob but confirms the protection is not structural. |
| 14 | Golden determinism harness | `run_scenario` builds a fresh SimClock/EventBus/Hub/ScriptedVenue runtime, canonicalizes feed/dispatch/strategy/portfolio/venue artifacts, and `assert_reproducible` compares two runs byte-for-byte + `dispatch_hash` | **S** as an arbiter; **gap**: scenarios pass `data=None` (no history view exercised) and inject the raw venue (surface 6) | `testing/golden.py:568-650`; `tests/golden/test_golden_harness.py:68-76` | The determinism arbiter never exercises `context.data`, so engine-path changes to the history/capability surface are unguarded by the golden test today. |
| 15 | Historical view internals | Ring buffers keyed `(symbol, kind)`; queries return tuples of frozen events (immutable values); `_guard` rejects future-dated recording; `_clip` raises `LookaheadError` for future `as_of`; clip is **on read**, not on construction; the view is long-lived and shared | **A** (guard) / **S** (value immutability) | `data/history.py:140-218, 385-395, 613-631` | Captured-view re-query: a strategy that stores `context.data` in `__init__` and re-queries later gets only ≤head data (correct), so the *leak* requires the guard to be bypassed (guard-off, fabricated `record`, or clock mutation — surfaces 3-5). Note: `repository.md` says queries "hard-clip"; the code **raises** `LookaheadError` instead (fail-loud) — doc/code mismatch to fix in the doc. |
| 16 | Replay/lake feed (engine side) | Feed published one event at a time between drains; preflight verifies replayed counts/fingerprints against the fixture; not strategy-reachable | **S** | `engine.py:352-359, 516-536`; `data/replay.py` | — |
| 17 | Bars | Pure function of frozen `TradeEvent`s behind the clipped window API | **S** (value) / **A** (window guard inherited from surface 15) | `data/bars.py:19-60`; `history.py:548-564` | — |

**Callback-time observability (surface 2 detail).** At the instant a strategy callback runs for event `E` at time `T`, it can observe: `context.clock.now() == E.timestamp` (clock advanced at pop, `events.py:113-114`); history containing every *dispatched* market event including `E` (history subscribes before bindings, `engine.py:227` vs `234`) and nothing queued-but-undispatched; the hub already marked with `E` (`engine.py:461-467` registered before bindings); its own order ownership; frozen event payloads; seeded RNG. Fills/reports for `E`-time outcomes are dispatched only in later drain iterations. **No dispatched-future path exists in this ordering** — the leaks are the mutation/fabrication channels (3, 5) and the guard-toggle (13), plus same-time cross-strategy scope (8).

---

## 3. Hardening design

Goal: future information must be **unrepresentable** at the strategy boundary; runtime guards remain as engine-internal assertions and defense-in-depth, never load-bearing. All changes must keep valid-strategy outcomes byte-identical — the golden harness (`assert_reproducible`) plus the runner's manifest comparison (`tests/harness/runtime/test_backtest_manifest.py:204-263`) are the arbiter for every engine-path change.

### D1 — Capability-scoped clock (structural)
Introduce a `ReadonlyClock` (name: `ClockView`) in `harness.core.clock`: a wrapper exposing only `now()` (and `head` for engine-internal use never exposed to strategies). `StrategyContext` stores and exposes the wrapper; the engine keeps the `SimClock`. The `Clock` protocol already matches (`core/clock.py:11-17`) — the change is that the context *wraps on construction* (`StrategyContext.__init__` accepts the engine clock and stores `ReadonlyClock(clock)`), so a strategy holding `context.clock` has no `advance_to` attribute at all.

- Semantics preservation: `now()` delegates; every legal read is unchanged. No valid strategy ever called `advance_to` (it would already corrupt a run).
- Type-level separation: `AdvanceableClock` remains kernel-only; the strategy-facing type is the read-only protocol.

### D2 — Opaque point-in-time capability view (structural)
Keep `HistoricalView` (explicit `as_of` + `LookaheadError`) as the **engine/test-facing** API. Add a narrow `StrategyHistory` facade in `harness.data` (or `harness.strategy.context`) that wraps `(view, clock)` and exposes **now-anchored** queries only:

- `read(symbol, data_type=None) -> Event | None` — no timestamp parameter at all (engine now implied).
- `last(symbol, count, selector=None)`, `window(symbol, start, end, selector=None)` where `end` may be omitted (defaults to engine now) and any supplied future `end` raises `LookaheadError` (kept as defense-in-depth; a *past* window is a legitimate backtest query and stays).
- `bars(symbol, interval, start, end=None)`, `aggregate(...)` similarly now-capped.
- No `record`, `subscribe`, `attach`, `capacity`, `subscriptions`, `head` surfaces.

`StrategyContext.data/history` expose only `StrategyHistory`. `PointInTimeData` protocol is re-shaped to `read(symbol, data_type)` (single-timestamp form deleted from the strategy contract).

- Semantics preservation: every existing valid call `read(t, sym)` with `t <= head` maps to `read(sym)` when `t == head` (the only non-degenerate case at callback time, since `t > head` already aborts); `t < head` callers use `window`/`last(as_of=t)` equivalents which remain available with *past* bounds. The canary (`tests/harness/data/test_history.py:286-358`) and runner lookahead tests are updated to the new surface; their assertions (never observe `timestamp > head`) are unchanged.
- The view stays shared and re-queryable, but every query is re-clipped to the engine clock **inside engine-owned code with no caller-supplied future representable** — the exploit class "hold a reference and time-travel later" is reduced to "query now", and the exploit class "record/poison" disappears because the facade has no `record`/`subscribe`/`attach`.

### D3 — One-way venue command channel with provenance (structural, formalize what exists)
The engine path is already correct (`_RiskVenue` capability, void commands, outcomes only as events). Hardening:

1. Make the capability explicit and public: promote a `StrategyVenue` (rename/wrap `_RiskVenue`'s four commands) in `runtime/backtest/strategies.py`; `OrdersView` stops exposing `.venue` (commands already exist on `OrdersView` itself — `strategy/context.py:525-541`), removing the raw-object escape hatch.
2. Golden harness: inject the capability-shaped venue into `StrategyContext` instead of the raw `ScriptedVenue` (`testing/golden.py:578-588`), so `calls`/`remaining_steps` become unreachable from strategy code; `ScriptedVenue` remains the test-facing step verifier held by the scenario, not the context.
3. Receipts: optional, reject for now. `submit() -> None` plus `orders.submitted_order_ids`/`owns` already give ownership receipts; adding a receipt object changes no semantics but expands the API without closing a hole (the current void return is already non-observant). Revisit only if multi-venue shells need it.
- Semantics preservation: pure narrowing; every observable outcome still arrives as a dispatched event. Golden `venue/calls.json` artifact is produced from the scenario-owned venue, unchanged.

### D4 — Strategy event provenance (structural)
`EventScheduler` (the strategy scheduler capability, `strategy/context.py:86-98`) enforces, at `schedule()`:

1. **Reserved event classes**: strategies may schedule only instances of a dedicated `StrategyEvent(Event)` base (new, `core/types/events.py`, no new `EventType` member needed if it reuses `EventType.ORDER`'s priority — simplest: a marker base class, engine observers simply never subscribe to it). Submitting a `QuoteEvent`/`TradeEvent`/`ExecutionReport`/`Fill`/`Position`/`Book`/`AccountSnapshot` through the strategy scheduler raises `StrategyScheduleError` before enqueue.
2. **Engine-assigned sequence**: the strategy scheduler stamps `sequence` from an engine-owned counter (event `sequence` input ignored/overwritten) so fabricated events cannot reorder against the feed at equal timestamps.

`EventBus`/`EventQueue` stay permissive (the venue and engine schedule reserved events internally), so the kernel contract in `repository.md` is untouched.

- Semantics preservation: no existing engine-path or golden strategy schedules any event (`ToyStrategy`, examples, test strategies — verified by reading them); kernel ordering code unchanged; `EVENT_CLASS_PRIORITY` table unchanged.

### D5 — Book-view scope (structural, one semantic decision required)
`BooksView.book(book_id)` resolves only the context's own `book_id`; other identifiers raise `UnknownBookError`. The shared-account **aggregate** (`context.portfolio`, `account.snapshot`) remains visible (that is the account the strategy trades in). Cross-strategy constituent books become unrepresentable.

- Semantics: single-strategy runs (all current golden scenarios, single-instance configs, examples) are unaffected. Multi-instance runs where a strategy reads a sibling book would change — flagged as the one deliberate semantic tightening in §7 (open question Q1).

### D6 — Lint as defense-in-depth only (position confirmed, doc fixed)
The lint must stay out of the engine run path (it is static, incomplete by design, and bypassable via dynamic constructs — `getattr`, `eval` is out of scope for a linter). Actions:

1. Keep `assert_sandbox_clean` as a **CI/test-time** gate: extend the lint suite to sweep `strategies/` (the shipped examples) in addition to fixtures.
2. Correct `repository.md:129-131` to state that the lint is CI defense-in-depth and that look-ahead protection is structural at the strategy boundary (post-hardening) with a fail-loud engine guard for engine-facing misuse.
- No runtime behavior change.

### D7 — Adversarial detector tests (the enforcement of "structural")
New suite `tests/harness/strategy/test_adversarial_lookahead.py` (plus runner-path cases). Each test constructs a **future-knowledge or mutation strategy** and asserts either (a) the attempt is unrepresentable (AttributeError/TypeError — the capability has no such surface), (b) a typed harness error is raised and the run policy applies, or (c) the observable outcome set is unchanged. Details in §4.

### D8 — Type-level separation (structural, enforced by test)
`harness.strategy` should import no concrete engine type. Today `strategy/context.py` imports `Clock`, `EventBus`, `PortfolioHub`, `ExecutionVenue`, `PortfolioSnapshot` for `isinstance` validation — replace with the existing `runtime_checkable` protocols (define narrow `PortfolioBooks`, `VenueCommands` protocols in `harness.strategy` and have engine types satisfy them structurally). Enforce with an architecture test asserting `harness.strategy` does not import `harness.{core.events, data, portfolio, venues, runtime}` (clock protocol stays; concrete `SimClock` must not be imported there).

- Semantics preservation: structural typing only; no dispatch change.

---

## 4. Adversarial detector test plan

New file(s): `tests/harness/strategy/test_adversarial_lookahead.py` (context-level, fast) and cases appended to `tests/harness/runtime/test_backtest_runner.py` (engine-level, golden-arbitrated). Pattern reused from the existing canary (`tests/harness/data/test_history.py:286-358`): the strategy records every read it obtained; the assertion is over what it *saw*.

| # | Adversary | Construct | Pass condition |
| --- | --- | --- | --- |
| A1 | Clock mover | `on_quote`: `context.clock.advance_to(context.clock.now() + timedelta(days=1))` | AttributeError on the read-only capability (D1); engine path additionally asserts the run completes with identical `dispatch_hash` to the same feed without the attempt |
| A2 | Time-travel reader | Captures `context.data` at `on_start`; on each quote queries a window ending `head + 1s` | With `StrategyHistory`: future `end` raises `LookaheadError`; with a now-only `read`, no future argument exists (TypeError). Engine-level: `LookaheadViolationError` abort retained as belt-and-braces |
| A3 | Captured-view re-query | At `on_quote` #1 store `(view, event)`; at `on_quote` #5 re-run every read and assert no returned event has `timestamp > head_at_read` | No future event in any read result; identical to the existing canary assertion but through the capability facade |
| A4 | Market-data fabricator | `context.schedule(QuoteEvent(...))` with `timestamp=head` and attacker-chosen `sequence` | `StrategyScheduleError` (D4); engine `dispatch_hash` and venue calls unchanged vs. baseline run |
| A5 | Venue state prober | `dir(context.orders)` / `context.venue` probes for `state`, `decisions_for`, `active_orders`, `latest_market`, `calls`, `remaining_steps` | None of these attributes exist on the strategy-visible capability (D3) |
| A6 | History poisoner | Probes the data capability for `record`, `record_many`, `subscribe`, `attach` | Not exposed (D2); direct `HistoricalView` misuse remains engine/test-side only |
| A7 | Cross-book reader | `context.books.book("<other-instance-id>")` | Raises `UnknownBookError`/is restricted to own book + aggregate (D5, gated on Q1) |
| A8 | Guard-off equivalence | Run the full adversarial set with `BacktestOptions(lookahead_guard=False)` | No adversary can *obtain* future data even with the runtime guard disabled — this is the test that proves structurality rather than guard-dependence |
| A9 | Determinism invariance | For every valid (non-adversarial) strategy in `strategies/harness_examples` + `ToyStrategy`: golden `assert_reproducible` before/after each hardening wave | Byte-identical artifacts, `dispatch_hash`, manifest |

A8 is the decisive detector: today several A-cases would pass trivially *because* `context.data`/`context.clock` expose rich engine objects only to be rejected at runtime or not at all; after hardening they pass because the capability lacks the surface.

---

## 5. Implementation wave breakdown

Ordered waves; one task per file set; **exclusive file ownership per task**; the coordinator runs repository verification (ruff + ty + pytest via `uv` canonical commands) once after each wave's edit freeze; `tests/golden/test_golden_harness.py` (via `assert_reproducible`) and `tests/harness/runtime/test_backtest_runner.py` + `test_backtest_manifest.py` are the arbiter for every engine-path task. `strategy/context.py` is integration-owned and touched by exactly one task per wave.

| Order | Task | Files (exclusive) | Acceptance |
| --- | --- | --- | --- |
| W1-T191 | Read-only clock capability | `src/harness/core/clock.py` (add `ReadonlyClock`/view), `src/harness/strategy/context.py` (wrap + expose), `tests/harness/strategy/test_strategy.py` (new cases), `tests/harness/core/test_clock.py` | `context.clock` has no `advance_to/advance_by/head`; engine unchanged elsewhere; golden `assert_reproducible` passes; runner suite green |
| W1-T192 | Venue capability in engine + golden | `src/harness/runtime/backtest/strategies.py` (promote `_RiskVenue` commands to public `StrategyVenue`), `src/harness/testing/golden.py` (inject capability venue), `tests/harness/runtime/test_backtest_runner.py`, `tests/golden/test_golden_harness.py` | `context.venue`/`orders.venue` expose commands only; A5 probe test green; golden artifacts byte-identical to pre-change baseline |
| W2-T193 | `StrategyHistory` capability view | `src/harness/data/history.py` (add facade, keep `HistoricalView` engine API), `src/harness/data/__init__.py`, `src/harness/strategy/context.py` (new `PointInTimeData` shape), `tests/harness/data/test_history.py`, `tests/harness/strategy/test_strategy.py` | Facade exposes now-anchored `read/last/window/bars/aggregate` only; past-window queries preserved; A2/A3/A6 pass; history unit suite green |
| W2-T194 | Engine wiring of the data capability | `src/harness/runtime/backtest/engine.py` (inject facade; guard toggle now governs the engine-side view only), `tests/harness/runtime/test_backtest_runner.py` (lookahead tests updated), `tests/harness/runtime/test_backtest_manifest.py` | `LookaheadViolationError` abort path retained; `lookahead_guard=False` run still denies future reads at the capability (A8); manifest hashes for existing configs unchanged except where the surface legitimately changed (assert explicitly) |
| W3-T195 | Strategy-schedule provenance | `src/harness/strategy/context.py` (`EventScheduler` reserved-type + engine-stamped sequence), `src/harness/core/types/events.py` (add `StrategyEvent` marker base only), `tests/harness/strategy/test_strategy.py`, `tests/harness/core/test_events.py` | A4 passes; kernel ordering untouched; golden + runner suites green |
| W4-T196 | Book-view scope (gated on Q1 decision) | `src/harness/strategy/context.py` (`BooksView.book` restriction), `tests/harness/strategy/test_strategy.py`, `tests/harness/portfolio/test_hub.py` | A7 passes; single-book golden byte-identical; multi-book behavior change documented in the architecture doc |
| W4-T196b | Type separation + import-boundary test | `src/harness/strategy/context.py` (protocol-only imports), new `tests/harness/strategy/test_import_boundary.py` | `harness.strategy` imports no concrete engine type; `ty` + `ruff` clean |
| W5-T197 | Adversarial suite + lint sweep (defense-in-depth) | new `tests/harness/strategy/test_adversarial_lookahead.py`, `tests/strategy_lint/test_strategy_lint.py` (sweep `strategies/`) | A1-A8 all green in one suite; adversarial strategies fail loudly under both guard states |
| W5-T198 | Golden arbiter extension for the data path | `src/harness/testing/golden.py` (optional `data=` scenario support producing a `history/` artifact), `tests/golden/test_golden_harness.py` | A golden scenario exercising `context.data` reproduces byte-identically across two runs |
| W5-T199 | Architecture doc correction | `.github/architecture/repository.md` (lint wording, structural boundary description, hard-clip→fail-loud correction) | Doc matches implemented mechanism; no code in the task |

Wave sequencing rationale: W1 closes the two raw-object escapes with zero API-shape change (lowest risk); W2 moves the data boundary (the actual look-ahead surface) once W1's capability pattern is proven; W3 closes fabrication; W4 closes scope + enforces the boundary with tests; W5 hardens the arbiter and docs. Each wave is independently shippable and golden-arbitrated.

---

## 6. Risks / open questions

- **Q1 (semantic decision)**: cross-book visibility (`BooksView.book`, `PortfolioSnapshot.books`). Restricting is a behavior change for multi-strategy runs. Default proposal: restrict to own book + account aggregate; needs an owner sign-off before W4-T196.
- **R1**: Python's dynamism means "structural" here means *unreachable through the sanctioned capability surface and enforced by the type/boundary tests*, not interpreter-level sandboxing. A strategy can still `import harness.data` and build its own objects; that is lint/CI territory by design (D6). State this explicitly in the docs so "structural" is not oversold (the current `repository.md` already oversells the lint).
- **R2**: `PointInTimeData` is a published protocol; reshaping `read(timestamp, ...)` → `read(symbol, ...)` is an API break for external strategies. Mitigation: keep the old signature on the engine-side `HistoricalView` only; the strategy protocol changes in the same release that ships the facade, and the two example suites + golden scenarios are the compatibility fence.
- **R3**: `_RiskVenue` → public `StrategyVenue` rename touches the runner's strategy glue; keep the private alias during W1 to avoid cross-wave churn.
- **R4**: Engine-stamped sequence numbers for strategy-scheduled events (D4) alter dispatch order *only* for strategies that deliberately mis-sequenced fabricated events — no existing test or example schedules events at all (verified), so golden/runner baselines hold.
- **R5**: The `_GuardedBinding`/`_StrategySink` taxonomy must keep classifying `LookaheadError` after the facade lands (the facade may raise it from `window` with future `end`); the runner abort semantics (`engine.py:388-398`) are unchanged by design.
- **R6**: `repository.md`/code mismatch on "hard-clip" (code raises) is corrected in W5-T199, not silently: the fail-loud behavior is the better one and is what the tests already assert (`tests/harness/data/test_history.py:124-155`).
- **OQ2**: `StrategyContext.random` is per-context seeded but shared across strategies with the same configured seed — deterministic, but two strategies see identical streams; acceptable (documented) but worth noting as a future correlation hazard, not a look-ahead issue.
- **OQ3**: `EventScheduler` accepts any `EventBus` instance today; after D8, the context should accept only the shell-provided scheduler capability, making the bus unreachable even by accidental construction.
