"""Trader process boundary for the supervised paper and shadow runtimes.

This module is the deployment composition between the quant-harness runtime
capabilities and the process the platform schedules.  ``trader run`` loads a
trader configuration, resolves credential references from the environment,
builds the supervision stack (boot policy, reconciler, state store, kill
switch), and drives one supervised session to a terminal outcome.  The
process exit code is the supervision exit code the platform observes.

Supported market-data adapters:

- ``harness.data.alpaca_ws:AlpacaSipWebSocketFeed`` (``feed: sip``)
- ``harness.data.alpaca_ws:AlpacaCryptoWebSocketFeed`` (``feed: crypto``)

The feed runs on a dedicated event-loop thread and pushes into a synchronous
inbox; the supervised shell pops from that inbox through the
:class:`~harness.runtime.paper.InboxPaperFeed` live boundary.  Network access
stays outside the deterministic kernel exactly as in backtests.
"""

from __future__ import annotations

import asyncio
import os
import signal
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import typer

from harness.cli._runner import run
from harness.cli.supervisor import (
    ReorderingInbox,
    SessionJournal,
    SupervisorSettings,
    TableKillSwitchPoller,
    TraderSupervisor,
    trading_session_for,
)
from harness.configuration import (
    TraderConfig,
    load_configuration,
    resolve_environment_reference,
)
from harness.core.clock import Clock, WallClock
from harness.data.alpaca_ws import (
    AlpacaCryptoInbox,
    AlpacaCryptoWebSocketFeed,
    AlpacaSipInbox,
    AlpacaSipWebSocketFeed,
)
from harness.risk.kill_switch import KillSwitch
from harness.runtime.paper import InboxPaperFeed
from harness.runtime.state_store import RuntimeStateStore, SnapshotObjectStore
from lake.contracts import ExchangeSession
from lake.storage.azure import AzureReadableBlobStore
from lake.storage.parquet import LocalFilesystemStore

app = typer.Typer(add_completion=False, no_args_is_help=False)

_ZERO = timedelta(0)

_CREDENTIAL_ENV_KEYS = {
    "HARNESS_ALPACA_API_KEY",
    "HARNESS_ALPACA_API_SECRET",
}

_SETTING_DURATIONS = {
    "HARNESS_TRADER_HEARTBEAT_SECONDS": "heartbeat_cadence",
    "HARNESS_TRADER_KILL_SWITCH_POLL_SECONDS": "kill_switch_poll",
    "HARNESS_TRADER_SNAPSHOT_CADENCE_SECONDS": "snapshot_cadence",
    "HARNESS_TRADER_RECONCILE_MINUTES": "reconcile_cadence",
    "HARNESS_TRADER_MARKET_WAIT_MINUTES": "market_wait",
    "HARNESS_TRADER_IDLE_PACE_SECONDS": "idle_pace",
    "HARNESS_TRADER_CONNECT_BACKOFF_SECONDS": "connect_backoff",
}

_SETTING_POSITIVE_INTEGERS = {
    "HARNESS_TRADER_KILL_SWITCH_STALE": "kill_switch_stale",
    "HARNESS_TRADER_CONNECT_ATTEMPTS": "connect_attempts",
    "HARNESS_TRADER_RESTART_BUDGET": "restart_budget",
}


def _resolve_credentials(
    config: TraderConfig, environ: dict[str, str] | None = None
) -> dict[str, str]:
    """Resolve the configured market-data credential references.

    The returned mapping is keyed by the referenced environment-variable
    name (``HARNESS_ALPACA_API_KEY``), not by the configuration field name,
    so the feed composition reads credentials by their deployment contract.
    """
    from harness.configuration import environment_variable_name

    resolved: dict[str, str] = {}
    for reference in config.market_data.credentials.values():
        variable = environment_variable_name(reference)
        resolved[variable] = resolve_environment_reference(
            reference, environ
        ).get_secret_value()
    missing = _CREDENTIAL_ENV_KEYS - set(resolved)
    if missing:
        raise SystemExit(
            f"trader market-data credentials reference unset environment "
            f"variables: {sorted(missing)}"
        )
    return resolved


def _settings_from_environment(
    environ: dict[str, str] | None = None,
) -> SupervisorSettings:
    """Build the supervision knobs from the deployment environment."""
    values = os.environ if environ is None else environ
    kwargs: dict[str, Any] = {}
    boot_policy = values.get("HARNESS_TRADER_BOOT_POLICY")
    if boot_policy is not None:
        kwargs["boot_policy"] = boot_policy
    initial_cash = values.get("HARNESS_TRADER_INITIAL_CASH")
    if initial_cash is not None:
        kwargs["initial_cash"] = Decimal(initial_cash)
    for variable, field_name in _SETTING_DURATIONS.items():
        raw = values.get(variable)
        if raw is None:
            continue
        seconds = float(raw)
        if seconds <= 0:
            raise SystemExit(f"{variable} must be a positive number of seconds")
        if field_name == "reconcile_cadence":
            kwargs[field_name] = timedelta(minutes=seconds)
        else:
            kwargs[field_name] = timedelta(seconds=seconds)
    for variable, field_name in _SETTING_POSITIVE_INTEGERS.items():
        raw = values.get(variable)
        if raw is not None:
            kwargs[field_name] = int(raw)
    try:
        return SupervisorSettings(**kwargs)
    except (TypeError, ValueError) as error:
        raise SystemExit(f"invalid trader supervision settings: {error}") from error


def _object_store(run_id: str) -> SnapshotObjectStore:
    """Select the state object store from the deployment environment."""
    account = os.environ.get("LAKE_STORAGE_ACCOUNT_NAME")
    container = os.environ.get("HARNESS_STATE_CONTAINER", "harness-state")
    if account:
        from azure.identity import DefaultAzureCredential

        return AzureReadableBlobStore.from_account_url(
            f"https://{account}.blob.core.windows.net",
            container,
            credential=DefaultAzureCredential(),
        )
    return LocalFilesystemStore(Path(".local") / "trader-state" / run_id)


def _state_store(run_id: str) -> RuntimeStateStore:
    """Bind the run's snapshot store."""
    return RuntimeStateStore(_object_store(run_id), run_id=run_id)


def _kill_switch_poller() -> Any | None:
    """Compose the kill-switch poller from the documented environment."""

    return TableKillSwitchPoller.from_environ()


def _websocket_transport() -> Any:
    """Build the concrete WebSocket transport for the live feed boundary."""
    try:
        import websockets
    except ImportError as error:  # pragma: no cover - deployment dependency
        raise SystemExit(
            "the 'websockets' package is required for live feed connections; "
            "install it into the runtime environment"
        ) from error

    class _Connection:
        def __init__(self, connection: Any) -> None:
            self._connection = connection

        async def send(self, message: str) -> None:
            await self._connection.send(message)

        async def receive(self, timeout_seconds: float) -> str | bytes | None:
            try:
                return await asyncio.wait_for(
                    self._connection.recv(), timeout=timeout_seconds
                )
            except TimeoutError:
                return None

        async def close(self) -> None:
            await self._connection.close()

    class _Transport:
        async def connect(self, url: str) -> Any:
            return _Connection(await websockets.connect(url))

    return _Transport()


def _feed_factory(
    config: TraderConfig,
    resolved: dict[str, str],
    clock: Clock,
) -> tuple[InboxPaperFeed, Any]:
    """Compose the configured live feed and its synchronous inbox bridge.

    Returns the feed handed to the supervisor together with the runnable
    async pump that must execute on the event-loop thread.
    """
    adapter = config.market_data.adapter
    settings = dict(config.market_data.settings)
    websocket_url = settings.get("websocket_url")
    if websocket_url is not None and not isinstance(websocket_url, str):
        raise SystemExit("market_data.settings.websocket_url must be a string")
    if settings.get("feed") == "crypto":
        if adapter != "harness.data.alpaca_ws:AlpacaCryptoWebSocketFeed":
            raise SystemExit(
                f"crypto feed configuration requires the crypto adapter, got {adapter!r}"
            )
        inbox: AlpacaSipInbox = AlpacaCryptoInbox()
        feed = AlpacaCryptoWebSocketFeed.from_trader_config(
            config,
            api_key=resolved["HARNESS_ALPACA_API_KEY"],
            api_secret=resolved["HARNESS_ALPACA_API_SECRET"],
            transport=_websocket_transport(),
            inbox=inbox,
            clock=lambda: clock.now(),
            websocket_url=websocket_url,
        )
    elif adapter == "harness.data.alpaca_ws:AlpacaSipWebSocketFeed":
        inbox = AlpacaSipInbox()
        feed = AlpacaSipWebSocketFeed.from_trader_config(
            config,
            api_key=resolved["HARNESS_ALPACA_API_KEY"],
            api_secret=resolved["HARNESS_ALPACA_API_SECRET"],
            transport=_websocket_transport(),
            inbox=inbox,
            clock=lambda: clock.now(),
            websocket_url=websocket_url,
        )
    else:
        raise SystemExit(
            f"unsupported market-data adapter {adapter!r}; supported adapters: "
            "harness.data.alpaca_ws:AlpacaSipWebSocketFeed (feed: sip), "
            "harness.data.alpaca_ws:AlpacaCryptoWebSocketFeed (feed: crypto)"
        )
    return InboxPaperFeed(ReorderingInbox(inbox)), feed


def _run_feed_thread(
    feed: Any, stop: threading.Event
) -> tuple[threading.Thread, Callable[[], None]]:
    """Run the async feed pump on a dedicated thread until ``stop`` is set."""
    loop = asyncio.new_event_loop()
    async_stop = asyncio.Event()

    def _watch_stop() -> None:
        while not stop.is_set():
            if stop.wait(0.2):
                loop.call_soon_threadsafe(async_stop.set)

    def _thread_main() -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(feed.run(async_stop))
        except Exception as error:  # pragma: no cover - boundary fault logging
            print(f"trader feed boundary fault: {error}", flush=True)
        finally:
            loop.call_soon_threadsafe(async_stop.set)
            loop.close()

    thread = threading.Thread(target=_thread_main, name="trader-feed", daemon=True)
    thread.start()
    watcher = threading.Thread(target=_watch_stop, name="trader-stop", daemon=True)
    watcher.start()

    def _shutdown() -> None:
        stop.set()
        thread.join(timeout=30.0)
        watcher.join(timeout=1.0)

    return thread, _shutdown


def _session_for(configuration: TraderConfig, clock: Clock) -> ExchangeSession | None:
    """Resolve the supervised session for the configured market data.

    XNYS equity feeds trade the exchange calendar, so sessions come from
    :func:`trading_session_for`.  Crypto trades continuously across every
    UTC day: a crypto feed supervises one UTC-day session containing the
    clock instant, so boots outside equity hours still run a real session.
    """
    if configuration.market_data.settings.get("feed") == "crypto":
        now = clock.now()
        open_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return ExchangeSession(now.date(), open_time, open_time + timedelta(days=1))
    return trading_session_for(clock)


def _compose(
    config_path: str,
) -> tuple[TraderSupervisor, Callable[[], None], datetime]:
    """Load configuration and compose the supervision stack."""
    configuration = load_configuration(config_path)
    if configuration.shell != "trader":
        raise SystemExit(
            f"{config_path} configures the {configuration.shell!r} shell; "
            "the trader process requires a trader document"
        )
    if not isinstance(configuration, TraderConfig):
        raise SystemExit("trader documents must validate as TraderConfig")
    resolved = _resolve_credentials(configuration)
    clock = WallClock()
    settings = _settings_from_environment()
    run_id = os.environ.get(
        "HARNESS_TRADER_RUN_ID",
        f"trader-{configuration.deployment.name}-"
        f"{clock.now().astimezone(UTC).date().isoformat()}",
    )
    store = _state_store(run_id)
    journal = SessionJournal(store=_object_store(run_id), run_id=run_id, clock=clock)
    feed, pump = _feed_factory(configuration, resolved, clock)
    stop = threading.Event()

    def feed_factory() -> InboxPaperFeed:
        return feed

    thread, shutdown = _run_feed_thread(pump, stop)
    supervisor = TraderSupervisor(
        configuration=configuration,
        settings=settings,
        session=_session_for(configuration, clock),
        run_id=run_id,
        state_store=store,
        journal=journal,
        clock=clock,
        feed_factory=feed_factory,
        kill_switch=KillSwitch(),
        kill_switch_poller=_kill_switch_poller(),
    )

    def shutdown_with_supervisor() -> None:
        supervisor.request_stop("process termination requested")
        shutdown()

    return supervisor, shutdown_with_supervisor, clock.now()


@app.callback(invoke_without_command=True)
def launch(
    ctx: typer.Context,
    config: str = typer.Option(
        "config/harness/trader.yaml",
        "--config",
        help="Path to the trader configuration document.",
    ),
) -> None:
    """Run one supervised paper or shadow trading session to completion."""
    if ctx.invoked_subcommand is not None:
        return
    supervisor, shutdown, _ = _compose(config)

    def _handle_termination(signum: int, frame: Any) -> None:
        supervisor.request_stop(f"signal {signum}")

    for signal_name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, signal_name):
            signal.signal(getattr(signal, signal_name), _handle_termination)
    outcome = supervisor.run()
    shutdown()
    raise typer.Exit(code=int(outcome.exit_code))


@app.command()
def status(
    config: str = typer.Option(
        "config/harness/trader.yaml",
        "--config",
        help="Path to the trader configuration document.",
    ),
) -> None:
    """Print the latest supervision document for the configured run."""
    configuration = load_configuration(config)
    if configuration.shell != "trader" or not isinstance(configuration, TraderConfig):
        raise typer.BadParameter("configuration must be a trader document")
    clock = WallClock()
    run_id = os.environ.get(
        "HARNESS_TRADER_RUN_ID",
        f"trader-{configuration.deployment.name}-"
        f"{clock.now().astimezone(UTC).date().isoformat()}",
    )
    store = _object_store(run_id)
    try:
        payload = store.read(f"runs/{run_id}/supervision.json")
    except (KeyError, OSError):
        typer.echo(f"no supervision record for run {run_id}")
        raise typer.Exit(code=1) from None
    typer.echo(payload.decode("utf-8"))


def main(argv: Sequence[str] | None = None) -> int:
    """Run the standalone trader process command.

    ``trader`` is wired to the Rust entrypoint switch but stays on the Python
    engine: the Rust live trader supervision runner is not ported, so it is
    absent from ``PORTED_ENTRYPOINTS`` and ``exec_rust_entrypoint`` returns
    ``None``. Wiring it here keeps the flip a one-line gate change once the
    decommission criteria are met.
    """
    from harness.cli.rust_entrypoints import exec_rust_entrypoint, resolved_argv

    args = resolved_argv(argv)
    rust_result = exec_rust_entrypoint("trader", args)
    if rust_result is not None:
        return rust_result
    return run(app, prog_name="trader", argv=args)
