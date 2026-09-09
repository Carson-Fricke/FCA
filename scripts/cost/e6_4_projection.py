"""E6.4 cost projection and equity rollout decision model (T050).

Pure, deterministic, offline projection of the monthly Azure cost of
backfilling equity history into the market lake, and the mechanical
broad-backfill decision against the deployed plan gates
(infra/modules/cost-controls.bicep): monthly budget alerts at 40 and 70
USD and the 1 TiB storage capacity ceiling.

No network access, no clock reads, no randomness: every input is a fixed
constant or an explicit CLI override, so re-running the script always
reproduces the recorded tables byte for byte.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, replace
from typing import Any

# --------------------------------------------------------------------------
# Price constants.
#
# Cool-tier Blob prices: Azure Retail Prices API (prices.azure.com,
# api-version 2023-01-01-preview), region westus2, product "General Block
# Blob v2 Hierarchical Namespace", skuName "Cool LRS", retrieved
# 2026-09-09.  Meters: "Cool LRS Data Stored" 0.01 /GB-month, "Cool LRS
# Data Retrieval" 0.01 /GB, "Cool LRS Write Operations" 0.13 /10K,
# "Cool LRS Read Operations" 0.013 /10K, "Cool LRS Early Delete" 0.01 /GB.
#
# Container Apps (Consumption) and Log Analytics (PerGB2018) prices are the
# values recorded in config/lake/soak-aapl-spy.yaml, retrieved from the
# same API on 2026-09-05.
# --------------------------------------------------------------------------
PRICE_SOURCE_COOL = (
    "Azure Retail Prices API, prices.azure.com, api-version "
    "2023-01-01-preview, westus2, General Block Blob v2 Hierarchical "
    "Namespace Cool LRS, retrieved 2026-09-09"
)
PRICE_SOURCE_COMPUTE = (
    "Azure Retail Prices API, prices.azure.com, api-version "
    "2023-01-01-preview, westus2, retrieved 2026-09-05, as recorded in "
    "config/lake/soak-aapl-spy.yaml"
)

COOL_STORED_USD_PER_GB_MONTH = 0.01
COOL_RETRIEVAL_USD_PER_GB = 0.01
COOL_WRITE_USD_PER_10K = 0.13
COOL_READ_USD_PER_10K = 0.013
CA_VCPU_USD_PER_SECOND = 0.000034
CA_MEMORY_USD_PER_GIB_SECOND = 0.000004
CA_REQUESTS_USD_PER_MILLION = 0.4
LOG_ANALYTICS_INGESTION_USD_PER_GB = 2.3
LOG_ANALYTICS_FREE_GB_PER_MONTH = 5.0
LOG_GB_PER_1000_REQUESTS = 0.25

GIB = 1024**3

# Plan gates, as deployed (infra/main.bicep budgetName fca-ml-dev-budget
# composed with infra/modules/cost-controls.bicep defaults; confirmed live
# via `az consumption budget list -g fca-rg-westus2` on 2026-09-09).
PLAN_BUDGET_NAME = "fca-ml-dev-budget"
FIRST_BUDGET_ALERT_USD = 40.0
SECOND_BUDGET_ALERT_USD = 70.0
STORAGE_CAPACITY_BYTES = 1_099_511_627_776


@dataclass(frozen=True)
class Gates:
    """The plan gates the decision is evaluated against."""

    first_budget_alert_usd: float = FIRST_BUDGET_ALERT_USD
    second_budget_alert_usd: float = SECOND_BUDGET_ALERT_USD
    storage_capacity_bytes: int = STORAGE_CAPACITY_BYTES

    def cost_gate_usd(self) -> float:
        """Projected monthly total must stay under the first alert."""
        return self.first_budget_alert_usd


@dataclass(frozen=True)
class MeasuredInputs:
    """Baseline captured from the live environment on 2026-09-09."""

    capture_date: str = "2026-09-09"
    lake_container_bytes: int = 19_910_681
    account_used_capacity_bytes: int = 46_432_231
    month_to_date_spend_usd: float = 1.1826143591999996
    month_to_date_days: int = 9
    symbol_count: int = 2
    soak_id: str = "e6-full-history-aapl-spy"
    soak_sessions_trade: int = 1_506
    soak_bytes_trade: int = 20_200_835
    soak_sessions_quote: int = 1_506
    soak_bytes_quote: int = 15_339_692
    soak_api_seconds_total: float = 7_984.383
    soak_api_requests: int = 3_564

    def bounded_bytes_per_session_trade(self) -> float:
        return self.soak_bytes_trade / self.soak_sessions_trade

    def bounded_bytes_per_session_quote(self) -> float:
        return self.soak_bytes_quote / self.soak_sessions_quote

    def api_seconds_per_session(self) -> float:
        return self.soak_api_seconds_total / self.soak_api_requests


BRANCH_APPROVE = "APPROVE_BROAD_BACKFILL"
BRANCH_VALVE = "QUOTE_HISTORY_VALVE"


@dataclass(frozen=True)
class Scenario:
    """One what-if composition of the backfill universe.

    history_years multiplies sessions_per_year to a session count per
    symbol per data type.  full_depth=False uses the measured bounded
    (1,000-record cap) bytes per session from the soak; full_depth=True
    uses the order-of-magnitude planning bytes for unbounded sessions
    from config/lake/soak-aapl-spy.yaml.
    """

    name: str
    symbols: int
    quote_symbols: int
    history_years: float
    sessions_per_year: int = 252
    full_depth: bool = True
    plan_trade_bytes_per_session: float = 6_000_000.0
    plan_quote_bytes_per_session: float = 80_000_000.0
    container_vcores: float = 0.25
    container_memory_gib: float = 0.5
    monthly_retrieval_gb: float = 0.0

    def sessions(self) -> int:
        return int(round(self.history_years * self.sessions_per_year))


@dataclass(frozen=True)
class Projection:
    """Deterministic monthly steady-state projection for one scenario."""

    scenario: str
    symbols: int
    quote_symbols: int
    history_years: float
    sessions_per_symbol: int
    monthly_sessions: int
    trade_bytes_per_session: float
    quote_bytes_per_session: float
    base_storage_bytes: int
    projected_cool_storage_bytes: int
    projected_cool_storage_gb: float
    cool_storage_usd_month: float
    cool_write_usd_month: float
    cool_read_usd_month: float
    cool_retrieval_usd_month: float
    container_apps_usd_month: float
    log_analytics_usd_month: float
    total_usd_month: float

    def gate_rows(self, gates: Gates) -> dict[str, tuple[float, float, bool]]:
        return {
            "total_monthly_usd": (
                self.total_usd_month,
                gates.cost_gate_usd(),
                self.total_usd_month <= gates.cost_gate_usd(),
            ),
            "cool_storage_bytes": (
                float(self.projected_cool_storage_bytes),
                float(gates.storage_capacity_bytes),
                self.projected_cool_storage_bytes <= gates.storage_capacity_bytes,
            ),
        }


@dataclass(frozen=True)
class ValveSpec:
    """The ranked quote-history valve: keep the most liquid quote history."""

    top_n_quote_symbols: int
    history_years: float
    ranking: str = "liquidity rank ascending; keep ranks 1..N"
    note: str = ""

    def describe(self) -> str:
        lines = [
            f"keep only the top-{self.top_n_quote_symbols} quote history, ranked by liquidity (most liquid first)",
            f"trade history for the full universe at {self.history_years:.2f} years",
            f"quote history for ranks 1..{self.top_n_quote_symbols} at {self.history_years:.2f} years",
        ]
        if self.note:
            lines.append(self.note)
        return "; ".join(lines)


@dataclass(frozen=True)
class Decision:
    branch: str
    reasons: tuple[str, ...]
    valve_spec: ValveSpec | None = None


def project(scenario: Scenario, measured: MeasuredInputs) -> Projection:
    """Compute the steady-state monthly projection for one scenario."""
    sessions = scenario.sessions()
    if scenario.full_depth:
        trade_bytes = scenario.plan_trade_bytes_per_session
        quote_bytes = scenario.plan_quote_bytes_per_session
    else:
        trade_bytes = measured.bounded_bytes_per_session_trade()
        quote_bytes = measured.bounded_bytes_per_session_quote()
    sessions_per_month = int(round(scenario.sessions_per_year / 12))
    monthly_sessions = (
        scenario.symbols * sessions_per_month
        + scenario.quote_symbols * sessions_per_month
    )
    base_bytes = measured.lake_container_bytes
    trade_total = scenario.symbols * sessions * trade_bytes
    quote_total = scenario.quote_symbols * sessions * quote_bytes
    storage_bytes = int(round(base_bytes + trade_total + quote_total))
    storage_gb = storage_bytes / GIB
    storage_usd = storage_gb * COOL_STORED_USD_PER_GB_MONTH
    write_usd = monthly_sessions / 10_000 * COOL_WRITE_USD_PER_10K
    read_usd = monthly_sessions / 10_000 * COOL_READ_USD_PER_10K
    compute_usd = (
        monthly_sessions
        * measured.api_seconds_per_session()
        * (
            scenario.container_vcores * CA_VCPU_USD_PER_SECOND
            + scenario.container_memory_gib * CA_MEMORY_USD_PER_GIB_SECOND
        )
    )
    requests_usd = monthly_sessions / 1_000_000 * CA_REQUESTS_USD_PER_MILLION
    log_gb = monthly_sessions / 1000 * LOG_GB_PER_1000_REQUESTS
    log_usd = max(0.0, log_gb - LOG_ANALYTICS_FREE_GB_PER_MONTH) * (
        LOG_ANALYTICS_INGESTION_USD_PER_GB
    )
    retrieval_usd = scenario.monthly_retrieval_gb * COOL_RETRIEVAL_USD_PER_GB
    container_apps_usd = compute_usd + requests_usd
    total_usd = math.fsum(
        [
            storage_usd,
            write_usd,
            read_usd,
            retrieval_usd,
            container_apps_usd,
            log_usd,
        ]
    )
    return Projection(
        scenario=scenario.name,
        symbols=scenario.symbols,
        quote_symbols=scenario.quote_symbols,
        history_years=scenario.history_years,
        sessions_per_symbol=sessions,
        monthly_sessions=monthly_sessions,
        trade_bytes_per_session=trade_bytes,
        quote_bytes_per_session=quote_bytes,
        base_storage_bytes=base_bytes,
        projected_cool_storage_bytes=storage_bytes,
        projected_cool_storage_gb=storage_gb,
        cool_storage_usd_month=storage_usd,
        cool_write_usd_month=write_usd,
        cool_read_usd_month=read_usd,
        cool_retrieval_usd_month=retrieval_usd,
        container_apps_usd_month=container_apps_usd,
        log_analytics_usd_month=log_usd,
        total_usd_month=total_usd,
    )


def solve_valve_spec(
    scenario: Scenario, measured: MeasuredInputs, gates: Gates
) -> ValveSpec:
    """Solve the largest valve that fits the storage capacity ceiling.

    Priority order: keep trades for the full universe at a one-year
    history and fit as many liquidity-ranked quote symbols as the
    remaining bytes allow; if trades alone do not fit at one year, drop
    quotes entirely and shorten the trade history until it fits; if even
    one session per symbol does not fit, return an empty infeasible spec.
    """
    history_years_min = 1.0
    sessions_min = int(round(history_years_min * scenario.sessions_per_year))
    base_bytes = measured.lake_container_bytes
    trade_bytes = scenario.plan_trade_bytes_per_session
    quote_bytes = scenario.plan_quote_bytes_per_session
    trade_total_min = scenario.symbols * sessions_min * trade_bytes
    remaining = gates.storage_capacity_bytes - base_bytes - trade_total_min
    if remaining >= 0:
        quote_bytes_per_symbol = sessions_min * quote_bytes
        top_n = (
            int(remaining // quote_bytes_per_symbol)
            if quote_bytes_per_symbol > 0
            else 0
        )
        top_n = min(top_n, scenario.quote_symbols)
        return ValveSpec(
            top_n_quote_symbols=top_n,
            history_years=history_years_min,
            note="largest fit under the 1 TiB ceiling at one-year history; "
            "plan bytes are order-of-magnitude, so size the first valve "
            "conservatively below N",
        )
    per_symbol = trade_bytes + quote_bytes
    sessions_max = (
        int(
            (gates.storage_capacity_bytes - base_bytes)
            // (scenario.symbols * per_symbol)
        )
        if per_symbol > 0
        else 0
    )
    if sessions_max < 1:
        return ValveSpec(
            top_n_quote_symbols=0,
            history_years=0.0,
            note="infeasible: even one session per symbol exceeds the "
            "storage capacity ceiling; requires bounded-depth ingest or "
            "an approved capacity increase",
        )
    return ValveSpec(
        top_n_quote_symbols=0,
        history_years=round(sessions_max / scenario.sessions_per_year, 4),
        note="quotes deferred entirely; trade history shortened until "
        "the full universe fits the 1 TiB ceiling",
    )


def decide(projection: Projection, gates: Gates) -> Decision:
    """Mechanical decision rule (E6.4 AC1).

    Broad backfill is approved only when both the projected Cool storage
    and the projected total monthly cost meet the plan gates; otherwise
    the ranked quote-history valve fires.
    """
    rows = projection.gate_rows(gates)
    reasons = []
    for name, (actual, limit, ok) in rows.items():
        verdict = "meets" if ok else "exceeds"
        reasons.append(f"{name}: {actual:.4f} {verdict} gate {limit:.4f}")
    if all(ok for _, _, ok in rows.values()):
        return Decision(
            branch=BRANCH_APPROVE,
            reasons=tuple(reasons),
            valve_spec=None,
        )
    return Decision(
        branch=BRANCH_VALVE,
        reasons=tuple(reasons),
        valve_spec=None,
    )


def decide_with_spec(
    scenario: Scenario, measured: MeasuredInputs, gates: Gates
) -> Decision:
    """decide() for a scenario, solving the valve spec on failure."""
    projection = project(scenario, measured)
    decision = decide(projection, gates)
    if decision.branch == BRANCH_VALVE:
        spec = solve_valve_spec(scenario, measured, gates)
        return replace(decision, valve_spec=spec)
    return decision


def apply_valve(ranked_symbols: list[str], spec: ValveSpec) -> list[str]:
    """Keep the top-N most liquid symbols in rank order."""
    return list(ranked_symbols[: spec.top_n_quote_symbols])


def default_scenarios() -> dict[str, Scenario]:
    """Documented scenario set (see docs/e6.4-cost-projection.md)."""
    return {
        "base": Scenario(
            name="base",
            symbols=2,
            quote_symbols=2,
            history_years=6.0,
            full_depth=False,
        ),
        "bounded200": Scenario(
            name="bounded200",
            symbols=200,
            quote_symbols=200,
            history_years=10.65,
            full_depth=False,
        ),
        "watchlist": Scenario(
            name="watchlist",
            symbols=200,
            quote_symbols=200,
            history_years=10.65,
        ),
        "valve": Scenario(
            name="valve",
            symbols=200,
            quote_symbols=25,
            history_years=1.0,
        ),
        "scaled": Scenario(
            name="scaled",
            symbols=500,
            quote_symbols=500,
            history_years=3.0,
            plan_trade_bytes_per_session=3_750_000.0,
            plan_quote_bytes_per_session=55_000_000.0,
        ),
    }


def measured_run_rate_usd_month(measured: MeasuredInputs) -> float:
    """Current spend run rate extrapolated linearly over a 30-day month."""
    return measured.month_to_date_spend_usd / measured.month_to_date_days * 30


def render_table(
    projections: list[Projection], gates: Gates, measured: MeasuredInputs
) -> str:
    """Fixed-width, sorted, deterministic table of the scenario set."""
    header = (
        f"{'scenario':<10} {'symbols':>7} {'quotes':>6} {'hist_y':>6} "
        f"{'cool_gb':>10} {'storage_usd':>11} {'ca_usd':>8} {'total_usd':>10} "
        f"{'storage_gate':>12} {'cost_gate':>9}"
    )
    lines = [header, "-" * len(header)]
    for p in sorted(projections, key=lambda item: item.scenario):
        rows = p.gate_rows(gates)
        lines.append(
            f"{p.scenario:<10} {p.symbols:>7d} {p.quote_symbols:>6d} "
            f"{p.history_years:>6.2f} {p.projected_cool_storage_gb:>10.3f} "
            f"{p.cool_storage_usd_month:>11.4f} {p.container_apps_usd_month:>8.4f} "
            f"{p.total_usd_month:>10.4f} "
            f"{('PASS' if rows['cool_storage_bytes'][2] else 'FAIL'):>12} "
            f"{('PASS' if rows['total_monthly_usd'][2] else 'FAIL'):>9}"
        )
    lines.append("-" * len(header))
    lines.append(
        f"gates: total <= {gates.cost_gate_usd():.2f} USD/month "
        f"(budget {PLAN_BUDGET_NAME}-40usd), storage <= "
        f"{gates.storage_capacity_bytes} bytes (1 TiB ceiling); "
        f"second budget alert {gates.second_budget_alert_usd:.2f} USD is the hard ceiling"
    )
    lines.append(
        f"measured baseline: {measured.capture_date}, lake container "
        f"{measured.lake_container_bytes} bytes, month-to-date spend "
        f"{measured.month_to_date_spend_usd:.4f} USD over "
        f"{measured.month_to_date_days} days "
        f"(run rate {measured_run_rate_usd_month(measured):.4f} USD/30d)"
    )
    return "\n".join(lines)


def to_json(payload: Any) -> str:
    """Deterministic JSON: sorted keys, fixed indent, trailing newline."""
    return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def build_output(
    names: list[str],
    measured: MeasuredInputs,
    gates: Gates,
    include_decisions: bool,
) -> dict[str, Any]:
    scenarios = default_scenarios()
    projections = [project(scenarios[name], measured) for name in names]
    payload: dict[str, Any] = {
        "price_sources": {"cool": PRICE_SOURCE_COOL, "compute": PRICE_SOURCE_COMPUTE},
        "gates": asdict(gates),
        "measured": asdict(measured),
        "measured_run_rate_usd_month": measured_run_rate_usd_month(measured),
        "projections": [
            asdict(p) for p in sorted(projections, key=lambda item: item.scenario)
        ],
    }
    if include_decisions:
        decisions = {}
        for name in names:
            decision = decide_with_spec(scenarios[name], measured, gates)
            decisions[name] = {
                "branch": decision.branch,
                "reasons": list(decision.reasons),
                "valve_spec": (
                    asdict(decision.valve_spec) if decision.valve_spec else None
                ),
            }
        payload["decisions"] = decisions
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="E6.4 cost projection and equity rollout decision model",
    )
    parser.add_argument(
        "--scenario",
        default="all",
        help="comma-separated scenario names (base,watchlist,valve,scaled) or 'all'",
    )
    parser.add_argument(
        "--format",
        choices=["json", "table"],
        default="table",
        help="output format (default: table)",
    )
    parser.add_argument(
        "--inputs",
        default=None,
        help="optional JSON file overriding the embedded measured baseline fields",
    )
    parser.add_argument(
        "--decisions",
        action="store_true",
        help="include the decide() outcome and solved valve specs",
    )
    return parser.parse_args(argv)


def load_measured_inputs(path: str | None) -> MeasuredInputs:
    if path is None:
        return MeasuredInputs()
    with open(path, encoding="utf-8") as handle:
        overrides = json.load(handle)
    return replace(MeasuredInputs(), **overrides)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    measured = load_measured_inputs(args.inputs)
    gates = Gates()
    scenarios = default_scenarios()
    if args.scenario == "all":
        names = sorted(scenarios)
    else:
        names = [name.strip() for name in args.scenario.split(",") if name.strip()]
        unknown = [name for name in names if name not in scenarios]
        if unknown:
            raise SystemExit(f"unknown scenario(s): {', '.join(sorted(unknown))}")
    if args.format == "json":
        print(to_json(build_output(names, measured, gates, args.decisions)), end="")
    else:
        projections = [project(scenarios[name], measured) for name in names]
        print(render_table(projections, gates, measured))
    return 0


if __name__ == "__main__":
    sys.exit(main())
