"""Focused tests for the E6.5 universe expansion and valve soak configs.

Fixed fixtures only: no network, no clock, no randomness. The soak driver
module is loaded by file path because scripts/soak is a script directory,
not an installed package.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from lake.configuration.loader import load_yaml_mapping
from lake.contracts import symbol_filename

REPO_ROOT = Path(__file__).resolve().parents[2]
UNIVERSE_PATH = REPO_ROOT / "config" / "lake" / "universe-equities-200.yaml"
TRADES_PATH = REPO_ROOT / "config" / "lake" / "soak-valve-trades-200.yaml"
QUOTES_PATH = REPO_ROOT / "config" / "lake" / "soak-valve-quotes-top25.yaml"
LEGACY_PATH = REPO_ROOT / "config" / "lake" / "soak-aapl-spy.yaml"

# Mirror of the canonical symbol pattern in
# src/lake/configuration/universes.py (_EQUITY_SYMBOL); filename acceptance
# is additionally checked against the production validator below.
SYMBOL_PATTERN = re.compile(r"[A-Z][A-Z0-9.-]*", re.ASCII)


def _load_run_soak() -> Any:
    spec = importlib.util.spec_from_file_location(
        "e65_run_soak", REPO_ROOT / "scripts" / "soak" / "run_soak.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


soak = _load_run_soak()


def _universe() -> dict[str, Any]:
    document = load_yaml_mapping(UNIVERSE_PATH)
    assert isinstance(document["universe"], dict)
    return document["universe"]


def _symbols() -> list[str]:
    return _universe()["symbols"]


def test_universe_is_200_unique_canonical_symbols() -> None:
    symbols = _symbols()
    assert len(symbols) == 200
    assert _universe()["count"] == 200
    assert len(set(symbols)) == 200
    for symbol in symbols:
        assert SYMBOL_PATTERN.fullmatch(symbol) is not None, symbol
        symbol_filename(symbol)


def test_universe_head_block_matches_the_valve_anchor() -> None:
    symbols = _symbols()
    # T050 handoff anchor: SPY, QQQ, AAPL head the ranked list.
    assert symbols[:3] == ["SPY", "QQQ", "AAPL"]
    assert symbols[25] != symbols[24]


def test_universe_documents_the_valve_derivation() -> None:
    ranking = _universe()["ranking"]
    assert ranking["snapshot_date"] == "2026-09-09"
    assert "2026-09-09" in ranking["single_name_source"]
    assert ranking["order"] == "newest liquidity rank first, rank equals list position"
    assert "e6.4-cost-projection.md" in ranking["valve_ref"]
    assert "top-25" in ranking["valve_ref"]


def test_valve_trades_config_covers_the_full_universe() -> None:
    config = soak.load_soak_config(TRADES_PATH)
    assert config.soak_id == "e65-valve-trades-200"
    assert config.data_types == ("trade",)
    assert config.symbols == tuple(_symbols())
    assert len(config.symbols) == 200
    assert config.default_start == date(2016, 1, 1)


def test_valve_quotes_config_is_the_top25_head() -> None:
    config = soak.load_soak_config(QUOTES_PATH)
    assert config.soak_id == "e65-valve-quotes-top25"
    assert config.data_types == ("quote",)
    assert config.symbols == tuple(_symbols()[:25])
    assert len(config.symbols) == 25


def test_valve_windows_match_the_t050_history_policy() -> None:
    trades = soak.load_soak_config(TRADES_PATH)
    quotes = soak.load_soak_config(QUOTES_PATH)
    assert trades.default_start == date(2016, 1, 1)
    assert quotes.default_start == date(2025, 9, 9)
    # One-year quote window pinned at the 2026-09-09 snapshot date.
    assert (date(2026, 9, 9) - quotes.default_start).days == 365
    assert quotes.default_start > trades.default_start


def test_estimate_covers_every_configured_symbol() -> None:
    trades = soak.load_soak_config(TRADES_PATH)
    quotes = soak.load_soak_config(QUOTES_PATH)
    assert set(trades.estimate.assumed_bytes) == set(trades.symbols)
    for symbol in trades.symbols:
        assert trades.estimate.assumed_bytes[symbol].keys() == {"trade"}
        assert trades.estimate.assumed_bytes[symbol]["trade"] > 0
    assert set(quotes.estimate.assumed_bytes) == set(quotes.symbols)
    for symbol in quotes.symbols:
        assert quotes.estimate.assumed_bytes[symbol].keys() == {"quote"}
        assert quotes.estimate.assumed_bytes[symbol]["quote"] > 0


def test_valve_run_paths_are_quarantined_from_t049() -> None:
    trades = soak.load_soak_config(TRADES_PATH)
    quotes = soak.load_soak_config(QUOTES_PATH)
    legacy = soak.load_soak_config(LEGACY_PATH)
    valve_paths = {
        trades.paths.state_file,
        trades.paths.results_file,
        trades.paths.summary_file,
        quotes.paths.state_file,
        quotes.paths.results_file,
        quotes.paths.summary_file,
        legacy.paths.state_file,
        legacy.paths.results_file,
        legacy.paths.summary_file,
    }
    assert len(valve_paths) == 9
    for path in (
        trades.paths.state_file,
        trades.paths.results_file,
        trades.paths.summary_file,
        quotes.paths.state_file,
        quotes.paths.results_file,
        quotes.paths.summary_file,
    ):
        assert path.as_posix().startswith(".local/soak/")
        assert "e65" in path.name


def test_valve_cost_blocks_match_the_measured_soak_config() -> None:
    trades = soak.load_soak_config(TRADES_PATH)
    quotes = soak.load_soak_config(QUOTES_PATH)
    legacy = soak.load_soak_config(LEGACY_PATH)
    assert trades.cost == legacy.cost
    assert quotes.cost == legacy.cost
    assert trades.items_per_page == legacy.items_per_page
    assert trades.run == legacy.run
    assert quotes.run == legacy.run


def test_config_loader_regression_legacy_config_unchanged() -> None:
    config = soak.load_soak_config(LEGACY_PATH)
    assert config.soak_id == "e6-full-history-aapl-spy"
    assert config.symbols == ("AAPL", "SPY")
    assert config.data_types == ("trade", "quote")
    assert config.default_start == date(2016, 1, 1)
    assert config.paths.state_file == Path(".local/soak/state.json")
    assert config.estimate.assumed_bytes["AAPL"] == {
        "trade": 1_500_000,
        "quote": 30_000_000,
    }
    assert config.estimate.assumed_bytes["SPY"] == {
        "trade": 6_000_000,
        "quote": 80_000_000,
    }


def test_newest_first_session_planning_excludes_open_session() -> None:
    sessions = soak.plan_closed_sessions(
        date(2026, 8, 31), date(2026, 9, 9), datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    )
    # 2026-09-07 is Labor Day; 2026-09-09 has not closed at the fixed as-of.
    assert sessions == (
        date(2026, 9, 8),
        date(2026, 9, 4),
        date(2026, 9, 3),
        date(2026, 9, 2),
        date(2026, 9, 1),
        date(2026, 8, 31),
    )


def test_dry_run_plans_one_session_per_symbol_stream() -> None:
    sessions = (date(2026, 9, 8),)
    trades = soak.load_soak_config(TRADES_PATH)
    plan = soak.dry_run(trades, trades.symbols, sessions, soak.SoakState())
    assert plan["mode"] == "dry-run"
    assert plan["window"]["closed_sessions"] == 1
    assert plan["already_completed"] == 0
    assert len(plan["plan"]) == 200
    assert set(plan["plan"].values()) == {1}
    assert plan["projected_cost"]["measured"]["sessions_complete"] == 200
    assert plan["projected_cost"]["measured"]["bytes"] == 200 * 6_000_000

    quotes = soak.load_soak_config(QUOTES_PATH)
    plan = soak.dry_run(quotes, quotes.symbols, sessions, soak.SoakState())
    assert len(plan["plan"]) == 25
    assert set(plan["plan"].values()) == {1}
    assert plan["projected_cost"]["measured"]["sessions_complete"] == 25
    assert plan["projected_cost"]["measured"]["bytes"] == 25 * 80_000_000


def test_dry_run_full_history_projection_reproduces_the_valve_cor() -> None:
    # At the T050 valve planning bytes, the planned corpus for the two valve
    # arms must land on the 806.4 GB envelope that produced the 7.69 USD/month
    # projection in docs/e6.4-cost-projection.md.
    trades = soak.load_soak_config(TRADES_PATH)
    quotes = soak.load_soak_config(QUOTES_PATH)
    sessions = tuple(date(2026, 9, 1) for _ in range(252))
    assert len(sessions) == 252  # 1.0 years at 252 sessions/year
    trades_plan = soak.dry_run(trades, trades.symbols, sessions, soak.SoakState())
    quotes_plan = soak.dry_run(quotes, quotes.symbols, sessions, soak.SoakState())
    corpus_bytes = (
        trades_plan["projected_cost"]["measured"]["bytes"]
        + quotes_plan["projected_cost"]["measured"]["bytes"]
    )
    assert corpus_bytes == 200 * 252 * 6_000_000 + 25 * 252 * 80_000_000
    assert corpus_bytes == 806_400_000_000
