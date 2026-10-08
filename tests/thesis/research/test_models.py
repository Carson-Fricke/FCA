"""Research model test keys must be portable relative paths."""

import pytest

from thesis.research.models import ResearchModel, ResearchModelError

_SOURCE = (
    "use harness_strategy_sdk::export_study_program;\nexport_study_program!(Model);\n"
)


def _model(tests: dict[str, str]) -> ResearchModel:
    return ResearchModel(
        model_id="midquote-model",
        version="1.0.0",
        description="Midquote reversal study",
        source=_SOURCE,
        outputs=("sharpe",),
        harness_configuration={
            "subscriptions": [{"data_type": "quote", "symbols": ["SPY"]}]
        },
        tests=tests,
    )


@pytest.mark.parametrize(
    "key",
    ["fit.data_ok < 1.0", "../escape.rs", "/absolute.rs", "con.rs"],
)
def test_unsafe_test_key_is_rejected_at_construction(key: str) -> None:
    with pytest.raises(ResearchModelError, match="confined relative path"):
        _model({key: "test text"})


def test_portable_test_key_is_accepted() -> None:
    model = _model({"tests/model.rs": "test text"})
    assert tuple(model.tests) == ("tests/model.rs",)
