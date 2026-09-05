from __future__ import annotations

from typing import cast

import pytest

from thesis.experiments import (
    ClaimKind,
    DeviationKind,
    ExperimentContract,
    ExperimentContractError,
    Partition,
    PartitionRole,
    StoppingRule,
    StoppingRuleKind,
    detect_deviations,
    record_deviation,
)

NOW = "2026-01-01T00:00:00.000000Z"


def _stopping_rule() -> StoppingRule:
    return StoppingRule(
        kind=StoppingRuleKind.MAX_LOOKS,
        description="At most two looks at the holdout endpoint.",
        max_looks=2,
    )


def _contract() -> ExperimentContract:
    return ExperimentContract(
        workspace_id="workspace-a",
        experiment_id="experiment-costs",
        version="1.0.0",
        title="Does the momentum signal survive costs?",
        claim_kind=ClaimKind.PREDICTIVE,
        hypothesis="Cost-adjusted Sharpe on holdout exceeds the baseline by 0.2.",
        primary_endpoint="annualized_sharpe",
        stopping_rule=_stopping_rule(),
        partitions=(
            Partition(role=PartitionRole.TRAIN, start_ns=1, end_ns=100),
            Partition(role=PartitionRole.HOLDOUT, start_ns=100, end_ns=200),
        ),
        created_by="agent-1",
        created_at=NOW,
        comparison_basis="equal-cost baseline strategy bundle",
    )


def test_contract_hash_is_canonical_and_reproducible() -> None:
    contract = _contract()
    again = _contract()
    assert contract.contract_sha256 == again.contract_sha256
    assert contract.contract_sha256 == contract.contract_sha256.lower()
    assert len(contract.contract_sha256) == 64
    assert contract.contract_id == f"ect-{contract.contract_sha256[:48]}"


def test_contract_locks_required_pre_registration_fields() -> None:
    contract = _contract()
    document = contract.canonical_document
    assert document["hypothesis"] == contract.hypothesis
    assert document["primaryEndpoint"] == "annualized_sharpe"
    assert document["comparisonBasis"] == "equal-cost baseline strategy bundle"
    stopping = cast(dict[str, object], document["stoppingRule"])
    assert stopping["kind"] == "max_looks"
    assert stopping["maxLooks"] == 2
    partitions = cast(list[dict[str, object]], document["partitions"])
    assert [
        (partition["role"], partition["startNs"], partition["endNs"])
        for partition in partitions
    ] == [("train", 1, 100), ("holdout", 100, 200)]


def test_any_field_change_produces_a_new_contract_hash() -> None:
    contract = _contract()
    modified = ExperimentContract(
        workspace_id="workspace-a",
        experiment_id="experiment-costs",
        version="1.0.0",
        title="Does the momentum signal survive costs?",
        claim_kind=ClaimKind.PREDICTIVE,
        hypothesis="A different hypothesis text.",
        primary_endpoint="annualized_sharpe",
        stopping_rule=_stopping_rule(),
        partitions=(
            Partition(role=PartitionRole.TRAIN, start_ns=1, end_ns=100),
            Partition(role=PartitionRole.HOLDOUT, start_ns=100, end_ns=200),
        ),
        created_by="agent-1",
        created_at=NOW,
    )
    assert modified.contract_sha256 != contract.contract_sha256


def test_causal_claim_requires_identification_design() -> None:
    with pytest.raises(ExperimentContractError):
        ExperimentContract(
            workspace_id="workspace-a",
            experiment_id="experiment-causal",
            version="1.0.0",
            title="Causal claim without a design",
            claim_kind=ClaimKind.CAUSAL,
            hypothesis="The intervention raises returns.",
            primary_endpoint="treatment_effect",
            stopping_rule=_stopping_rule(),
            partitions=(Partition(role=PartitionRole.TRAIN, start_ns=1, end_ns=100),),
            created_by="agent-1",
            created_at=NOW,
        )


def test_detect_deviations_reports_modified_and_undeclared_fields() -> None:
    contract = _contract()
    observed = {
        "primaryEndpoint": "total_return",
        "unexpectedField": "invented mid-run",
    }
    findings = detect_deviations(contract, observed)
    by_field = {finding.field: finding for finding in findings}
    assert set(by_field) == {"primaryEndpoint", "unexpectedField"}
    assert by_field["primaryEndpoint"].deviation_kind is DeviationKind.MODIFIED_FIELD
    assert by_field["primaryEndpoint"].declared == "annualized_sharpe"
    assert by_field["unexpectedField"].deviation_kind is DeviationKind.UNDECLARED_FIELD


def test_detect_deviations_is_deterministic_and_clean_when_identical() -> None:
    contract = _contract()
    observed = dict(contract.canonical_document)
    assert detect_deviations(contract, observed) == detect_deviations(
        contract, observed
    )
    observed["primaryEndpoint"] = "sortino"
    observed["hypothesis"] = "Rewritten after looking."
    reordered = {
        "hypothesis": "Rewritten after looking.",
        "primaryEndpoint": "sortino",
    }
    assert detect_deviations(contract, reordered) == detect_deviations(
        contract, observed
    )


def test_record_deviation_requires_a_reason_and_hashes_content() -> None:
    contract = _contract()
    finding = detect_deviations(contract, {"primaryEndpoint": "total_return"})[0]
    with pytest.raises(ExperimentContractError):
        record_deviation(
            contract,
            finding,
            reason="   ",
            actor="agent-1",
            recorded_at=NOW,
        )
    deviation = record_deviation(
        contract,
        finding,
        reason="Endpoint changed after the metric failed its sanity check.",
        actor="agent-1",
        recorded_at=NOW,
    )
    assert deviation.contract_sha256 == contract.contract_sha256
    assert deviation.deviation_kind is DeviationKind.MODIFIED_FIELD
    assert deviation.content_sha256 == deviation.content_sha256


def test_stopping_rule_exposure_reports_multiple_testing_budget() -> None:
    rule = _stopping_rule()
    assert rule.exposure(1).exceeds_budget is False
    assert rule.exposure(2).exceeds_budget is False
    assert rule.exposure(3).exceeds_budget is True
    assert rule.exposure(3).allowed_looks == 2


def test_fixed_sample_stopping_rule_allows_one_look() -> None:
    rule = StoppingRule(
        kind=StoppingRuleKind.FIXED_SAMPLE,
        description="One look at the endpoint.",
    )
    assert rule.exposure(1).exceeds_budget is False
    assert rule.exposure(2).exceeds_budget is True
    with pytest.raises(ExperimentContractError):
        StoppingRule(
            kind=StoppingRuleKind.FIXED_SAMPLE,
            description="Two looks.",
            max_looks=2,
        )


def test_sequential_boundary_rules_are_boundary_governed() -> None:
    rule = StoppingRule(
        kind=StoppingRuleKind.SEQUENTIAL_BOUNDARY,
        description="Alpha-spending boundary on a two-sided test.",
        boundary="pocock-0.05-two-sided",
    )
    exposure = rule.exposure(10)
    assert exposure.exceeds_budget is False
    assert exposure.allowed_looks is None
    assert exposure.governed_by == "sequential_boundary"
    with pytest.raises(ExperimentContractError):
        StoppingRule(
            kind=StoppingRuleKind.SEQUENTIAL_BOUNDARY,
            description="Missing boundary.",
        )


def test_partitions_must_be_time_ordered_and_non_overlapping() -> None:
    with pytest.raises(ExperimentContractError):
        ExperimentContract(
            workspace_id="workspace-a",
            experiment_id="experiment-bad",
            version="1.0.0",
            title="Overlapping partitions",
            claim_kind=ClaimKind.PREDICTIVE,
            hypothesis="Overlapping windows are leakage.",
            primary_endpoint="annualized_sharpe",
            stopping_rule=_stopping_rule(),
            partitions=(
                Partition(role=PartitionRole.TRAIN, start_ns=1, end_ns=150),
                Partition(role=PartitionRole.HOLDOUT, start_ns=100, end_ns=200),
            ),
            created_by="agent-1",
            created_at=NOW,
        )


def test_naive_timestamps_are_rejected() -> None:
    with pytest.raises(ExperimentContractError):
        ExperimentContract(
            workspace_id="workspace-a",
            experiment_id="experiment-naive",
            version="1.0.0",
            title="Naive timestamp",
            claim_kind=ClaimKind.PREDICTIVE,
            hypothesis="Naive time is ambiguous.",
            primary_endpoint="annualized_sharpe",
            stopping_rule=_stopping_rule(),
            partitions=(Partition(role=PartitionRole.TRAIN, start_ns=1, end_ns=100),),
            created_by="agent-1",
            created_at="2026-01-01T00:00:00",
        )


def test_timestamps_are_normalized_to_utc_z() -> None:
    contract = ExperimentContract(
        workspace_id="workspace-a",
        experiment_id="experiment-offset",
        version="1.0.0",
        title="Offset timestamp",
        claim_kind=ClaimKind.PREDICTIVE,
        hypothesis="Offsets normalize.",
        primary_endpoint="annualized_sharpe",
        stopping_rule=_stopping_rule(),
        partitions=(Partition(role=PartitionRole.TRAIN, start_ns=1, end_ns=100),),
        created_by="agent-1",
        created_at="2026-01-01T01:00:00+01:00",
    )
    assert contract.created_at == NOW
