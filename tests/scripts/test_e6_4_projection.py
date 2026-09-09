"""Focused tests for the E6.4 cost projection model math.

Fixed fixtures only: no network, no clock, no randomness.  The module
under test is loaded by file path because scripts/cost is a script
directory, not an installed package.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "scripts" / "cost" / "e6_4_projection.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("e6_4_projection", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


m = _load_module()

# Clean-room fixtures: zero base storage, exact byte counts, a measured
# baseline whose derived per-session values are exact at 3 decimals.
MEASURED = m.MeasuredInputs(
    lake_container_bytes=0,
    soak_sessions_trade=1_000,
    soak_bytes_trade=13_413_000,
    soak_sessions_quote=1_000,
    soak_bytes_quote=10_186_000,
    soak_api_seconds_total=2_240.3,
    soak_api_requests=1_000,
)

GATES = m.Gates()

ONE_YEAR_SESSIONS = 252
SESSIONS_PER_MONTH = 21


def test_linear_extrapolation_storage() -> None:
    scenario = m.Scenario(
        name="fixture",
        symbols=2,
        quote_symbols=1,
        history_years=1.0,
        plan_trade_bytes_per_session=6_000_000.0,
        plan_quote_bytes_per_session=80_000_000.0,
    )
    p = m.project(scenario, MEASURED)
    expected_bytes = 2 * ONE_YEAR_SESSIONS * 6_000_000 + ONE_YEAR_SESSIONS * 80_000_000
    assert p.sessions_per_symbol == ONE_YEAR_SESSIONS
    assert p.projected_cool_storage_bytes == expected_bytes
    expected_gb = expected_bytes / m.GIB
    assert p.projected_cool_storage_gb == expected_gb
    assert p.cool_storage_usd_month == expected_gb * m.COOL_STORED_USD_PER_GB_MONTH


def test_session_and_monthly_volume_scaling() -> None:
    scenario = m.Scenario(
        name="fixture",
        symbols=3,
        quote_symbols=2,
        history_years=2.0,
    )
    p = m.project(scenario, MEASURED)
    assert p.sessions_per_symbol == 2 * ONE_YEAR_SESSIONS
    assert p.monthly_sessions == (3 + 2) * SESSIONS_PER_MONTH


def test_full_depth_vs_bounded_bytes() -> None:
    full = m.project(
        m.Scenario(name="full", symbols=1, quote_symbols=0, history_years=1.0),
        MEASURED,
    )
    bounded = m.project(
        m.Scenario(
            name="bounded",
            symbols=1,
            quote_symbols=0,
            history_years=1.0,
            full_depth=False,
        ),
        MEASURED,
    )
    assert full.trade_bytes_per_session == 6_000_000.0
    assert bounded.trade_bytes_per_session == 13_413.0
    assert bounded.quote_bytes_per_session == 10_186.0


def test_monthly_compute_and_requests_math() -> None:
    scenario = m.Scenario(name="fixture", symbols=1, quote_symbols=0, history_years=1.0)
    p = m.project(scenario, MEASURED)
    per_second = 0.25 * m.CA_VCPU_USD_PER_SECOND + 0.5 * m.CA_MEMORY_USD_PER_GIB_SECOND
    expected_compute = SESSIONS_PER_MONTH * 2.2403 * per_second
    expected_requests = SESSIONS_PER_MONTH / 1_000_000 * m.CA_REQUESTS_USD_PER_MILLION
    expected_write = SESSIONS_PER_MONTH / 10_000 * m.COOL_WRITE_USD_PER_10K
    expected_read = SESSIONS_PER_MONTH / 10_000 * m.COOL_READ_USD_PER_10K
    assert p.container_apps_usd_month == expected_compute + expected_requests
    assert p.cool_write_usd_month == expected_write
    assert p.cool_read_usd_month == expected_read


def test_total_is_sum_of_line_items() -> None:
    p = m.project(
        m.Scenario(name="fixture", symbols=10, quote_symbols=5, history_years=1.0),
        MEASURED,
    )
    assert p.total_usd_month == math.fsum(
        [
            p.cool_storage_usd_month,
            p.cool_write_usd_month,
            p.cool_read_usd_month,
            p.cool_retrieval_usd_month,
            p.container_apps_usd_month,
            p.log_analytics_usd_month,
        ]
    )


def test_gate_comparison_approve_branch() -> None:
    p = m.project(
        m.Scenario(name="small", symbols=2, quote_symbols=2, history_years=1.0),
        MEASURED,
    )
    decision = m.decide(p, GATES)
    assert decision.branch == m.BRANCH_APPROVE
    assert decision.valve_spec is None
    assert len(decision.reasons) == 2


def test_gate_comparison_valve_branch() -> None:
    p = m.project(
        m.Scenario(name="broad", symbols=200, quote_symbols=200, history_years=10.65),
        MEASURED,
    )
    decision = m.decide(p, GATES)
    assert decision.branch == m.BRANCH_VALVE
    assert decision.valve_spec is None
    assert any("exceeds" in reason for reason in decision.reasons)


def test_gate_comparison_storage_only_failure() -> None:
    # Storage gate fails while the cost gate passes: still the valve branch.
    scenario = m.Scenario(
        name="storage-heavy", symbols=80, quote_symbols=0, history_years=10.65
    )
    p = m.project(scenario, MEASURED)
    assert p.projected_cool_storage_bytes > GATES.storage_capacity_bytes
    assert p.total_usd_month <= GATES.cost_gate_usd()
    assert m.decide(p, GATES).branch == m.BRANCH_VALVE


def test_gate_comparison_cost_only_failure() -> None:
    # Cost gate fails while the storage gate passes: still the valve branch.
    # The cost lever here is Cool data retrieval (research read-back of
    # stored quote bytes), billed at COOL_RETRIEVAL_USD_PER_GB.
    scenario = m.Scenario(
        name="cost-heavy",
        symbols=1,
        quote_symbols=0,
        history_years=1.0,
        monthly_retrieval_gb=5_000.0,
    )
    p = m.project(scenario, MEASURED)
    assert p.projected_cool_storage_bytes <= GATES.storage_capacity_bytes
    assert p.cool_retrieval_usd_month == 5_000.0 * m.COOL_RETRIEVAL_USD_PER_GB
    assert p.total_usd_month > GATES.cost_gate_usd()
    assert m.decide(p, GATES).branch == m.BRANCH_VALVE


def test_valve_spec_solves_top_n_under_capacity() -> None:
    scenario = m.Scenario(
        name="broad", symbols=200, quote_symbols=200, history_years=10.65
    )
    spec = m.solve_valve_spec(scenario, MEASURED, GATES)
    trade_total = 200 * ONE_YEAR_SESSIONS * 6_000_000
    remaining = GATES.storage_capacity_bytes - trade_total
    expected_n = remaining // (ONE_YEAR_SESSIONS * 80_000_000)
    assert spec.top_n_quote_symbols == expected_n
    assert spec.history_years == 1.0
    # The solved spec must actually fit the storage ceiling.
    fitted = m.project(
        m.Scenario(
            name="fitted",
            symbols=200,
            quote_symbols=spec.top_n_quote_symbols,
            history_years=1.0,
        ),
        MEASURED,
    )
    assert fitted.projected_cool_storage_bytes <= GATES.storage_capacity_bytes


def test_valve_spec_trades_only_shortened_history() -> None:
    scenario = m.Scenario(
        name="huge", symbols=800, quote_symbols=800, history_years=10.65
    )
    spec = m.solve_valve_spec(scenario, MEASURED, GATES)
    assert spec.top_n_quote_symbols == 0
    assert spec.history_years > 0.0
    fitted = m.project(
        m.Scenario(
            name="fitted",
            symbols=800,
            quote_symbols=0,
            history_years=spec.history_years,
        ),
        MEASURED,
    )
    assert fitted.projected_cool_storage_bytes <= GATES.storage_capacity_bytes


def test_valve_spec_infeasible_branch() -> None:
    scenario = m.Scenario(
        name="absurd",
        symbols=1000,
        quote_symbols=1000,
        history_years=10.65,
        plan_trade_bytes_per_session=10_000_000_000.0,
        plan_quote_bytes_per_session=10_000_000_000.0,
    )
    spec = m.solve_valve_spec(scenario, MEASURED, GATES)
    assert spec.top_n_quote_symbols == 0
    assert spec.history_years == 0.0
    assert "infeasible" in spec.note


def test_valve_ranking_order() -> None:
    spec = m.ValveSpec(top_n_quote_symbols=3, history_years=1.0)
    ranked = ["SPY", "AAPL", "QQQ", "IWM", "TSLA"]
    assert m.apply_valve(ranked, spec) == ["SPY", "AAPL", "QQQ"]
    assert m.apply_valve([], spec) == []
    assert (
        m.apply_valve(ranked, m.ValveSpec(top_n_quote_symbols=0, history_years=1.0))
        == []
    )


def test_decide_with_spec_returns_solved_valve() -> None:
    scenario = m.Scenario(
        name="broad", symbols=200, quote_symbols=200, history_years=10.65
    )
    decision = m.decide_with_spec(scenario, MEASURED, GATES)
    assert decision.branch == m.BRANCH_VALVE
    assert decision.valve_spec is not None
    assert decision.valve_spec.top_n_quote_symbols > 0


def test_embedded_valve_scenario_meets_gates() -> None:
    scenario = m.Scenario(
        name="valve", symbols=200, quote_symbols=25, history_years=1.0
    )
    p = m.project(scenario, MEASURED)
    decision = m.decide(p, GATES)
    assert decision.branch == m.BRANCH_APPROVE
    assert p.projected_cool_storage_bytes <= GATES.storage_capacity_bytes
    assert p.total_usd_month <= GATES.cost_gate_usd()


def test_render_table_is_deterministic_and_sorted() -> None:
    measured = m.MeasuredInputs()
    projections = [m.project(s, measured) for s in m.default_scenarios().values()]
    first = m.render_table(projections, GATES, measured)
    second = m.render_table(list(reversed(projections)), GATES, measured)
    assert first == second
    expected_names = sorted(m.default_scenarios())
    names = [
        line.split()[0] for line in first.splitlines()[2 : 2 + len(expected_names)]
    ]
    assert names == expected_names


def test_to_json_is_deterministic_and_sorted() -> None:
    payload = m.build_output(["base", "watchlist"], MEASURED, GATES, True)
    first = m.to_json(payload)
    second = m.to_json(m.build_output(["base", "watchlist"], MEASURED, GATES, True))
    assert first == second
    assert (
        first
        == json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    )
    assert json.loads(first) == payload


def test_measured_run_rate_extrapolation() -> None:
    measured = m.MeasuredInputs(month_to_date_spend_usd=1.2, month_to_date_days=9)
    assert m.measured_run_rate_usd_month(measured) == 1.2 / 9 * 30
