"""Trader monitoring infrastructure contract.

The contract is pinned without deploying Azure: the alert module's resource
composition, parameterized email delivery, trader app-name scoping, and the
supervision-string taxonomy are checked directly against the Bicep source.
The first real deployment happens through the go-live runbook wiring, not in
these tests.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
MONITORING = ROOT / "infra" / "modules" / "monitoring.bicep"

# The supervision detail prefixes the kernel writes
# (src/harness/cli/supervisor.py) plus the one string the composition
# boundary prints unconditionally today (src/harness/cli/trader.py). The
# fault class is expected-recoverable trouble; the halt class stops trading.
FAULT_STRINGS = (
    "restartable fault",
    "market-wait exceeded",
    "trader feed boundary fault",
    "Traceback",
)
HALT_STRINGS = (
    "kill-switch engaged",
    "reconciliation aborted",
    "daily_loss_halt limit reached",
    "restart budget exhausted",
    "supervision ledger write failed",
)

_RESOURCE = re.compile(r"^resource (\w+) '([^'@]+)", re.MULTILINE)
_VAR = re.compile(r"^var (\w+) =", re.MULTILINE)
_DECLARATION = re.compile(r"^(?:targetScope|param|var|resource|output) ", re.MULTILINE)
_EMAIL_LITERAL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]+")
_SECRET_MARKERS = ("AccountKey=", "SharedAccessSignature", "password", "client_secret")


def _declarations(pattern: re.Pattern[str]) -> dict[str, str]:
    """Map each top-level declaration's name to its body text.

    Every body runs from its declaration line to the next top-level
    declaration of any kind, which is exact for the module's flat
    composition.
    """
    content = MONITORING.read_text(encoding="utf-8")
    starts = sorted(match.start() for match in _DECLARATION.finditer(content))
    bodies: dict[str, str] = {}
    for match in pattern.finditer(content):
        following = [start for start in starts if start > match.start()]
        end = following[0] if following else len(content)
        bodies[match.group(1)] = content[match.start() : end]
    return bodies


def _resources() -> dict[str, tuple[str, str]]:
    """Map each resource symbol to its (API-versionless type, body)."""
    resources: dict[str, tuple[str, str]] = {}
    for symbol, body in _declarations(_RESOURCE).items():
        kind = body.split("'")[1]
        resources[symbol] = (kind.split("@")[0], body)
    return resources


def _vars() -> dict[str, str]:
    return _declarations(_VAR)


def _by_type(type_suffix: str) -> dict[str, str]:
    return {
        symbol: body
        for symbol, (kind, body) in _resources().items()
        if kind.endswith(type_suffix)
    }


def test_action_group_delivers_a_parameterized_email() -> None:
    action_groups = _by_type("Microsoft.Insights/actionGroups")
    assert set(action_groups) == {"traderActionGroup"}
    body = next(iter(action_groups.values()))

    assert "emailReceivers" in body
    assert re.search(r"emailAddress:\s+traderAlertContactEmail", body)
    assert _EMAIL_LITERAL.search(body) is None


def test_module_embeds_no_plaintext_email_or_secret() -> None:
    content = MONITORING.read_text(encoding="utf-8")

    assert _EMAIL_LITERAL.search(content) is None
    lowered = content.lower()
    for marker in _SECRET_MARKERS:
        assert marker.lower() not in lowered


def test_required_email_parameter_carries_no_default() -> None:
    content = MONITORING.read_text(encoding="utf-8")

    assert "param traderAlertContactEmail string\n" in content
    assert not re.search(r"traderAlertContactEmail\s+string\s*=", content)


def test_query_alerts_wire_the_scoped_trader_query() -> None:
    query_rules = _by_type("Microsoft.Insights/scheduledQueryRules")
    assert set(query_rules) == {"traderFaultAlert", "traderHaltAlert"}
    queries = _vars()
    app_filter = queries["traderAppFilter"]

    for symbol, body in query_rules.items():
        query_name = "faultQuery" if symbol == "traderFaultAlert" else "haltQuery"
        assert f"query: {query_name}" in body
        assert "traderActionGroup.id" in body
        query = queries[query_name]
        assert "ContainerAppConsoleLogs_CL" in query
        assert "where ${traderAppFilter}" in query
        assert "startswith \\'${namePrefix}-\\'" in app_filter
        assert "endswith \\'-trader\\'" in app_filter


@pytest.mark.parametrize("match", FAULT_STRINGS)
def test_fault_alert_matches_the_expected_fault_string(match: str) -> None:
    fault_query = _vars()["faultQuery"]

    assert match in fault_query
    for halt_string in HALT_STRINGS:
        assert halt_string not in fault_query


@pytest.mark.parametrize("match", HALT_STRINGS)
def test_halt_alert_matches_the_expected_halt_string(match: str) -> None:
    halt_query = _vars()["haltQuery"]

    assert match in halt_query
    for fault_string in FAULT_STRINGS:
        assert fault_string not in halt_query


def test_query_alerts_declare_severity_and_resolve_the_existing_workspace() -> None:
    query_rules = _by_type("Microsoft.Insights/scheduledQueryRules")
    workspace = _by_type("Microsoft.OperationalInsights/workspaces")

    assert set(workspace) == {"logAnalyticsWorkspace"}
    assert " existing" in workspace["logAnalyticsWorkspace"]
    for body in query_rules.values():
        assert re.search(r"severity: \d", body)
        assert "logAnalyticsWorkspace.id" in body


def test_activity_alert_targets_trader_management_operations() -> None:
    activity_rules = _by_type("Microsoft.Insights/activityLogAlerts")
    assert set(activity_rules) == {"traderRestartAlert"}
    body = next(iter(activity_rules.values()))

    assert "'Microsoft.App/containerApps'" in body
    # Activity log alert conditions require exactly one alertable category and
    # cannot filter on a resource-name substring (resourceUri is not alertable),
    # so the rule is scoped by category + resource type + operation anyOf.
    assert "Administrative" in body
    assert "anyOf" in body
    assert "Microsoft.App/containerApps/revisions/restart/action" in body
    assert "traderActionGroup" in body


def test_every_deployed_alert_routes_the_action_group() -> None:
    resources = _resources()
    alert_symbols = {
        symbol
        for symbol, (kind, _) in resources.items()
        if kind.startswith("Microsoft.Insights/scheduledQueryRules")
        or kind.startswith("Microsoft.Insights/activityLogAlerts")
    }

    assert len(alert_symbols) == 3
    for symbol in alert_symbols:
        assert "traderActionGroup" in resources[symbol][1]
