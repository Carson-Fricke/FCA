"""Focused tests for RCA alias release and information-flow controls."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, cast

import pytest

from thesis.protected import (
    ALIASING_POLICY_VERSION,
    AliasingError,
    AliasSelectionError,
    AliasStrategyKind,
    FlowFindingKind,
    IdentifierLeakError,
    IdentityMapping,
    IdentitySurface,
    OutputSurface,
    ProtectedRcaRelease,
    RcaEvidenceTier,
    RcaReleaseRequest,
    ReleaseBlockedError,
    ReleaseGate,
    ReleaseRiskAssessment,
    StrategyGateDecision,
    assert_no_raw_identifiers,
    build_aliasing,
    sanitize_output,
    screen_source,
)

NOW = "2026-03-01T12:00:00.000000Z"

ARTIFACT = (
    "experiment-77 AAPL filled 2024-01-05T14:03:00Z in 123.4ms from "
    "C:\\Users\\alice\\run.log contact alice@example.com via "
    "https://internal.example.com feed #win"
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
        "strategy_source": "def run(config):\n    return config.universe\n",
        "artifacts": {"metrics.txt": ARTIFACT},
        "symbols": ("AAPL",),
    }
    values.update(overrides)
    return RcaReleaseRequest(**values)


def _mapping(salt: str = "salt-alpha") -> IdentityMapping:
    plan = build_aliasing(RcaEvidenceTier.CUTOFF_CONSTRAINED, _open_gate())
    return IdentityMapping.build(
        release_id="rel-test",
        salt=salt,
        plan=plan,
        values={IdentitySurface.SYMBOL: ("AAPL",)},
    )


# AC1: aliasing is selected by evidence tier and release gates, not modes.


def test_aliasing_strategy_selection_follows_tier_and_gate() -> None:
    full = build_aliasing(RcaEvidenceTier.FULL_ALIASED, _open_gate())
    assert full.strategy is AliasStrategyKind.HARDENED_DEIDENTIFICATION
    assert full.fingerprint_suppression is True
    assert full.policy_version == ALIASING_POLICY_VERSION
    assert set(full.surfaces) == set(IdentitySurface)

    cutoff = build_aliasing(RcaEvidenceTier.CUTOFF_CONSTRAINED, _open_gate())
    assert cutoff.strategy is AliasStrategyKind.IDENTITY_MASKING
    assert cutoff.fingerprint_suppression is False
    assert set(cutoff.surfaces) == set(IdentitySurface)

    summary = build_aliasing(RcaEvidenceTier.SUMMARY_BLINDED, _open_gate())
    assert summary.strategy is AliasStrategyKind.IDENTITY_MASKING
    assert IdentitySurface.SYMBOL not in summary.surfaces
    assert IdentitySurface.STRATEGY_ID in summary.surfaces
    assert IdentitySurface.TIMESTAMP in summary.surfaces


def test_aliasing_downgrades_strategy_before_blocking() -> None:
    gate = ReleaseGate(
        cross_release_disclosure_ok=True,
        decisions=(
            _decision(AliasStrategyKind.IDENTITY_MASKING),
            _decision(
                AliasStrategyKind.HARDENED_DEIDENTIFICATION,
                empirical=False,
                reason="empirical attack success above the hardened threshold",
            ),
        ),
    )
    plan = build_aliasing(RcaEvidenceTier.FULL_ALIASED, gate)
    assert plan.strategy is AliasStrategyKind.IDENTITY_MASKING


def test_aliasing_blocks_when_every_strategy_fails_the_gate() -> None:
    gate = ReleaseGate(
        cross_release_disclosure_ok=True,
        decisions=(
            _decision(
                AliasStrategyKind.IDENTITY_MASKING,
                utility=False,
                reason="diagnostic utility below the identity-masking threshold",
            ),
            _decision(
                AliasStrategyKind.HARDENED_DEIDENTIFICATION,
                empirical=False,
                reason="empirical attack success above the hardened threshold",
            ),
        ),
    )
    with pytest.raises(AliasSelectionError) as error:
        build_aliasing(RcaEvidenceTier.FULL_ALIASED, gate)
    assert isinstance(error.value, ReleaseBlockedError)
    assert any("identity-masking" in reason for reason in error.value.reasons)
    assert any("hardened-deidentification" in reason for reason in error.value.reasons)


def test_aliasing_blocks_on_cross_release_disclosure() -> None:
    gate = ReleaseGate(
        cross_release_disclosure_ok=False,
        decisions=(
            _decision(AliasStrategyKind.IDENTITY_MASKING),
            _decision(AliasStrategyKind.HARDENED_DEIDENTIFICATION),
        ),
    )
    with pytest.raises(AliasSelectionError) as error:
        build_aliasing(RcaEvidenceTier.CUTOFF_CONSTRAINED, gate)
    assert any("cross-release" in reason for reason in error.value.reasons)


def test_no_release_tier_authorizes_nothing() -> None:
    with pytest.raises(AliasSelectionError):
        build_aliasing(RcaEvidenceTier.NO_RELEASE, _open_gate())


# AC2/AC6: deterministic per-release aliases; the mapping stays internal.


def test_aliases_stable_within_release_and_differ_across_releases() -> None:
    first = _mapping("salt-alpha")
    second = _mapping("salt-alpha")
    other = _mapping("salt-beta")
    alias = first.alias_for(IdentitySurface.SYMBOL, "AAPL")
    assert alias == second.alias_for(IdentitySurface.SYMBOL, "AAPL")
    assert alias != other.alias_for(IdentitySurface.SYMBOL, "AAPL")
    assert len(set([alias, other.alias_for(IdentitySurface.SYMBOL, "AAPL")])) == 2


def test_alias_tokens_are_opaque_and_salt_keyed() -> None:
    alias = _mapping().alias_for(IdentitySurface.SYMBOL, "AAPL")
    assert alias.startswith("asset_")
    suffix = alias.removeprefix("asset_")
    assert len(suffix) == 6
    int(suffix, 16)
    assert alias != hashlib.sha256(b"AAPL").hexdigest()[:6]


def test_timestamp_counters_preserve_order_and_intervals_and_dont_link() -> None:
    moments = (
        "2024-01-05T14:03:00Z",
        "2024-01-05T14:05:00Z",
        "2024-01-06T14:03:00Z",
    )
    plan = build_aliasing(RcaEvidenceTier.CUTOFF_CONSTRAINED, _open_gate())
    mapping = IdentityMapping.build(
        release_id="rel-test",
        salt="salt-alpha",
        plan=plan,
        values={IdentitySurface.TIMESTAMP: moments},
    )
    counters = [
        int(mapping.alias_for(IdentitySurface.TIMESTAMP, moment).removeprefix("t="))
        for moment in moments
    ]
    assert counters[1] - counters[0] == 2
    assert counters[2] - counters[1] == 23 * 60 + 58
    shifted = IdentityMapping.build(
        release_id="rel-other",
        salt="salt-beta",
        plan=plan,
        values={IdentitySurface.TIMESTAMP: moments},
    )
    other = [
        int(shifted.alias_for(IdentitySurface.TIMESTAMP, moment).removeprefix("t="))
        for moment in moments
    ]
    assert other[1] - other[0] == 2
    assert other != counters


def test_apply_to_text_replaces_every_identity_surface() -> None:
    plan = build_aliasing(RcaEvidenceTier.CUTOFF_CONSTRAINED, _open_gate())
    mapping = IdentityMapping.build(
        release_id="rel-test",
        salt="salt-alpha",
        plan=plan,
        values={
            IdentitySurface.STRATEGY_ID: ("momentum-v9",),
            IdentitySurface.EXPERIMENT_ID: ("experiment-77",),
            IdentitySurface.LINEAGE_ID: ("lineage-12",),
            IdentitySurface.SYMBOL: ("AAPL",),
        },
    )
    aliased = mapping.apply_to_text(
        "experiment-77 runs momentum-v9 on AAPL (lineage-12)"
    )
    for real in ("experiment-77", "momentum-v9", "AAPL", "lineage-12"):
        assert real not in aliased
    assert "exp_" in aliased
    assert "strat_" in aliased
    assert "asset_" in aliased
    assert "lin_" in aliased


def test_duplicate_identity_value_across_surfaces_fails_closed() -> None:
    plan = build_aliasing(RcaEvidenceTier.CUTOFF_CONSTRAINED, _open_gate())
    with pytest.raises(AliasingError) as error:
        IdentityMapping.build(
            release_id="rel-test",
            salt="salt-alpha",
            plan=plan,
            values={
                IdentitySurface.EXPERIMENT_ID: ("dup-1",),
                IdentitySurface.LINEAGE_ID: ("dup-1",),
            },
        )
    assert "dup-1" not in str(error.value)
    assert "experiment-id" in str(error.value)
    assert "lineage-id" in str(error.value)


# AC3: source screening blocks identity-revealing source.


def test_screen_source_blocks_identity_revealing_content() -> None:
    source = (
        "def run():\n"
        "    # written by alice@example.com\n"
        '    return "AAPL", "2024-01-05", "C:\\\\repo\\\\notes.txt"\n'
    )
    findings = screen_source(source, known_identifiers=("AAPL",))
    kinds = {finding.kind for finding in findings}
    assert FlowFindingKind.CONTACT in kinds
    assert FlowFindingKind.IDENTITY_COMMENT in kinds
    assert FlowFindingKind.IDENTIFIER in kinds
    assert FlowFindingKind.DATE_LITERAL in kinds
    assert FlowFindingKind.ABSOLUTE_PATH in kinds
    for finding in findings:
        assert "alice@example.com" not in finding.describe()
        assert finding.line >= 1
        assert finding.column >= 1


def test_screen_source_passes_clean_source() -> None:
    source = "def run(config):\n    return config.universe\n"
    assert screen_source(source, known_identifiers=("AAPL",)) == []


# AC4: allowlist-driven side-channel sanitization.


def test_sanitize_output_removes_disallowed_side_channels() -> None:
    text = (
        "/home/dev/notes.txt dev@example.com 2024-01-05T10:00:00Z 5ms #alpha\n"
        "2024-01-05T10:00:01Z INFO started\n"
        "error=boom\n"
    )
    sanitized, removed = sanitize_output(text)
    assert sanitized == "[path] [contact] [timestamp] [timing] [tag]\n[log]\n[error]\n"
    kinds = {finding.kind for finding in removed}
    assert kinds == {
        FlowFindingKind.ABSOLUTE_PATH,
        FlowFindingKind.CONTACT,
        FlowFindingKind.TIMESTAMP,
        FlowFindingKind.TIMING,
        FlowFindingKind.TAG,
        FlowFindingKind.LOG_LINE,
        FlowFindingKind.ERROR_MESSAGE,
    }
    assert all(finding.excerpt.count("***") == 1 for finding in removed)


def test_sanitize_output_allowlist_buckets_timing() -> None:
    sanitized, removed = sanitize_output(
        "took 12.3ms from /home/dev/a.log", (OutputSurface.TIMING,)
    )
    assert "[timing:ms2]" in sanitized
    assert "/home/dev/a.log" not in sanitized
    assert {finding.kind for finding in removed} == {FlowFindingKind.ABSOLUTE_PATH}


def test_sanitize_output_is_deterministic() -> None:
    text = "2024-01-05T10:00:00Z INFO x=1 in 250.0ms path C:\\tmp\\a.log"
    assert sanitize_output(text) == sanitize_output(text)


# AC5: egress checks for advice returning to the main agent.


def test_assert_no_raw_identifiers_blocks_aliases_and_reals() -> None:
    mapping = _mapping()
    alias = mapping.alias_for(IdentitySurface.SYMBOL, "AAPL")
    with pytest.raises(IdentifierLeakError):
        assert_no_raw_identifiers(f"{alias} outperformed", mapping)
    with pytest.raises(IdentifierLeakError):
        assert_no_raw_identifiers("AAPL outperformed", mapping)
    assert_no_raw_identifiers("the selected instrument outperformed", mapping)


# Release composition: AC2-AC6.


def test_release_blocks_identity_revealing_source_without_rewrite() -> None:
    request = _request(
        strategy_source=(
            'def run():\n    # maintained by dev@corp.example\n    return "AAPL"\n'
        )
    )
    with pytest.raises(ReleaseBlockedError) as error:
        ProtectedRcaRelease.create(request)
    reasons = error.value.reasons
    assert reasons
    assert all(
        reason.startswith("strategy source reveals identity:") for reason in reasons
    )
    assert "dev@corp.example" not in str(error.value)
    assert "AAPL" not in str(error.value)


def test_release_blocks_no_release_tier() -> None:
    with pytest.raises(ReleaseBlockedError):
        ProtectedRcaRelease.create(_request(evidence_tier=RcaEvidenceTier.NO_RELEASE))


def test_release_artifacts_are_aliased_and_sanitized() -> None:
    release = ProtectedRcaRelease.create(_request())
    artifact = release.artifacts["metrics.txt"]
    for real in (
        "experiment-77",
        "AAPL",
        "2024-01-05",
        "123.4ms",
        "C:\\Users\\alice",
        "alice@example.com",
        "https://internal.example.com",
        "#win",
    ):
        assert real not in artifact
    assert "exp_" in artifact
    assert "asset_" in artifact
    assert "t=" in artifact
    assert "[timing:ms3]" in artifact
    assert "[path]" in artifact
    assert "[contact]" in artifact
    assert "[endpoint]" in artifact
    assert "[tag]" in artifact


def test_release_binds_full_artifacts_to_the_named_session() -> None:
    release = ProtectedRcaRelease.create(_request())
    payload = release.release_to_session("rca-session-1")
    assert payload["releaseId"] == release.release_id
    assert _map(payload["artifacts"])["metrics.txt"] == release.artifacts["metrics.txt"]
    with pytest.raises(ReleaseBlockedError):
        release.release_to_session("rca-session-2")


def test_released_payloads_exclude_the_identity_mapping() -> None:
    release = ProtectedRcaRelease.create(_request())
    payload = release.release_to_session("rca-session-1")
    document = {
        "aliasEntries": _map(payload["manifest"])["aliasEntries"],
        "artifacts": _map(payload["artifacts"]),
        "manifest": payload["manifest"],
        "releaseId": payload["releaseId"],
        "rcaSessionId": payload["rcaSessionId"],
    }
    dumped = json.dumps(document, sort_keys=True)
    for real in (
        "AAPL",
        "experiment-77",
        "momentum-v9",
        "lineage-12",
        "sub-1",
        "ws-test",
        "alice",
        "2024-01-05",
    ):
        assert real not in dumped
    alias_entries = _map(payload["manifest"])["aliasEntries"]
    for entry in cast(list[object], alias_entries):
        assert set(_map(entry)) == {"alias", "ordinal", "surface"}


def test_release_manifest_records_removals_and_digests() -> None:
    release = ProtectedRcaRelease.create(_request())
    document = release.manifest.canonical_document
    counts = _map(document["removedCounts"])
    assert counts["absolute-path"] == 1
    assert counts["contact"] == 1
    assert counts["endpoint"] == 1
    assert counts["tag"] == 1
    expected = hashlib.sha256(
        release.artifacts["metrics.txt"].encode("utf-8")
    ).hexdigest()
    assert _map(document["artifactSha256"])["metrics.txt"] == expected
    assert document["notes"]


def test_filter_advice_excludes_raw_aliases_and_side_channels() -> None:
    release = ProtectedRcaRelease.create(_request())
    alias = release.identity_mapping.alias_for(IdentitySurface.SYMBOL, "AAPL")
    advice = (
        f"Alias {alias} suggests latency 250.0ms after 2024-01-05; "
        "details in /tmp/run.log"
    )
    filtered = release.filter_advice(advice, from_session="rca-session-1")
    assert "[symbol:1]" in filtered
    assert alias not in filtered
    assert "250.0ms" not in filtered
    assert "/tmp/run.log" not in filtered
    assert "2024-01-05" not in filtered
    with pytest.raises(ReleaseBlockedError):
        release.filter_advice(advice, from_session="rca-session-2")
    with pytest.raises(IdentifierLeakError):
        release.filter_advice("AAPL again", from_session="rca-session-1")


def test_release_record_is_deterministic_sorted_key_json_v1() -> None:
    first = ProtectedRcaRelease.create(_request())
    second = ProtectedRcaRelease.create(_request())
    assert first.content_sha256 == second.content_sha256
    assert first.canonical_document == second.canonical_document
    document = first.canonical_document
    assert document["schema"] == "fca-thesis-protected-rca-release-v1"
    assert document["policyVersion"] == ALIASING_POLICY_VERSION
    assert document["evidenceTier"] == "cutoff-constrained"
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    assert canonical == json.dumps(
        json.loads(canonical), sort_keys=True, separators=(",", ":")
    )


def test_release_record_carries_the_risk_assessment() -> None:
    request = _request()
    release = ProtectedRcaRelease.create(request)
    assert release.risk.residual_risk
    assert release.canonical_document["risk"] == request.risk.canonical_document
    risk_document = request.risk.canonical_document
    assert risk_document["schema"] == "fca-thesis-release-risk-assessment-v1"
    assert set(risk_document) == {
        "crossReleaseDisclosure",
        "diagnosticUtility",
        "empiricalReidentificationRisk",
        "residualRisk",
        "schema",
    }
