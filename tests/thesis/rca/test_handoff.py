"""Focused tests for the blinded RCA handoff, response schema, and egress."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, cast

import pytest

from thesis.experiments.grades import EvidenceGrade
from thesis.protected import (
    AliasStrategyKind,
    IdentifierLeakError,
    ProtectedRcaRelease,
    RcaEvidenceTier,
    RcaReleaseRequest,
    ReleaseGate,
    ReleaseRiskAssessment,
    StrategyGateDecision,
)
from thesis.protected.aliasing import IdentitySurface
from thesis.rca import (
    BUNDLE_PAYLOAD_SECTIONS,
    RESPONSE_SCHEMA,
    AdviceEgress,
    AdviceEgressError,
    RcaHandoffError,
    RcaResponseError,
    RcaResponseField,
    RcaResponseVerdict,
    RevisionHandoff,
    RevisionHandoffError,
    assemble_handoff,
    record_response,
    validate_rca_response,
)

NOW = "2026-03-01T12:00:00.000000Z"
RECORDED_AT = "2026-03-02T09:30:00.000000Z"

ARTIFACT = (
    "experiment-77 AAPL filled 2024-01-05T14:03:00Z in 123.4ms from "
    "C:\\Users\\alice\\run.log contact alice@example.com via "
    "https://internal.example.com feed #win"
)
SOURCE = "def run(config):\n    return config.universe\n"
CONTRACT = {"contract": "experiment momentum-v9 trades AAPL per lineage-12 rules"}
DEPENDENCIES = {"duckdb": "1.1.3", "numpy": "2.0.1"}
BENCHMARK = {"benchmarkReturn": 0.07}
COSTS = {"feeBps": 1.0}
DATA_QUALITY = {"missingRows": 0}

MANIFEST_JSON: dict[str, Any] = dict(
    sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
)


def _map(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return cast(dict[str, object], value)


def _decision(
    strategy: AliasStrategyKind,
    *,
    empirical: bool = True,
    utility: bool = True,
    reason: str = "",
) -> StrategyGateDecision:
    if empirical and utility:
        reason = ""
    elif not reason:
        reason = "release gate threshold failed"
    return StrategyGateDecision(
        strategy=strategy,
        empirical_reidentification_ok=empirical,
        diagnostic_utility_ok=utility,
        reason=reason,
    )


def _open_gate() -> ReleaseGate:
    return ReleaseGate(
        cross_release_disclosure_ok=True,
        decisions=(
            _decision(AliasStrategyKind.IDENTITY_MASKING),
            _decision(AliasStrategyKind.HARDENED_DEIDENTIFICATION),
        ),
    )


def _request(**overrides: Any) -> RcaReleaseRequest:
    values: dict[str, Any] = {
        "workspace_id": "ws-test",
        "submission_id": "sub-1",
        "strategy_id": "momentum-v9",
        "experiment_id": "experiment-77",
        "lineage_id": "lineage-12",
        "rca_session_id": "rca-session-1",
        "evidence_tier": RcaEvidenceTier.CUTOFF_CONSTRAINED,
        "release_gate": _open_gate(),
        "release_salt": "salt-alpha",
        "created_at": NOW,
        "risk": ReleaseRiskAssessment(
            cross_release_disclosure="overlapping window reached one prior session",
            empirical_reidentification_risk=(
                "price-path attack success below the policy threshold"
            ),
            diagnostic_utility="order-flow attribution retained at minute grain",
            residual_risk="correlation structure may still fingerprint the venue",
        ),
        "strategy_source": SOURCE,
        "artifacts": {"metrics.txt": ARTIFACT},
        "symbols": ("AAPL",),
    }
    values.update(overrides)
    return RcaReleaseRequest(**values)


def _release(**overrides: Any) -> ProtectedRcaRelease:
    return ProtectedRcaRelease.create(_request(**overrides))


def _assemble(release: ProtectedRcaRelease, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "strategy_source": SOURCE,
        "contract_snapshot": CONTRACT,
        "dependency_versions": DEPENDENCIES,
        "benchmark": BENCHMARK,
        "costs": COSTS,
        "data_quality_manifest": DATA_QUALITY,
    }
    values.update(overrides)
    return assemble_handoff(release, **values)


def _response(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "candidateExplanations": ["cost drag explains the shortfall"],
        "supportingEvidence": ["fee-adjusted returns approach the benchmark"],
        "conflictingEvidence": ["gross returns remain above the benchmark"],
        "uncertainty": ["one window cannot separate fees from signal decay"],
        "alternativeExplanations": ["the signal decayed after the regime shift"],
        "discriminatingTests": ["re-run with a fee-free fill model"],
    }
    values.update(overrides)
    return values


def _accepted(
    release: ProtectedRcaRelease, **response_overrides: Any
) -> RcaResponseVerdict:
    bundle = _assemble(release)
    return record_response(bundle, _response(**response_overrides))


# AC1: the bundle assembles exactly the policy-approved payload.


def test_bundle_assembles_exactly_the_policy_approved_payload() -> None:
    release = _release()
    bundle = _assemble(release)
    assert set(bundle.payload) == set(BUNDLE_PAYLOAD_SECTIONS)
    assert dict(bundle.payload["aliasedRun"]) == dict(release.artifacts)
    assert dict(bundle.payload["strategySource"]) == {"source": SOURCE}
    manifest_json = json.dumps(release.manifest.canonical_document, **MANIFEST_JSON)
    assert dict(bundle.payload["transformationManifest"]) == {"manifest": manifest_json}
    document = bundle.canonical_document
    assert set(document) == {
        "aliasStrategy",
        "evidenceTier",
        "payload",
        "payloadSha256",
        "policyVersion",
        "releaseContentSha256",
        "releaseId",
        "rcaSessionId",
        "schema",
    }
    assert document["evidenceTier"] == "cutoff-constrained"
    assert document["aliasStrategy"] == "identity-masking"
    assert document["releaseId"] == release.release_id
    assert document["rcaSessionId"] == release.rca_session_id
    assert document["releaseContentSha256"] == release.content_sha256
    assert (
        document["payloadSha256"]["aliasedRun/metrics.txt"]
        == hashlib.sha256(release.artifacts["metrics.txt"].encode("utf-8")).hexdigest()
    )
    assert (
        document["payloadSha256"]["strategySource/source"]
        == hashlib.sha256(SOURCE.encode("utf-8")).hexdigest()
    )
    assert (
        document["payloadSha256"]["transformationManifest/manifest"]
        == hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()
    )
    for section, entries in bundle.payload.items():
        for name in entries:
            assert f"{section}/{name}" in bundle.payload_sha256
    for name, expected in release.manifest.artifact_sha256.items():
        key = f"aliasedRun/{name}"
        assert bundle.payload_sha256[key] == expected


def test_bundle_document_carries_no_real_identity() -> None:
    release = _release()
    bundle = _assemble(release)
    dumped = json.dumps(bundle.canonical_document, sort_keys=True)
    for real in (
        "momentum-v9",
        "AAPL",
        "experiment-77",
        "lineage-12",
        "ws-test",
        "sub-1",
        "alice",
        "2024-01-05",
        "example.com",
    ):
        assert real not in dumped
    contract = bundle.payload["contractSnapshot"]["contract"]
    assert "momentum-v9" not in contract
    assert "AAPL" not in contract
    assert "lineage-12" not in contract
    assert "strat_" in contract
    assert "asset_" in contract
    assert "lin_" in contract


def test_bundle_blocks_identity_revealing_source_fail_closed() -> None:
    release = _release()
    with pytest.raises(RcaHandoffError) as error:
        _assemble(release, strategy_source='def run():\n    return "AAPL"\n')
    message = str(error.value)
    assert "screening" in message
    assert "AAPL" not in message


def test_bundle_is_deterministic_and_content_addressed() -> None:
    release = _release()
    first = _assemble(release)
    second = _assemble(release)
    assert first.canonical_document == second.canonical_document
    assert first.content_sha256 == second.content_sha256
    assert first.payload_sha256 == second.payload_sha256
    canonical = json.dumps(
        first.canonical_document, sort_keys=True, separators=(",", ":")
    )
    assert canonical == json.dumps(
        json.loads(canonical), sort_keys=True, separators=(",", ":")
    )


def test_bundle_rejects_incomplete_or_unserializable_sections() -> None:
    release = _release()
    with pytest.raises(RcaHandoffError):
        _assemble(release, contract_snapshot={})
    with pytest.raises(RcaHandoffError):
        _assemble(release, dependency_versions={"numpy": ""})
    with pytest.raises(RcaHandoffError):
        _assemble(release, benchmark={"note": object()})
    with pytest.raises(RcaHandoffError):
        _assemble(release, costs={1: "1.0"})
    with pytest.raises(RcaHandoffError):
        _assemble(release, strategy_source="")


# AC2: the response schema admits only the diagnosis categories.


def test_accepted_response_recorded_once_with_schema_fields() -> None:
    release = _release()
    bundle = _assemble(release)
    verdict = record_response(bundle, _response())
    assert verdict.accepted is True
    assert verdict.violations == ()
    assert verdict.response is not None
    assert set(verdict.response) == {field.value for field in RcaResponseField}
    assert verdict.release_id == release.release_id
    assert verdict.rca_session_id == release.rca_session_id
    assert verdict.evidence_tier == "cutoff-constrained"
    assert verdict.release_content_sha256 == release.content_sha256
    assert bundle.recorded is verdict
    again = record_response(_assemble(release), _response())
    assert again.response_sha256 == verdict.response_sha256
    with pytest.raises(RcaHandoffError):
        record_response(bundle, _response())


def test_response_schema_rejects_edit_and_promotion_fields() -> None:
    release = _release()
    bundle = _assemble(release)
    rejected = record_response(
        bundle,
        _response(
            strategyEdit="patch the entry rule",
            parameterRecommendations=["slow the entries"],
            promotionDecision="activate the strategy",
        ),
    )
    assert rejected.accepted is False
    assert rejected.response is None
    assert bundle.recorded is None
    joined = "; ".join(rejected.violations)
    for field in ("strategyEdit", "parameterRecommendations", "promotionDecision"):
        assert field in joined
    with pytest.raises(RcaResponseError) as error:
        validate_rca_response(_response(strategyEdit="patch the entry rule"))
    assert "strategyEdit" in "; ".join(error.value.violations)


def test_response_schema_rejects_missing_and_malformed_fields() -> None:
    release = _release()
    bundle = _assemble(release)
    rejected = record_response(bundle, _response(uncertainty=None))
    assert rejected.accepted is False
    assert "missing field 'uncertainty'" in rejected.violations
    assert bundle.recorded is None
    malformed = record_response(
        bundle,
        _response(discriminatingTests="re-run with fees", uncertainty=[42]),
    )
    assert malformed.accepted is False
    joined = "; ".join(malformed.violations)
    assert "field 'discriminatingTests' must be a list of strings" in joined
    assert "item 0 must be a non-empty string" in joined
    assert bundle.recorded is None


def test_response_schema_validates_optional_schema_marker() -> None:
    release = _release()
    assert record_response(_assemble(release), _response()).accepted is True
    marked = record_response(_assemble(release), _response(schema=RESPONSE_SCHEMA))
    assert marked.accepted is True
    wrong = record_response(_assemble(release), _response(schema="other-v1"))
    assert wrong.accepted is False
    assert any("schema" in violation for violation in wrong.violations)


def test_rejected_response_records_nothing_but_allows_a_retry() -> None:
    release = _release()
    bundle = _assemble(release)
    rejected = record_response(bundle, _response(strategyEdit="edit"))
    assert rejected.accepted is False
    assert bundle.recorded is None
    accepted = record_response(bundle, _response())
    assert accepted.accepted is True
    assert bundle.recorded is accepted


# AC3: advice egress is schema-limited and filter-guarded.


def test_advice_document_carries_only_schema_limited_payload() -> None:
    release = _release()
    advice = AdviceEgress.emit(release, _accepted(release))
    document = advice.canonical_document
    assert set(document) == {
        "categories",
        "provenance",
        "releaseId",
        "rcaSessionId",
        "risk",
        "schema",
    }
    categories = _map(document["categories"])
    assert set(categories) == {field.value for field in RcaResponseField}
    assert categories["candidateExplanations"] == ["cost drag explains the shortfall"]
    assert document["risk"] == release.risk.canonical_document
    assert document["provenance"] == {
        "releaseContentSha256": release.content_sha256,
        "transformationManifestSha256": hashlib.sha256(
            json.dumps(release.manifest.canonical_document, **MANIFEST_JSON).encode(
                "utf-8"
            )
        ).hexdigest(),
        "artifact/metrics.txt": release.manifest.artifact_sha256["metrics.txt"],
    }
    dumped = json.dumps(document, sort_keys=True)
    for token in (
        "asset_",
        "exp_",
        "strat_",
        "lin_",
        "AAPL",
        "momentum-v9",
        "experiment-77",
        "lineage-12",
        "[path]",
        "[contact]",
        "[timing:ms3]",
        "filled t=",
    ):
        assert token not in dumped


def test_advice_egress_replaces_aliases_and_blocks_raw_identifiers() -> None:
    release = _release()
    alias = release.identity_mapping.alias_for(IdentitySurface.SYMBOL, "AAPL")
    advice = AdviceEgress.emit(
        release,
        _accepted(
            release,
            candidateExplanations=[f"the {alias} book absorbed flows steadily"],
        ),
    )
    text = advice.categories["candidateExplanations"][0]
    assert "[symbol:1]" in text
    assert alias not in text
    with pytest.raises(IdentifierLeakError):
        AdviceEgress.emit(release, _accepted(release, candidateExplanations=["AAPL"]))


def test_advice_egress_blocks_verbatim_quotation_from_the_run() -> None:
    release = _release()
    artifact = release.artifacts["metrics.txt"]
    start = artifact.index("[timing:ms3]")
    quotation = artifact[start - 4 : start + 60]
    with pytest.raises(AdviceEgressError) as error:
        AdviceEgress.emit(
            release,
            _accepted(release, candidateExplanations=[quotation]),
        )
    message = str(error.value)
    assert "verbatim" in message
    assert "metrics.txt" in message
    assert "[timing:ms3]" not in message


def test_advice_egress_blocks_web_query_linkage_and_sanitizes_urls() -> None:
    release = _release()
    for probe in (
        "search?q=momentum fills",
        "site:internal.example.com fills",
        "ask google about the fills",
    ):
        with pytest.raises(AdviceEgressError):
            AdviceEgress.emit(
                release, _accepted(release, candidateExplanations=[probe])
            )
    advice = AdviceEgress.emit(
        release,
        _accepted(
            release,
            candidateExplanations=["see https://internal.example.com report"],
        ),
    )
    assert "[endpoint]" in advice.categories["candidateExplanations"][0]
    assert "internal.example.com" not in advice.categories["candidateExplanations"][0]


def test_advice_egress_rejects_rejected_or_foreign_responses() -> None:
    release = _release()
    bundle = _assemble(release)
    rejected = record_response(bundle, _response(strategyEdit="edit"))
    with pytest.raises(AdviceEgressError):
        AdviceEgress.emit(release, rejected)
    other = _release(rca_session_id="rca-session-2", release_salt="salt-beta")
    with pytest.raises(AdviceEgressError):
        AdviceEgress.emit(other, _accepted(release))
    with pytest.raises(AdviceEgressError):
        AdviceEgress.emit(release, "not-a-verdict")  # ty: ignore[invalid-argument-type]


def test_advice_document_is_deterministic_with_injected_timestamp() -> None:
    release = _release()
    first = AdviceEgress.emit(release, _accepted(release), recorded_at=RECORDED_AT)
    second = AdviceEgress.emit(release, _accepted(release), recorded_at=RECORDED_AT)
    assert first.canonical_document == second.canonical_document
    assert first.content_sha256 == second.content_sha256
    assert first.canonical_document["recordedAt"] == RECORDED_AT
    canonical = json.dumps(
        first.canonical_document, sort_keys=True, separators=(",", ":")
    )
    assert canonical == json.dumps(
        json.loads(canonical), sort_keys=True, separators=(",", ":")
    )
    plain = AdviceEgress.emit(release, _accepted(release))
    assert "recordedAt" not in plain.canonical_document
    for bad in ("not-a-date", "2026-03-02T09:30:00"):
        with pytest.raises(AdviceEgressError):
            AdviceEgress.emit(release, _accepted(release), recorded_at=bad)


# AC4: the revision handoff returns to exploratory or validation data only.


def test_revision_handoff_records_consumption_and_modifiers() -> None:
    release = _release()
    accepted = _accepted(release)
    document = RevisionHandoff.from_accepted_response(
        accepted, target_grade=EvidenceGrade.HISTORICAL_VALIDATION
    )
    consumed = _map(document.canonical_document["consumed"])
    assert consumed["protectedResult"] is True
    assert consumed["consumedForDiagnosis"] is True
    assert consumed["diagnosisResponseSha256"] == accepted.response_sha256
    assert consumed["releaseContentSha256"] == release.content_sha256
    reuse = _map(document.canonical_document["reuseModifiers"])
    assert reuse["sealedWindowReuse"] == "barred"
    assert reuse["fullResultRcaAdaptation"] == "strongest-selection-reuse-modifier"
    assert reuse["targetGrade"] == "historical_validation"
    assert document.canonical_document["adaptation"] == {
        "requiresNewContractVersion": True,
        "requiresNewLineageVersion": True,
    }
    assert document.canonical_document["allowedGrades"] == [
        "exploratory",
        "historical_validation",
    ]
    assert document.canonical_document["barredGrades"] == [
        "sealed_historical",
        "forward_observation",
    ]
    assert document.canonical_document["consumedEvidenceTier"] == "cutoff-constrained"
    assert document.canonical_document["evidenceModifiers"] == [
        "adaptive-selection",
        "reused",
        "transformed",
    ]
    assert document.release_id == release.release_id
    assert document.rca_session_id == release.rca_session_id


def test_revision_handoff_rejects_barred_target_grades() -> None:
    accepted = _accepted(_release())
    for grade in (
        EvidenceGrade.SEALED_HISTORICAL,
        EvidenceGrade.FORWARD_OBSERVATION,
    ):
        with pytest.raises(RevisionHandoffError):
            RevisionHandoff.from_accepted_response(accepted, target_grade=grade)
    with pytest.raises(RevisionHandoffError):
        RevisionHandoff.from_accepted_response(
            accepted,
            target_grade="exploratory",  # ty: ignore[invalid-argument-type]
        )


def test_revision_handoff_rejects_rejected_or_unlinked_verdicts() -> None:
    release = _release()
    bundle = _assemble(release)
    rejected = record_response(bundle, _response(strategyEdit="edit"))
    with pytest.raises(RevisionHandoffError):
        RevisionHandoff.from_accepted_response(
            rejected, target_grade=EvidenceGrade.EXPLORATORY
        )
    unlinked = RcaResponseVerdict(
        accepted=True,
        violations=(),
        response={field.value: () for field in RcaResponseField},
    )
    with pytest.raises(RevisionHandoffError):
        RevisionHandoff.from_accepted_response(
            unlinked, target_grade=EvidenceGrade.EXPLORATORY
        )


def test_revision_handoff_is_deterministic_sorted_key_json_v1() -> None:
    release = _release()
    first = RevisionHandoff.from_accepted_response(
        _accepted(release),
        target_grade=EvidenceGrade.HISTORICAL_VALIDATION,
        recorded_at=RECORDED_AT,
    )
    second = RevisionHandoff.from_accepted_response(
        _accepted(release),
        target_grade=EvidenceGrade.HISTORICAL_VALIDATION,
        recorded_at=RECORDED_AT,
    )
    assert first.canonical_document == second.canonical_document
    assert first.content_sha256 == second.content_sha256
    canonical = json.dumps(
        first.canonical_document, sort_keys=True, separators=(",", ":")
    )
    assert canonical == json.dumps(
        json.loads(canonical), sort_keys=True, separators=(",", ":")
    )
    assert first.canonical_document["recordedAt"] == RECORDED_AT
    with pytest.raises(RevisionHandoffError):
        RevisionHandoff.from_accepted_response(
            _accepted(release),
            target_grade=EvidenceGrade.EXPLORATORY,
            recorded_at="not-a-date",
        )


def test_package_exports_the_public_rca_api() -> None:
    import thesis.rca

    for name in thesis.rca.__all__:
        assert getattr(thesis.rca, name) is not None
