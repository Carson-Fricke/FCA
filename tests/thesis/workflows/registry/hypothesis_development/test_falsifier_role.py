"""The falsifier role: default, wire acceptance, and composer pass-through."""

from __future__ import annotations

import pytest

from thesis.agent.runtime.schema import violations
from thesis.research.hypothesis import (
    FalsifierRole,
    HypothesisDocument,
    HypothesisFalsifier,
    HypothesisFrontMatter,
    HypothesisStatus,
)
from thesis.workflows.registry.hypothesis_development.composers.hypothesis import (
    _compose_falsifiers,
)
from thesis.workflows.registry.hypothesis_development.contracts.validation import (
    HYPOTHESIS_PAYLOAD_SCHEMA,
    hypothesis_payload_errors,
)

from ._support import _hypothesis_envelope


def _falsifier_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "falsifier_id": "f1",
        "observable": "sharpe",
        "condition": "lt",
        "threshold": 0.0,
        "test": "Run the momentum probe and require a negative Sharpe.",
    }
    payload.update(overrides)
    return payload


def test_role_defaults_to_claim_and_accepts_every_declared_role() -> None:
    default = HypothesisFalsifier.model_validate(_falsifier_payload())
    assert default.role is FalsifierRole.CLAIM
    for role in FalsifierRole:
        validated = HypothesisFalsifier.model_validate(
            _falsifier_payload(role=role.value)
        )
        assert validated.role is role
    with pytest.raises(ValueError):
        HypothesisFalsifier.model_validate(_falsifier_payload(role="sanity"))


def test_payload_schema_accepts_declared_roles_and_rejects_unknown() -> None:
    for role in ("claim", "assumption", "control"):
        payload = _hypothesis_envelope([_falsifier_payload(role=role)])
        assert hypothesis_payload_errors(payload) == []

    payload = _hypothesis_envelope([_falsifier_payload(role="sanity")])
    misses = violations(HYPOTHESIS_PAYLOAD_SCHEMA, payload, "hypothesis")
    assert any(miss.path.endswith("falsifiers[0].role") for miss in misses)
    assert hypothesis_payload_errors(payload)


def test_composer_passes_role_through() -> None:
    composed = _compose_falsifiers([_falsifier_payload(role="assumption")])
    assert composed[0].role is FalsifierRole.ASSUMPTION
    defaulted = _compose_falsifiers([_falsifier_payload()])
    assert defaulted[0].role is FalsifierRole.CLAIM


def test_front_matter_round_trips_role() -> None:
    document = HypothesisDocument(
        HypothesisFrontMatter(
            hypothesis_id="h-role",
            status=HypothesisStatus.ACTIVE,
            claim="Momentum persists",
            disagreement="The market underprices persistence",
            risk_factor="Momentum",
            horizon="daily",
            universe=("SPY",),
            falsifiers=(
                HypothesisFalsifier(
                    observable="sharpe",
                    condition="lt",
                    threshold=0.0,
                    test="Run the probe.",
                    falsifier_id="f1",
                    role=FalsifierRole.ASSUMPTION,
                ),
            ),
        ),
        body="Reasoning",
    )
    rendered = document.render()
    assert '"role": "assumption"' in rendered
    parsed = HypothesisDocument.parse(rendered)
    assert parsed.front_matter.falsifiers[0].role is FalsifierRole.ASSUMPTION
    assert parsed == document
