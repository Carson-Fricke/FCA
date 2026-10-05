"""The newly submitted model must supply its newly declared falsifiers."""

from thesis.workflows.registry.hypothesis_development.contracts.validation import (
    hypothesis_payload_errors,
)

from ._support import _hypothesis_envelope


def test_falsifier_missing_from_new_outputs_is_rejected() -> None:
    payload = _hypothesis_envelope(
        [
            {
                "falsifierId": "f1",
                "observable": "residual_ar1",
                "condition": "lt",
                "threshold": 0.0,
                "test": "Check the residual",
            }
        ]
    )

    errors = hypothesis_payload_errors(payload)

    assert any(
        "newly authored model.outputs: residual_ar1" in error for error in errors
    )


def test_inline_source_is_not_a_live_completion_field() -> None:
    payload = _hypothesis_envelope()
    model = payload["model"]
    assert isinstance(model, dict)
    model["source"] = 'metrics.publish("sharpe", 1.0);'

    errors = hypothesis_payload_errors(payload)

    assert any("hypothesis.model.source" in error for error in errors)


def test_valid_new_model_can_replace_old_output_names() -> None:
    payload = _hypothesis_envelope(
        [
            {
                "falsifierId": "f1",
                "observable": "ar1_gap",
                "condition": "lt",
                "threshold": 0.0,
                "test": "Check the revised gap",
            }
        ]
    )
    model = payload["model"]
    assert isinstance(model, dict)
    model["outputs"] = ["ar1_gap"]

    assert hypothesis_payload_errors(payload) == []
