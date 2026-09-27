from __future__ import annotations

from pathlib import Path

import pytest

from thesis.research.models import (
    ResearchModel,
    ResearchModelError,
    WorkspaceResearchModelStore,
)

_STUDY_SOURCE = """use harness_strategy_sdk::{
    export_study_program, MetricPublisher, QuoteEvent, StudyProgram, TradeEvent,
};
use harness_strategy_sdk::serde_json::Value;

struct PairModel;

impl StudyProgram for PairModel {
    fn new(_parameters: Value) -> Self {
        Self
    }

    fn on_quote(&mut self, _event: &QuoteEvent) {}

    fn on_trade(&mut self, _event: &TradeEvent) {}

    fn on_stop(&mut self, metrics: &mut MetricPublisher) {
        let _ = metrics.publish("observed_metric", 1.0);
    }
}

export_study_program!(PairModel);
"""


def _model() -> ResearchModel:
    return ResearchModel(
        model_id="pair-model",
        version="1.0.0",
        description="Pair regularity model.",
        source=_STUDY_SOURCE,
        outputs=("observed_metric",),
        harness_configuration={
            "symbols": ["MSTR", "IBIT"],
            "seed": 7,
            "start": "2024-01-01T00:00:00Z",
            "end": "2024-03-01T00:00:00Z",
        },
        tests={"test_model.txt": "Covered by the harness jit suite.\n"},
    )


def test_workspace_model_publication_is_visible_and_content_addressed(
    tmp_path: Path,
) -> None:
    class Paths:
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

    store = WorkspaceResearchModelStore(Paths(tmp_path))
    model = _model()
    reference = store.publish("workspace-one", model)

    directory = tmp_path / "workspace-one" / reference.relative_path
    assert (directory / "definition.json").is_file()
    assert (directory / "program.rs").read_text(encoding="utf-8") == model.source
    assert store.load("workspace-one", reference) == model

    fit_reference = store.publish_fit("workspace-one", reference, {"beta": 0.8})
    assert store.load_fit("workspace-one", fit_reference) == {"beta": 0.8}
    assert (tmp_path / "workspace-one" / fit_reference.relative_path).is_file()


def test_model_source_must_be_sdk_authored() -> None:
    with pytest.raises(ResearchModelError, match="harness-strategy-sdk"):
        ResearchModel(
            model_id="pair-model",
            version="1.0.0",
            description="Pair regularity model.",
            source="def fit(context):\n    return {}\n",
            outputs=("observed_metric",),
            harness_configuration={},
            tests={},
        )

    with pytest.raises(ResearchModelError, match="export_study_program!"):
        ResearchModel(
            model_id="pair-model",
            version="1.0.0",
            description="Pair regularity model.",
            source="use harness_strategy_sdk::StudyProgram;\nfn main() {}\n",
            outputs=("observed_metric",),
            harness_configuration={},
            tests={},
        )
