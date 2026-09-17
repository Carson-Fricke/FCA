from __future__ import annotations

from pathlib import Path

import pytest

from harness.indicators import IndicatorSpec
from thesis.research.indicators import (
    IndicatorArtifactError,
    IndicatorRef,
    InMemoryIndicatorStore,
    ScopedIndicatorResolver,
    WorkspaceIndicator,
    WorkspaceIndicatorStore,
)


def _indicator() -> WorkspaceIndicator:
    return WorkspaceIndicator(
        name="rolling_corr",
        version="1.0.0",
        description="Rolling correlation between two aligned return series.",
        source=(
            "def calculate(context, parameters):\n"
            "    left = context['left']\n"
            "    right = context['right']\n"
            "    return sum(a * b for a, b in zip(left, right)) / len(left)\n"
        ),
        parameter_schema={"window": {"type": "integer", "minimum": 2}},
        tests={"test_indicator.py": "def test_contract():\n    assert True\n"},
    )


def test_refs_keep_global_and_workspace_namespaces_explicit() -> None:
    global_ref = IndicatorRef.global_indicator("sma")
    store = InMemoryIndicatorStore()
    workspace_ref = store.publish("workspace-one", _indicator())

    assert global_ref.key == "global:sma"
    assert workspace_ref.key.startswith("workspace:workspace-one/rolling_corr@")
    assert global_ref.content_sha256 is None
    assert workspace_ref.content_sha256 == _indicator().content_sha256


def test_scoped_resolver_rejects_cross_workspace_reads() -> None:
    store = InMemoryIndicatorStore()
    reference = store.publish("workspace-one", _indicator())
    resolver = ScopedIndicatorResolver(store)

    assert isinstance(
        resolver.resolve(
            IndicatorRef.global_indicator("sma"), workspace_id="workspace-two"
        ),
        IndicatorSpec,
    )
    assert resolver.resolve(reference, workspace_id="workspace-one") == _indicator()
    with pytest.raises(IndicatorArtifactError, match="another workspace"):
        resolver.resolve(reference, workspace_id="workspace-two")


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


def test_workspace_store_publishes_visible_immutable_source(tmp_path: Path) -> None:
    store = WorkspaceIndicatorStore(_Paths(tmp_path))
    indicator = _indicator()

    reference = store.publish("workspace-one", indicator)
    loaded = store.load("workspace-one", reference)

    directory = (
        tmp_path
        / "workspace-one"
        / "indicators"
        / "rolling_corr"
        / indicator.content_sha256
    )
    assert loaded == indicator
    assert (directory / "definition.json").is_file()
    assert (directory / "indicator.py").read_text(encoding="utf-8") == indicator.source
    assert (directory / "tests" / "test_indicator.py").is_file()
    with pytest.raises(IndicatorArtifactError, match="another workspace"):
        store.load("workspace-two", reference)
