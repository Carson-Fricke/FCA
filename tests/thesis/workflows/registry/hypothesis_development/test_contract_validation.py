"""Deterministic, complete validation of the creation completion contract."""

from __future__ import annotations

import pytest

from thesis.research.grammar import IDENTIFIER_PATTERN, VERSION_PATTERN
from thesis.workflows.registry.hypothesis_development.contracts.validation import (
    HYPOTHESIS_COMPLETION_SCHEMA,
    HYPOTHESIS_PAYLOAD_SCHEMA,
    format_contract_errors,
    hypothesis_payload_errors,
)
from thesis.workflows.registry.hypothesis_development.errors import (
    FalsificationComposeError,
)
from thesis.workflows.registry.hypothesis_development.phases.trials import (
    _draft_from_contract,
)

from ._support import _hypothesis_envelope, _model


def test_advertised_target_enum_never_invites_the_failed_phase() -> None:
    assert HYPOTHESIS_COMPLETION_SCHEMA["properties"]["target"]["enum"] == [
        "falsification_trials"
    ]


def test_valid_payload_collects_no_errors() -> None:
    payload = _hypothesis_envelope()

    assert hypothesis_payload_errors(payload) == []
    # Parity: the composer the deterministic successor phase runs also accepts
    # exactly the payloads the collector accepts.
    _draft_from_contract(payload)


def test_every_missing_field_is_reported_once_in_declaration_order() -> None:
    errors = hypothesis_payload_errors({"claim": "ok"})

    assert errors == [
        "hypothesis.disagreement: required non-empty text is missing",
        "hypothesis.model: required object is missing",
        "hypothesis.falsifiers: required sequence is missing",
    ]


def test_wrongly_typed_fields_report_the_actual_type() -> None:
    errors = hypothesis_payload_errors(
        {
            "claim": 7,
            "disagreement": "   ",
            "model": {"modelId": 1, "outputs": "sharpe"},
        }
    )

    assert "hypothesis.claim: must be text, got int" in errors
    assert "hypothesis.disagreement: must be non-empty text" in errors
    assert "hypothesis.model.modelId: must be text, got int" in errors
    assert "hypothesis.model.version: required non-empty text is missing" in errors
    assert "hypothesis.model.outputs: must be a sequence, got str" in errors
    assert "hypothesis.model.tests: required mapping is missing" in errors


def test_the_model_must_declare_the_window_it_wants_fetched() -> None:
    # A model that declares no window is how a 42-session hypothesis ended up
    # fetching 2.75 years of history: the window is the model's to specify, so
    # an absent bound is a contract problem to repair, never a silent default.
    undeclared = {**_model(), "harnessConfiguration": {"seed": 7, "symbols": ["SPY"]}}
    errors = hypothesis_payload_errors({**_hypothesis_envelope(), "model": undeclared})

    assert (
        "hypothesis.model.harnessConfiguration.start: required non-empty text is "
        "missing" in errors
    )
    assert (
        "hypothesis.model.harnessConfiguration.end: required non-empty text is "
        "missing" in errors
    )

    # Parity: the composer rejects it too, so a payload the collector refuses
    # can never reach the fetch path through a different entry point.
    try:
        _draft_from_contract({**_hypothesis_envelope(), "model": undeclared})
    except Exception:  # noqa: BLE001 - any rejection preserves the parity claim
        pass
    else:  # pragma: no cover - a live default window is the bug returning
        raise AssertionError("composer accepted a model with no declared window")


def test_falsifier_errors_report_every_field_and_index() -> None:
    errors = hypothesis_payload_errors(
        {
            "claim": "c",
            "disagreement": "d",
            "model": _model(),
            "falsifiers": [
                {
                    "falsifierId": "f1",
                    "observable": "sharpe",
                    "condition": "below",
                    "threshold": "0.5",
                    "test": "Run the probe.",
                },
                "not-an-object",
            ],
        }
    )

    assert (
        "hypothesis.falsifiers[0].condition: Input should be 'gte', 'lte', 'gt' or 'lt'"
        in errors
    )
    assert (
        "hypothesis.falsifiers[0].threshold: Value error, threshold must be a "
        "finite number" in errors
    )
    assert "hypothesis.falsifiers[1]: must be an object, got str" in errors


def test_universe_and_indicator_errors_are_precise() -> None:
    errors = hypothesis_payload_errors(
        {
            "claim": "c",
            "disagreement": "d",
            "model": {**_model(), "indicators": [{"alias": "a"}]},
            "universe": ["SPY", "bad symbol!", 3],
        }
    )

    assert (
        "hypothesis.universe[1]: must match the identifier grammar 'bad symbol!'"
        in errors
    )
    assert "hypothesis.universe[2]: must be a non-empty symbol" in errors
    assert (
        "hypothesis.model.indicators[0].scope: required non-empty text is missing"
        in errors
    )
    assert (
        "hypothesis.model.indicators[0].name: required non-empty text is missing"
        in errors
    )
    assert (
        "hypothesis.model.indicators[0].symbol: required non-empty text is missing"
        in errors
    )


def test_collection_is_deterministic() -> None:
    payload = {"claim": None, "disagreement": [], "model": "text"}

    assert hypothesis_payload_errors(payload) == hypothesis_payload_errors(payload)


def test_format_lists_every_problem_for_the_repair_turn() -> None:
    formatted = format_contract_errors(
        [
            "hypothesis.claim: required non-empty text is missing",
            "hypothesis.model: required object is missing",
        ]
    )

    assert formatted.startswith(
        "completion contract rejected with 2 problem(s) — fix every problem "
        "and call request_phase_completion again:"
    )
    assert "- hypothesis.claim: required non-empty text is missing" in formatted
    assert "- hypothesis.model: required object is missing" in formatted


# -- falsifier minimum and schema/composer parity ----------------------------


def _falsifier(**overrides: object) -> dict[str, object]:
    return {
        "falsifierId": "f1",
        "observable": "sharpe",
        "condition": "lt",
        "threshold": 0.0,
        "test": "Run the probe.",
        **overrides,
    }


def _indicator(**overrides: object) -> dict[str, object]:
    return {
        "alias": "pair_corr",
        "scope": "global",
        "name": "pair_corr",
        "symbol": "SPY",
        **overrides,
    }


def test_a_hypothesis_without_falsifiers_is_rejected_before_any_transition() -> None:
    """An empty falsifier set is the validator disagreement from the 2026-09-23
    failure: creation accepted it, the trials phase then planned zero trials and
    its own contract validator refused the empty trial list after the transition
    had committed.  Both sides now reject the contract up front."""
    missing = {
        key: value
        for key, value in _hypothesis_envelope().items()
        if key != "falsifiers"
    }
    empty = {**_hypothesis_envelope(), "falsifiers": []}

    assert hypothesis_payload_errors(missing) == [
        "hypothesis.falsifiers: required sequence is missing"
    ]
    assert hypothesis_payload_errors(empty) == [
        "hypothesis.falsifiers: must contain at least one falsifier"
    ]
    for payload in (missing, empty):
        with pytest.raises(FalsificationComposeError):
            _draft_from_contract(payload)


def test_outputs_are_identifier_strings_not_objects() -> None:
    """The incident's actual rejection: ``outputs`` arrived as ``{name, type}``
    objects while the schema promised plain strings.  Objects are reported
    precisely, and identifier strings — underscores included — pass both sides."""
    objects = {
        **_hypothesis_envelope(),
        "model": {**_model(), "outputs": [{"name": "data_ok", "type": "number"}]},
    }
    errors = hypothesis_payload_errors(objects)

    assert "hypothesis.model.outputs[0]: must be a non-empty identifier" in errors
    with pytest.raises(FalsificationComposeError):
        _draft_from_contract(objects)

    underscored = {
        **_hypothesis_envelope(),
        "model": {**_model(), "outputs": ["data_ok", "gamma_ci_upper"]},
    }
    assert hypothesis_payload_errors(underscored) == []
    _draft_from_contract(underscored)


def test_advertised_schema_is_never_weaker_than_the_composer() -> None:
    """The provider-facing schema and the collector both read the same grammar
    constants as the composer; this pins every declarative constraint the
    composer enforces to the advertised shape."""
    assert HYPOTHESIS_PAYLOAD_SCHEMA["required"] == [
        "claim",
        "disagreement",
        "falsifiers",
        "model",
    ]
    falsifiers = HYPOTHESIS_PAYLOAD_SCHEMA["properties"]["falsifiers"]
    assert falsifiers["minItems"] == 1
    assert falsifiers["items"]["additionalProperties"] is False
    assert (
        falsifiers["items"]["properties"]["observable"]["pattern"] == IDENTIFIER_PATTERN
    )
    assert set(falsifiers["items"]["required"]) == {
        "observable",
        "condition",
        "threshold",
        "test",
    }

    model = HYPOTHESIS_PAYLOAD_SCHEMA["properties"]["model"]
    assert model["required"] == [
        "modelId",
        "version",
        "description",
        "source",
        "outputs",
        "tests",
        "harnessConfiguration",
    ]
    assert model["properties"]["modelId"]["pattern"] == IDENTIFIER_PATTERN
    assert model["properties"]["version"]["pattern"] == VERSION_PATTERN
    outputs = model["properties"]["outputs"]
    assert outputs["minItems"] == 1
    assert outputs["uniqueItems"] is True
    assert outputs["items"]["pattern"] == IDENTIFIER_PATTERN
    assert model["properties"]["tests"]["minProperties"] == 1
    assert model["properties"]["harnessConfiguration"]["required"] == [
        "start",
        "end",
    ]
    indicators = model["properties"]["indicators"]["items"]
    assert indicators["required"] == ["alias", "scope", "name", "symbol"]


_REJECTED_PAYLOADS = [
    pytest.param(
        {
            key: value
            for key, value in _hypothesis_envelope().items()
            if key != "falsifiers"
        },
        id="falsifiers-missing",
    ),
    pytest.param({**_hypothesis_envelope(), "falsifiers": []}, id="falsifiers-empty"),
    pytest.param(
        {**_hypothesis_envelope(), "falsifiers": [_falsifier(unknown=True)]},
        id="falsifier-extra-key",
    ),
    pytest.param(
        {
            **_hypothesis_envelope(),
            "model": {**_model(), "outputs": [{"name": "sharpe", "type": "number"}]},
        },
        id="outputs-objects",
    ),
    pytest.param(
        {**_hypothesis_envelope(), "model": {**_model(), "outputs": []}},
        id="outputs-empty",
    ),
    pytest.param(
        {
            **_hypothesis_envelope(),
            "model": {**_model(), "outputs": ["sharpe", "sharpe"]},
        },
        id="outputs-duplicate",
    ),
    pytest.param(
        {
            **_hypothesis_envelope(),
            "model": {**_model(), "outputs": ["bad output!"]},
        },
        id="outputs-junk-symbol",
    ),
    pytest.param(
        {**_hypothesis_envelope(), "model": {**_model(), "outputs": "sharpe"}},
        id="outputs-not-a-sequence",
    ),
    pytest.param(
        {**_hypothesis_envelope(), "model": {**_model(), "modelId": "bad id"}},
        id="modelId-grammar",
    ),
    pytest.param(
        {**_hypothesis_envelope(), "model": {**_model(), "version": "1.0"}},
        id="version-not-semver",
    ),
    pytest.param(
        {**_hypothesis_envelope(), "model": {**_model(), "tests": {}}},
        id="tests-empty",
    ),
    pytest.param(
        {
            **_hypothesis_envelope(),
            "model": {**_model(), "tests": {"test_model.py": 7}},
        },
        id="tests-not-text",
    ),
    pytest.param(
        {
            **_hypothesis_envelope(),
            "model": {
                **_model(),
                "harnessConfiguration": {"end": "2024-03-01T00:00:00Z"},
            },
        },
        id="harness-window-missing-start",
    ),
    pytest.param(
        {**_hypothesis_envelope(), "model": {**_model(), "fitEntrypoint": "1fit"}},
        id="entrypoint-not-identifier",
    ),
    pytest.param({**_hypothesis_envelope(), "claim": "x" * 8_193}, id="claim-too-long"),
    pytest.param(
        {
            **_hypothesis_envelope(),
            "model": {**_model(), "indicators": [_indicator(), _indicator()]},
        },
        id="indicator-alias-duplicate",
    ),
    pytest.param(
        {
            **_hypothesis_envelope(),
            "model": {**_model(), "indicators": [_indicator(name="BadName")]},
        },
        id="indicator-name-grammar",
    ),
    pytest.param(
        {
            **_hypothesis_envelope(),
            "model": {
                **_model(),
                "indicators": [{"alias": "a", "name": "x", "symbol": "SPY"}],
            },
        },
        id="indicator-scope-missing",
    ),
]


@pytest.mark.parametrize("payload", _REJECTED_PAYLOADS)
def test_collector_and_composer_reject_the_same_payloads(
    payload: dict[str, object],
) -> None:
    """The dangerous drift is a schema/collector weaker than the composer; for
    every mirrored constraint both sides reject."""
    assert hypothesis_payload_errors(payload) != []
    with pytest.raises(FalsificationComposeError):
        _draft_from_contract(payload)


_ACCEPTED_PAYLOADS = [
    pytest.param(_hypothesis_envelope(), id="default"),
    pytest.param(
        {
            **_hypothesis_envelope(),
            "model": {**_model(), "outputs": ["data_ok", "gamma_ci_upper"]},
        },
        id="underscored-outputs",
    ),
    pytest.param(
        {
            **_hypothesis_envelope(),
            "model": {**_model(), "modelId": "_spy_momentum"},
        },
        id="leading-underscore-model-id",
    ),
]


@pytest.mark.parametrize("payload", _ACCEPTED_PAYLOADS)
def test_collector_and_composer_accept_the_same_payloads(
    payload: dict[str, object],
) -> None:
    assert hypothesis_payload_errors(payload) == []
    _draft_from_contract(payload)
