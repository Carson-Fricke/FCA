from __future__ import annotations

import pytest

from thesis.research.hypothesis import (
    FALSIFIER_CONDITIONS,
    HypothesisArtifactError,
    HypothesisDocument,
    HypothesisFalsifier,
    HypothesisFrontMatter,
    HypothesisLinks,
    HypothesisStatus,
    InMemoryHypothesisStore,
    WorkspaceHypothesisStore,
    parse_hypothesis,
    render_hypothesis,
    slugify,
)


def _front_matter() -> HypothesisFrontMatter:
    return HypothesisFrontMatter(
        hypothesis_id="momentum-breakout",
        status=HypothesisStatus.ACTIVE,
        claim="Five-day momentum in SPY continues over the next five days.",
        disagreement=(
            "The market prices momentum as noise; the continuation is underreaction "
            "to a slow information diffusion."
        ),
        risk_factor="Crowded positioning can compress the edge.",
        horizon="5d",
        universe=("SPY",),
        falsifiers=(
            HypothesisFalsifier(
                observable="sharpe",
                condition="lt",
                threshold=0.0,
                test="Backtest the momentum probe and require a negative Sharpe.",
                falsifier_id="f1",
            ),
        ),
        sizing="fixed fraction 0.1 of equity",
    )


def test_front_matter_round_trips_through_markdown() -> None:
    document = HypothesisDocument(_front_matter(), "The continuation thesis.")

    text = render_hypothesis(document)
    parsed = parse_hypothesis(text)

    assert text.startswith("---\n")
    assert parsed.front_matter == document.front_matter
    assert parsed.body == "The continuation thesis."
    assert parsed.content_sha256 == document.content_sha256


def test_render_is_deterministic_and_self_contained() -> None:
    document = HypothesisDocument(_front_matter(), "body")
    assert document.render() == document.render()
    assert document.to_bytes() == document.render().encode("utf-8")
    assert document.relative_path == "hypotheses/momentum-breakout.md"


def test_projection_updates_only_workflow_owned_fields() -> None:
    document = HypothesisDocument(_front_matter(), "agent prose")
    links = HypothesisLinks(runs=("run-1",), evidence=("a" * 64,))

    projected = document.projected(status=HypothesisStatus.SUPPORTED, links=links)

    assert projected.status is HypothesisStatus.SUPPORTED
    assert projected.front_matter.links.runs == ("run-1",)
    assert projected.body == "agent prose"
    assert document.status is HypothesisStatus.ACTIVE
    assert document.front_matter.links.runs == ()


def test_parse_rejects_malformed_artifacts() -> None:
    with pytest.raises(HypothesisArtifactError, match="front matter"):
        parse_hypothesis("no delimiters here")
    with pytest.raises(HypothesisArtifactError, match="terminated"):
        parse_hypothesis("---\n{}\n")
    with pytest.raises(HypothesisArtifactError):
        parse_hypothesis("---\nnot json\n---\n")


@pytest.mark.parametrize(
    "mutation",
    [
        {"condition": "between"},
        {"observable": ""},
        {"test": ""},
    ],
)
def test_falsifier_conditions_and_fields_are_validated(mutation) -> None:
    payload = {
        "observable": "sharpe",
        "condition": "lt",
        "threshold": 0.0,
        "test": "run the probe",
        "falsifier_id": "f1",
    }
    payload.update(mutation)
    with pytest.raises(ValueError):
        HypothesisFalsifier.model_validate(payload)
    assert "lt" in FALSIFIER_CONDITIONS


def test_front_matter_rejects_unknown_keys_and_duplicates() -> None:
    values = _front_matter().model_dump(mode="json")
    values["unexpected"] = True
    with pytest.raises(ValueError):
        HypothesisFrontMatter.model_validate(values)

    duplicate = _front_matter().model_dump(mode="json")
    duplicate["universe"] = ["SPY", "SPY"]
    with pytest.raises(ValueError, match="unique"):
        HypothesisFrontMatter.model_validate(duplicate)


def test_in_memory_store_round_trips_documents() -> None:
    document = HypothesisDocument(_front_matter(), "body")
    store = InMemoryHypothesisStore()

    assert store.save(document) == "hypotheses/momentum-breakout.md"
    assert store.load(document.hypothesis_id) == document
    assert store.list() == (document.hypothesis_id,)


def test_workspace_store_writes_and_reads_markdown(tmp_path) -> None:
    document = HypothesisDocument(_front_matter(), "body")
    store = WorkspaceHypothesisStore(tmp_path)

    relative = store.save(document)

    assert relative == "hypotheses/momentum-breakout.md"
    assert (tmp_path / relative).exists()
    assert store.load(document.hypothesis_id) == document
    assert store.list() == (document.hypothesis_id,)
    assert store.path_for(document.hypothesis_id).is_relative_to(tmp_path)

    with pytest.raises(HypothesisArtifactError):
        store.load("missing-hypothesis")


def test_slugify_is_filesystem_safe() -> None:
    assert slugify("Momentum Breakout!!") == "momentum-breakout"
    assert slugify("") == "hypothesis"

    document = HypothesisDocument(
        _front_matter().model_copy(update={"hypothesis_id": "h-execution-1"}), "body"
    )
    assert document.slug == "h-execution-1"
