from __future__ import annotations

from pathlib import Path

import pytest

from thesis.research.indicators import IndicatorScope, WorkspaceIndicator
from thesis.research.models import (
    IndicatorBinding,
    ResearchModel,
    ResearchModelError,
    WorkspaceResearchModelStore,
)


class _Paths:
    def __init__(self, root: Path) -> None:
        self.root = root

    def resolve_path(
        self,
        workspace_id: str,
        relative_path: str | Path = ".",
        *,
        must_exist: bool = False,
    ) -> Path:
        boundary = (self.root / workspace_id).resolve(strict=False)
        boundary.mkdir(parents=True, exist_ok=True)
        path = (boundary / relative_path).resolve(strict=must_exist)
        if not path.is_relative_to(boundary):
            raise ValueError("escaped")
        return path


def _workspace_indicator() -> WorkspaceIndicator:
    return WorkspaceIndicator(
        name="rolling_corr",
        version="1.0.0",
        description="Rolling cross-asset correlation.",
        source="def calculate(context, parameters):\n    return 0.75\n",
        tests={"test_indicator.py": "def test_indicator():\n    assert True\n"},
    )


def _model() -> ResearchModel:
    return ResearchModel(
        model_id="mstr-ibit-relative-performance",
        version="1.0.0",
        description="Estimate MSTR/IBIT correlation and relative drift.",
        source=(
            "def fit(context):\n"
            "    return {'baseline': 0.5}\n\n"
            "def observe(fit, context):\n"
            "    return {'rolling_corr_60d_median': "
            "context['indicators']['pair_corr']}\n"
        ),
        fit_entrypoint="fit",
        observe_entrypoint="observe",
        outputs=("rolling_corr_60d_median",),
        harness_configuration={"symbols": ["MSTR", "IBIT"], "seed": 7},
        indicators=(
            IndicatorBinding(
                alias="pair_corr",
                scope=IndicatorScope.WORKSPACE,
                name="rolling_corr",
                symbol="MSTR",
                definition=_workspace_indicator(),
            ),
        ),
        tests={"test_model.py": "def test_model():\n    assert True\n"},
    )


def test_workspace_model_publication_is_visible_and_content_addressed(
    tmp_path: Path,
) -> None:
    store = WorkspaceResearchModelStore(_Paths(tmp_path))
    model = _model()

    reference = store.publish("workspace-one", model)
    loaded = store.load("workspace-one", reference)

    directory = tmp_path / "workspace-one" / reference.relative_path
    assert loaded == model
    assert reference.key.startswith("workspace-model:workspace-one/")
    assert (directory / "definition.json").is_file()
    assert (directory / "model.py").read_text(encoding="utf-8") == model.source
    fit_reference = store.publish_fit("workspace-one", reference, {"beta": 0.8})
    assert store.load_fit("workspace-one", fit_reference) == {"beta": 0.8}
    assert (tmp_path / "workspace-one" / fit_reference.relative_path).is_file()
    run_path = store.record_run(
        "workspace-one",
        reference,
        {
            "jobId": "run-one",
            "state": "completed",
            "metrics": {"rolling_corr_60d_median": "0.73"},
        },
        (),
    )
    assert (tmp_path / "workspace-one" / run_path).is_file()
    with pytest.raises(ResearchModelError, match="another workspace"):
        store.load("workspace-two", reference)


def test_model_and_strategy_are_distinct_types() -> None:
    model = _model()

    assert not hasattr(model, "strategy_id")
    assert not hasattr(model, "risk_declarations")
    assert model.indicators[0].scope is IndicatorScope.WORKSPACE
