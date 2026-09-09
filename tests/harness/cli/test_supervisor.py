"""Trader supervision stack tests: settings, journal, kernel boot paths,
kill-switch table adapter, and the ``harness trader status`` command.

The tests prove the acceptance behaviors of the supervision stack: the
validated supervision knobs; the durable ``supervision.json`` and
``restarts.json`` documents the journal writes under the run's state-store
prefix; every observed boot gate (kill switch, crash-loop budget, missing
market session, restartable connect faults, unexpected faults) with its
platform exit code; the XNYS session resolver; the Azure Table kill-switch
source and its environment contracts; and the status command that renders the
supervision document.  Every wall-clock value comes from an injected
:class:`~harness.core.clock.SimClock` and every pacing boundary is
monkeypatched, so no test waits.
"""

from __future__ import annotations

import json
import sys
import types
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from azure.core.credentials import AzureNamedKeyCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient
from typer.testing import CliRunner

from harness.cli import supervisor as sup
from harness.cli import trader
from harness.cli.supervisor import (
    FORMAT_VERSION,
    KILL_SWITCH_TABLE_PARTITION_KEY,
    KILL_SWITCH_TABLE_ROW_KEY,
    KillSwitchObservation,
    OutcomeKind,
    SessionJournal,
    SupervisionConfigurationError,
    SupervisionPhase,
    SupervisorSettings,
    TableKillSwitchPoller,
    TraderExitCode,
    TraderSupervisor,
    trading_session_for,
)
from harness.configuration import (
    StrategyInstanceConfig,
    TraderConfig,
    load_example,
)
from harness.core.clock import SimClock
from harness.core.symbols import Symbol
from harness.core.types import Fill, QuoteEvent
from harness.risk.kill_switch import KillSwitch
from harness.runtime import (
    PaperOptions,
    PaperSessionPolicy,
    PaperShell,
    RuntimeStateStore,
)
from harness.strategy import Strategy, StrategyContext
from lake.contracts import ExchangeSession
from lake.contracts.sessions import xnys_sessions

RUN_ID = "trader-supervisor-1"
NOW = datetime(2026, 9, 9, 14, 0, tzinfo=UTC)
SESSION_DATE = date(2026, 9, 9)
OPEN = datetime(2026, 9, 9, 13, 30, tzinfo=UTC)
CLOSE = datetime(2026, 9, 9, 20, 0, tzinfo=UTC)
CASH = Decimal("100000.00")
STRATEGY_MODULE = "supervisor_test"

runner = CliRunner()


# --------------------------------------------------------------------------
# Test strategy, storage adapters, and fakes
# --------------------------------------------------------------------------


class NoopStrategy(Strategy):
    """Bind without submitting orders; the supervision tests never pump."""

    def on_start(self, context: StrategyContext) -> None:
        return None

    def on_quote(self, event: QuoteEvent) -> None:
        return None

    def on_fill(self, event: Fill) -> None:
        return None

    def on_stop(self, context: StrategyContext) -> None:
        return None


def _register_strategy_module() -> None:
    """Expose the test strategy under the configured implementation module."""
    module = types.ModuleType(STRATEGY_MODULE)
    for name, value in (("NoopStrategy", NoopStrategy),):
        setattr(module, name, value)
    sys.modules[STRATEGY_MODULE] = module


_register_strategy_module()


class MemorySnapshotStore:
    """In-memory adapter for the snapshot object-store port."""

    __slots__ = ("_objects",)

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def replace(self, relative_path: str, payload: bytes) -> None:
        self._objects[relative_path] = bytes(payload)

    def read(self, relative_path: str) -> bytes:
        try:
            return self._objects[relative_path]
        except KeyError:
            raise KeyError(relative_path) from None


class _RecordingPoller:
    """Kill-switch boundary serving one observation or one injected error."""

    def __init__(
        self,
        observation: KillSwitchObservation | None = None,
        error: Exception | None = None,
    ) -> None:
        self.polls = 0
        self._observation = observation
        self._error = error

    def poll(self) -> KillSwitchObservation:
        self.polls += 1
        if self._error is not None:
            raise self._error
        assert self._observation is not None
        return self._observation


class _FakeFeed:
    """Minimal live boundary recording whether the kernel closed it."""

    def __init__(self) -> None:
        self.closed = False

    @property
    def exhausted(self) -> bool:
        return False

    def next_event(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _RecordingFeedFactory:
    """Feed factory counting calls; serves a feed or raises every time."""

    def __init__(
        self,
        *,
        feed: _FakeFeed | None = None,
        error: Exception | None = None,
    ) -> None:
        self.calls = 0
        self._feed = feed
        self._error = error

    def __call__(self) -> Any:
        self.calls += 1
        if self._error is not None:
            raise self._error
        assert self._feed is not None
        return self._feed


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def _history(ledger: dict[str, object]) -> list[dict[str, Any]]:
    """Return the ledger's bounded outcome history with readable entries."""
    return cast("list[dict[str, Any]]", ledger["history"])


def _instance(instance_id: str = "solo") -> StrategyInstanceConfig:
    return StrategyInstanceConfig(
        instance_id=instance_id,
        implementation=f"{STRATEGY_MODULE}:NoopStrategy",
        bundle_hash="b" * 64,
        capital_fraction=Decimal("1.0"),
        symbols=("SPY",),
        parameters={},
    )


def _trader_config() -> TraderConfig:
    example = load_example("trader")
    assert isinstance(example, TraderConfig)
    return example.model_copy(
        update={"mode": "paper", "strategy_instances": (_instance(),)}
    )


def _session() -> ExchangeSession:
    return ExchangeSession(SESSION_DATE, OPEN, CLOSE)


def _journal() -> tuple[MemorySnapshotStore, SessionJournal, SimClock]:
    clock = SimClock(NOW)
    store = MemorySnapshotStore()
    journal = SessionJournal(store=store, run_id=RUN_ID, clock=clock)
    return store, journal, clock


def _supervisor(
    store: MemorySnapshotStore,
    journal: SessionJournal,
    clock: SimClock,
    *,
    session: ExchangeSession | None = None,
    poller: _RecordingPoller | None = None,
    feed_factory: Callable[[], Any] | None = None,
    settings: SupervisorSettings | None = None,
) -> TraderSupervisor:
    return TraderSupervisor(
        configuration=_trader_config(),
        settings=settings if settings is not None else SupervisorSettings(),
        session=session,
        run_id=RUN_ID,
        state_store=RuntimeStateStore(store, run_id=RUN_ID),
        journal=journal,
        clock=clock,
        feed_factory=(
            feed_factory
            if feed_factory is not None
            else _RecordingFeedFactory(error=AssertionError("feed must not compose"))
        ),
        kill_switch=KillSwitch(),
        kill_switch_poller=poller,
    )


def _quote() -> QuoteEvent:
    return QuoteEvent(
        event_id="quote-1",
        timestamp=OPEN + timedelta(minutes=1),
        sequence=1,
        symbol=Symbol("SPY"),
        bid_price=Decimal("449.99"),
        bid_quantity=Decimal("100"),
        ask_price=Decimal("450.01"),
        ask_quantity=Decimal("100"),
        venue="XNYS",
    )


def _example_yaml(name: str, tmp_path: Path) -> Path:
    config_path = tmp_path / name
    config_path.write_text(
        (Path(__file__).parents[3] / "config" / "harness" / name).read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    return config_path


# --------------------------------------------------------------------------
# SupervisorSettings validation
# --------------------------------------------------------------------------


def test_supervisor_settings_defaults_are_valid() -> None:
    settings = SupervisorSettings()

    assert settings.boot_policy == "resume_reconcile"
    assert settings.initial_cash == CASH
    assert settings.heartbeat_cadence == timedelta(seconds=60)
    assert settings.kill_switch_poll == timedelta(seconds=10)
    assert settings.kill_switch_stale == 3
    assert settings.snapshot_cadence == timedelta(seconds=60)
    assert settings.reconcile_cadence == timedelta(minutes=15)
    assert settings.market_wait == timedelta(minutes=15)
    assert settings.idle_pace == timedelta(seconds=1)
    assert settings.connect_attempts == 2
    assert settings.connect_backoff == timedelta(seconds=2)
    assert settings.restart_budget == 3


def test_supervisor_settings_reject_unknown_boot_policies() -> None:
    with pytest.raises(SupervisionConfigurationError, match="unknown boot policy"):
        SupervisorSettings(boot_policy="restart_fresh")


@pytest.mark.parametrize(
    "field_name",
    [
        "heartbeat_cadence",
        "kill_switch_poll",
        "snapshot_cadence",
        "reconcile_cadence",
        "market_wait",
        "idle_pace",
        "connect_backoff",
    ],
)
def test_supervisor_settings_reject_non_positive_durations(field_name: str) -> None:
    override = cast(dict[str, Any], {field_name: timedelta(0)})
    with pytest.raises(SupervisionConfigurationError, match="positive duration"):
        SupervisorSettings(**override)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("kill_switch_stale", 0),
        ("connect_attempts", 0),
        ("restart_budget", 0),
        ("restart_budget", True),
    ],
)
def test_supervisor_settings_reject_non_positive_integer_knobs(
    field_name: str, value: Any
) -> None:
    with pytest.raises(SupervisionConfigurationError, match="positive integer"):
        SupervisorSettings(**{field_name: value})


def test_supervisor_settings_reject_non_positive_initial_cash() -> None:
    with pytest.raises(SupervisionConfigurationError, match="initial_cash"):
        SupervisorSettings(initial_cash=Decimal("0.00"))


# --------------------------------------------------------------------------
# SessionJournal durable documents
# --------------------------------------------------------------------------


def test_session_journal_status_round_trip_carries_clock_and_ledger_facts() -> None:
    store, journal, _ = _journal()
    journal.record_boot_outcome(OutcomeKind.FAULT, "injected fault")

    journal.record_status(
        phase=SupervisionPhase.CONNECT, detail="connecting", heartbeat=True
    )
    status = journal.read_status()

    assert status["format_version"] == FORMAT_VERSION
    assert status["run_id"] == RUN_ID
    assert status["phase"] == "connect"
    assert status["detail"] == "connecting"
    stamp = NOW.isoformat(timespec="microseconds")
    assert status["phase_at"] == stamp
    assert status["updated_at"] == stamp
    assert status["last_heartbeat_at"] == stamp
    assert status["consecutive_faults"] == 1
    assert store.read(f"runs/{RUN_ID}/supervision.json")


def test_read_ledger_returns_a_fresh_document_when_absent() -> None:
    _, journal, _ = _journal()

    ledger = journal.read_ledger()

    assert ledger == {
        "format_version": FORMAT_VERSION,
        "run_id": RUN_ID,
        "consecutive_faults": 0,
        "history": [],
    }


def test_boot_outcome_ledger_counts_faults_and_resets_on_clean_outcomes() -> None:
    _, journal, _ = _journal()

    journal.record_boot_outcome(OutcomeKind.FAULT, "first fault")
    journal.record_boot_outcome(OutcomeKind.FAULT, "second fault")
    assert journal.read_ledger()["consecutive_faults"] == 2

    journal.record_boot_outcome(OutcomeKind.CLEAN, "clean boot")
    assert journal.read_ledger()["consecutive_faults"] == 0

    journal.record_boot_outcome(OutcomeKind.FAULT, "third fault")
    journal.record_boot_outcome(OutcomeKind.KILL_SWITCH, "operator halt")
    ledger = journal.read_ledger()
    assert ledger["consecutive_faults"] == 0
    assert [entry["outcome"] for entry in _history(ledger)] == [
        "fault",
        "fault",
        "clean",
        "fault",
        "kill_switch",
    ]


def test_ledger_history_is_bounded_to_eight_entries() -> None:
    _, journal, _ = _journal()

    for index in range(10):
        journal.record_boot_outcome(OutcomeKind.FAULT, f"fault {index}")

    ledger = journal.read_ledger()
    assert ledger["consecutive_faults"] == 10
    assert len(_history(ledger)) == 8
    assert _history(ledger)[-1]["detail"] == "fault 9"


# --------------------------------------------------------------------------
# trading_session_for: XNYS session resolution
# --------------------------------------------------------------------------


def test_trading_session_for_returns_none_on_a_weekend() -> None:
    saturday = date(2026, 9, 5)
    assert xnys_sessions(saturday, saturday) == ()

    clock = SimClock(datetime(2026, 9, 5, 15, 0, tzinfo=UTC))

    assert trading_session_for(clock) is None


def test_trading_session_for_returns_the_session_covering_the_instant() -> None:
    (session,) = xnys_sessions(SESSION_DATE, SESSION_DATE)
    clock = SimClock(session.open_time + timedelta(minutes=5))

    assert trading_session_for(clock) == session


def test_trading_session_for_returns_the_next_session_before_open() -> None:
    (session,) = xnys_sessions(SESSION_DATE, SESSION_DATE)
    clock = SimClock(session.open_time - timedelta(minutes=30))

    assert trading_session_for(clock) == session


def test_trading_session_for_returns_none_after_the_session_close() -> None:
    (session,) = xnys_sessions(SESSION_DATE, SESSION_DATE)
    clock = SimClock(session.close_time + timedelta(minutes=1))

    assert trading_session_for(clock) is None


# --------------------------------------------------------------------------
# TraderSupervisor boot paths
# --------------------------------------------------------------------------


def test_kill_switch_gate_halt_exits_41_and_records_the_outcome() -> None:
    store, journal, clock = _journal()
    poller = _RecordingPoller(
        KillSwitchObservation(engaged=True, reason="operator halt", at=NOW)
    )
    supervisor = _supervisor(store, journal, clock, session=_session(), poller=poller)

    outcome = supervisor.run()

    assert outcome.exit_code is TraderExitCode.KILL_SWITCH_HALTED
    assert int(outcome.exit_code) == 41
    assert outcome.phase is SupervisionPhase.HALTED
    assert outcome.clean is True
    assert outcome.detail == "kill switch engaged at boot"
    assert supervisor.kill_switch.engaged
    assert poller.polls == 1
    ledger = journal.read_ledger()
    assert ledger["consecutive_faults"] == 0
    assert _history(ledger)[0]["outcome"] == OutcomeKind.KILL_SWITCH.value
    assert journal.read_status()["phase"] == SupervisionPhase.HALTED.value


def test_kill_switch_gate_poll_fault_exits_restartable() -> None:
    store, journal, clock = _journal()
    poller = _RecordingPoller(error=OSError("kill-switch table unreachable"))
    supervisor = _supervisor(store, journal, clock, session=_session(), poller=poller)

    outcome = supervisor.run()

    assert outcome.exit_code is TraderExitCode.RESTARTABLE_FAULT
    assert int(outcome.exit_code) == 75
    assert outcome.phase is SupervisionPhase.FAULTED
    assert outcome.detail is not None
    assert "kill-switch gate poll failed" in outcome.detail
    assert "kill-switch table unreachable" in outcome.detail
    assert supervisor.kill_switch.engaged is False
    ledger = journal.read_ledger()
    assert ledger["consecutive_faults"] == 1
    assert _history(ledger)[0]["outcome"] == OutcomeKind.FAULT.value


def test_crash_loop_budget_refuses_to_boot_without_composing_a_feed() -> None:
    store, journal, clock = _journal()
    budget = SupervisorSettings().restart_budget
    for _ in range(budget):
        journal.record_boot_outcome(OutcomeKind.FAULT, "injected fault")
    factory = _RecordingFeedFactory(error=RuntimeError("must not be composed"))
    supervisor = _supervisor(
        store, journal, clock, session=_session(), feed_factory=factory
    )

    outcome = supervisor.run()

    assert outcome.exit_code is TraderExitCode.CRASH_LOOP
    assert int(outcome.exit_code) == 86
    assert outcome.phase is SupervisionPhase.CRASH_LOOP
    assert outcome.detail is not None
    assert "restart budget exhausted" in outcome.detail
    assert factory.calls == 0
    assert journal.read_status()["phase"] == SupervisionPhase.CRASH_LOOP.value
    assert journal.read_ledger()["consecutive_faults"] == budget + 1


def test_sessionless_boot_exits_clean_without_composing_a_feed() -> None:
    store, journal, clock = _journal()
    journal.record_boot_outcome(OutcomeKind.FAULT, "prior fault")
    factory = _RecordingFeedFactory(error=RuntimeError("must not be composed"))
    supervisor = _supervisor(store, journal, clock, feed_factory=factory)

    outcome = supervisor.run()

    assert outcome.exit_code is TraderExitCode.SUCCESS
    assert int(outcome.exit_code) == 0
    assert outcome.phase is SupervisionPhase.NO_SESSION
    assert outcome.clean is True
    assert outcome.session is None
    assert factory.calls == 0
    assert journal.read_status()["phase"] == SupervisionPhase.NO_SESSION.value
    ledger = journal.read_ledger()
    assert ledger["consecutive_faults"] == 0
    assert _history(ledger)[-1]["outcome"] == OutcomeKind.CLEAN.value


def test_feed_factory_faults_retry_with_backoff_then_exit_restartable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(sup, "sleep", lambda seconds: sleeps.append(seconds))
    store, journal, clock = _journal()
    factory = _RecordingFeedFactory(error=RuntimeError("websocket unavailable"))
    settings = SupervisorSettings(
        connect_attempts=2, connect_backoff=timedelta(seconds=2)
    )
    supervisor = _supervisor(
        store,
        journal,
        clock,
        session=_session(),
        feed_factory=factory,
        settings=settings,
    )

    outcome = supervisor.run()

    assert factory.calls == 2
    assert sleeps == [2.0]
    assert outcome.exit_code is TraderExitCode.RESTARTABLE_FAULT
    assert int(outcome.exit_code) == 75
    assert outcome.phase is SupervisionPhase.FAULTED
    assert outcome.detail is not None
    assert "market-data boundary could not be composed" in outcome.detail
    assert "websocket unavailable" in outcome.detail
    assert journal.read_status()["phase"] == SupervisionPhase.FAULTED.value
    assert journal.read_ledger()["consecutive_faults"] == 1


def test_unexpected_boot_fault_exits_failed_and_writes_the_status_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ExplodingShell:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("shell composition exploded")

    monkeypatch.setattr(sup, "PaperShell", _ExplodingShell)
    store, journal, clock = _journal()
    feed = _FakeFeed()
    factory = _RecordingFeedFactory(feed=feed)
    supervisor = _supervisor(
        store, journal, clock, session=_session(), feed_factory=factory
    )

    outcome = supervisor.run()

    assert outcome.exit_code is TraderExitCode.FAILED
    assert int(outcome.exit_code) == 1
    assert outcome.phase is SupervisionPhase.FAULTED
    assert outcome.detail is not None
    assert "shell composition exploded" in outcome.detail
    assert feed.closed is True
    assert journal.read_status()["phase"] == SupervisionPhase.FAULTED.value
    assert _history(journal.read_ledger())[-1]["outcome"] == OutcomeKind.FAULT.value


# --------------------------------------------------------------------------
# TableKillSwitchPoller: the deployment kill-switch source
# --------------------------------------------------------------------------


def test_table_poller_requires_an_azure_table_client() -> None:
    with pytest.raises(TypeError, match="TableClient"):
        TableKillSwitchPoller(client=cast(Any, object()))


def _poller_with(
    monkeypatch: pytest.MonkeyPatch, get_entity: Callable[..., Any]
) -> TableKillSwitchPoller:
    client = TableClient(
        "https://killswitch.table.core.windows.net",
        "killswitch",
        credential=AzureNamedKeyCredential("killswitch", "<placeholder-key>"),
    )
    monkeypatch.setattr(client, "get_entity", get_entity)
    return TableKillSwitchPoller(client=client)


def test_table_poller_treats_a_missing_row_as_disengaged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, str]] = []

    def get_entity(*, partition_key: str, row_key: str) -> Any:
        calls.append({"partition_key": partition_key, "row_key": row_key})
        raise ResourceNotFoundError("entity missing")

    poller = _poller_with(monkeypatch, get_entity)

    observation = poller.poll()

    assert observation.engaged is False
    assert observation.reason is None
    assert observation.at.tzinfo is not None
    assert calls == [
        {
            "partition_key": KILL_SWITCH_TABLE_PARTITION_KEY,
            "row_key": KILL_SWITCH_TABLE_ROW_KEY,
        }
    ]


def test_table_poller_reports_the_engaged_switch_and_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def get_entity(*, partition_key: str, row_key: str) -> dict[str, object]:
        assert (partition_key, row_key) == (
            KILL_SWITCH_TABLE_PARTITION_KEY,
            KILL_SWITCH_TABLE_ROW_KEY,
        )
        return {
            "PartitionKey": partition_key,
            "RowKey": row_key,
            "engaged": True,
            "reason": "operator halt",
        }

    poller = _poller_with(monkeypatch, get_entity)

    observation = poller.poll()

    assert observation.engaged is True
    assert observation.reason == "operator halt"


def test_table_poller_reports_a_blank_reason_as_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def get_entity(*, partition_key: str, row_key: str) -> dict[str, object]:
        return {"PartitionKey": partition_key, "RowKey": row_key, "engaged": False}

    poller = _poller_with(monkeypatch, get_entity)

    observation = poller.poll()

    assert observation.engaged is False
    assert observation.reason is None


def test_table_poller_propagates_unexpected_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def get_entity(*, partition_key: str, row_key: str) -> Any:
        raise RuntimeError("table throttled")

    poller = _poller_with(monkeypatch, get_entity)

    with pytest.raises(RuntimeError, match="table throttled"):
        poller.poll()


def test_from_environ_returns_none_without_a_contract() -> None:
    assert TableKillSwitchPoller.from_environ({}) is None


def test_from_environ_composes_the_shared_key_client() -> None:
    poller = TableKillSwitchPoller.from_environ(
        {
            "FCA_KILL_SWITCH_TABLE__ENDPOINT": "https://acct.table.core.windows.net/",
            "FCA_KILL_SWITCH_TABLE__KEY": "<placeholder-key>",
            "HARNESS_KILL_SWITCH_TABLE": "ops-killswitch",
        }
    )

    assert poller is not None
    client = poller._client
    assert client.account_name == "acct"
    assert client.table_name == "ops-killswitch"
    assert client.url == "https://acct.table.core.windows.net"


def test_from_environ_composes_the_managed_identity_client() -> None:
    poller = TableKillSwitchPoller.from_environ(
        {
            "LAKE_STORAGE_ACCOUNT_NAME": "storageacct",
            "AZURE_CLIENT_ID": "00000000-0000-0000-0000-000000000000",
        }
    )

    assert poller is not None
    client = poller._client
    assert client.account_name == "storageacct"
    assert client.table_name == "killswitch"


# --------------------------------------------------------------------------
# Settings composition from the deployment environment
# --------------------------------------------------------------------------


def test_settings_from_environment_defaults() -> None:
    assert trader._settings_from_environment({}) == SupervisorSettings()


def test_settings_from_environment_maps_deployment_variables() -> None:
    settings = trader._settings_from_environment(
        {
            "HARNESS_TRADER_BOOT_POLICY": "halt_only",
            "HARNESS_TRADER_INITIAL_CASH": "250000.00",
            "HARNESS_TRADER_HEARTBEAT_SECONDS": "30",
            "HARNESS_TRADER_RECONCILE_MINUTES": "5",
            "HARNESS_TRADER_RESTART_BUDGET": "7",
            "HARNESS_TRADER_CONNECT_ATTEMPTS": "4",
        }
    )

    assert settings.boot_policy == "halt_only"
    assert settings.initial_cash == Decimal("250000.00")
    assert settings.heartbeat_cadence == timedelta(seconds=30)
    assert settings.reconcile_cadence == timedelta(minutes=5)
    assert settings.restart_budget == 7
    assert settings.connect_attempts == 4


@pytest.mark.parametrize(
    ("environ", "message"),
    [
        ({"HARNESS_TRADER_BOOT_POLICY": "bogus"}, "invalid trader supervision"),
        ({"HARNESS_TRADER_HEARTBEAT_SECONDS": "0"}, "positive number of seconds"),
        ({"HARNESS_TRADER_RESTART_BUDGET": "0"}, "invalid trader supervision"),
    ],
)
def test_settings_from_environment_rejects_invalid_values(
    environ: dict[str, str], message: str
) -> None:
    with pytest.raises(SystemExit, match=message):
        trader._settings_from_environment(environ)


def test_resolve_credentials_keys_by_environment_variable_name() -> None:
    configuration = _trader_config()
    resolved = trader._resolve_credentials(
        configuration,
        {
            "HARNESS_ALPACA_API_KEY": "key-value",
            "HARNESS_ALPACA_API_SECRET": "secret-value",
        },
    )
    assert resolved == {
        "HARNESS_ALPACA_API_KEY": "key-value",
        "HARNESS_ALPACA_API_SECRET": "secret-value",
    }


def test_resolve_credentials_rejects_unset_environment_contract() -> None:
    configuration = _trader_config()
    market_data = configuration.market_data.model_copy(
        update={"credentials": {"api_key": "${HARNESS_ALPACA_API_KEY}"}}
    )
    single_reference = configuration.model_copy(update={"market_data": market_data})
    with pytest.raises(SystemExit, match="unset environment variables"):
        trader._resolve_credentials(
            single_reference,
            {"HARNESS_ALPACA_API_KEY": "key-value"},
        )


def test_session_for_crypto_supervises_the_continuous_utc_day() -> None:
    configuration = _trader_config()
    market_data = configuration.market_data.model_copy(
        update={"settings": {"feed": "crypto"}}
    )
    crypto_config = configuration.model_copy(update={"market_data": market_data})
    # 21:30 UTC is after the XNYS close: the equity path would be no_session.
    clock = SimClock(datetime(2026, 9, 8, 21, 30, tzinfo=UTC))

    session = trader._session_for(crypto_config, clock)

    assert session is not None
    assert session.open_time == datetime(2026, 9, 8, 0, 0, tzinfo=UTC)
    assert session.close_time == datetime(2026, 9, 9, 0, 0, tzinfo=UTC)
    assert session.open_time <= clock.now() < session.close_time


def test_session_for_equity_feed_delegates_to_the_xnys_calendar() -> None:
    configuration = _trader_config()
    saturday = SimClock(datetime(2026, 9, 5, 15, 0, tzinfo=UTC))

    assert trader._session_for(configuration, saturday) is None


def test_feed_factory_passes_the_configured_crypto_websocket_url() -> None:
    configuration = _trader_config()
    instance = _instance().model_copy(update={"symbols": ("BTC/USD",)})
    market_data = configuration.market_data.model_copy(
        update={
            "adapter": "harness.data.alpaca_ws:AlpacaCryptoWebSocketFeed",
            "settings": {
                "feed": "crypto",
                "websocket_url": "wss://stream.example.test/v1beta3/crypto/us",
            },
        }
    )
    crypto_config = configuration.model_copy(
        update={"strategy_instances": (instance,), "market_data": market_data}
    )

    _paper_feed, pump = trader._feed_factory(
        crypto_config,
        {
            "HARNESS_ALPACA_API_KEY": "key-value",
            "HARNESS_ALPACA_API_SECRET": "secret-value",
        },
        SimClock(datetime(2026, 9, 9, 12, 0, tzinfo=UTC)),
    )

    assert pump._websocket_url == "wss://stream.example.test/v1beta3/crypto/us"


# --------------------------------------------------------------------------
# ReorderingInbox: the live feed ordering boundary
# --------------------------------------------------------------------------


class _ScriptedInbox:
    """Inner inbox serving a fixed frame sequence, then idling."""

    def __init__(self, events: Sequence[QuoteEvent]) -> None:
        self._events = list(events)

    def pop(self) -> QuoteEvent | None:
        return self._events.pop(0) if self._events else None


def _stamp(event: QuoteEvent) -> datetime:
    return event.timestamp


def test_reordering_inbox_releases_out_of_order_frames_in_kernel_order() -> None:
    late_first = _quote()
    early_next = _quote()
    reordered = replace(early_next, timestamp=OPEN + timedelta(minutes=1, seconds=-2))
    inbox = sup.ReorderingInbox(
        _ScriptedInbox([late_first, reordered]), lateness=timedelta(seconds=3)
    )

    released = [
        inbox.pop(),
        inbox.pop(),
        inbox.pop(),
    ]

    assert None not in released[:2]
    stamps = [event.timestamp for event in released[:2] if event is not None]
    assert stamps == sorted(stamps)
    assert released[2] is None
    assert inbox.dropped_late == 0


def test_reordering_inbox_drops_beyond_tolerance_late_events() -> None:
    first = _quote()
    far_late = _quote()
    straggler = _quote()
    on_time = _quote()
    base = OPEN + timedelta(minutes=1)
    first = replace(first, timestamp=base)
    far_late = replace(far_late, timestamp=base + timedelta(minutes=10))
    straggler = replace(straggler, timestamp=base - timedelta(seconds=30))
    on_time = replace(on_time, timestamp=base + timedelta(minutes=10, seconds=1))
    inbox = sup.ReorderingInbox(
        _ScriptedInbox([first, far_late, straggler, on_time]),
        lateness=timedelta(seconds=2),
    )

    released = []
    while True:
        event = inbox.pop()
        if event is None:
            break
        released.append(event)

    stamps = [event.timestamp for event in released]
    assert stamps == sorted(stamps)
    assert straggler not in released
    assert inbox.dropped_late == 1


def test_reordering_inbox_rejects_invalid_construction() -> None:
    with pytest.raises(ValueError, match="lateness"):
        sup.ReorderingInbox(_ScriptedInbox([]), lateness=timedelta(seconds=-1))
    with pytest.raises(ValueError, match="capacity"):
        sup.ReorderingInbox(_ScriptedInbox([]), capacity=0)
    with pytest.raises(TypeError, match="pop"):
        sup.ReorderingInbox(object())  # ty: ignore[invalid-argument-type]


# --------------------------------------------------------------------------
# harness trader status: the supervision status command
# --------------------------------------------------------------------------


def _seed_supervision_document() -> tuple[MemorySnapshotStore, SessionJournal]:
    store, journal, clock = _journal()
    shell = PaperShell(
        _trader_config(),
        options=PaperOptions(
            run_id=RUN_ID,
            initial_cash=CASH,
            session_policy=PaperSessionPolicy.STOP_AT_CLOSE,
            session=_session(),
        ),
        wall_clock=clock,
    )
    shell.step(_quote())
    journal.record_status(
        phase=SupervisionPhase.RUNNING,
        detail="heartbeat",
        shell=shell,
        heartbeat=True,
    )
    return store, journal


def test_trader_status_reports_the_supervision_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _example_yaml("trader.yaml", tmp_path)
    store, _ = _seed_supervision_document()
    monkeypatch.setenv("HARNESS_TRADER_RUN_ID", RUN_ID)
    monkeypatch.setattr(trader, "_object_store", lambda run_id: store)

    result = runner.invoke(trader.app, ["status", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["run_id"] == RUN_ID
    assert document["phase"] == "running"
    assert document["last_heartbeat_at"] == NOW.isoformat(timespec="microseconds")
    assert document["last_event_time"] == (OPEN + timedelta(minutes=1)).isoformat(
        timespec="microseconds"
    )
    assert document["books"] == [
        {
            "book_id": "solo",
            "initial_cash": "100000.000",
            "cash": "100000.000",
            "equity": "100000.000",
            "positions": [],
        }
    ]


def test_trader_status_reports_a_missing_supervision_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _example_yaml("trader.yaml", tmp_path)
    monkeypatch.setenv("HARNESS_TRADER_RUN_ID", RUN_ID)
    monkeypatch.setattr(
        trader,
        "_object_store",
        lambda run_id: MemorySnapshotStore(),
    )

    result = runner.invoke(trader.app, ["status", "--config", str(config_path)])

    assert result.exit_code == 1
    assert f"no supervision record for run {RUN_ID}" in result.stdout


def test_trader_status_rejects_a_non_trader_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _example_yaml("backtest.yaml", tmp_path)
    monkeypatch.setenv("HARNESS_TRADER_RUN_ID", RUN_ID)

    result = runner.invoke(trader.app, ["status", "--config", str(config_path)])

    assert result.exit_code == 2
    assert "trader document" in result.stderr
