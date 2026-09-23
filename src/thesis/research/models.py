"""Content-addressed statistical research models published per workspace.

A research model is not a strategy. It declares historical data configuration,
optional global/workspace indicator bindings, source with separate ``fit`` and
``observe`` entrypoints, and the finite named observations it promises to
publish. The workspace store owns source, tests, and fitted parameters under
``models/``; execution is a separate injected workflow port.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, cast

from thesis.research.grammar import (
    IDENTIFIER as _IDENTIFIER,
)
from thesis.research.grammar import (
    NAME as _NAME,
)
from thesis.research.grammar import (
    PYTHON_IDENTIFIER as _PYTHON_IDENTIFIER,
)
from thesis.research.grammar import (
    VERSION as _VERSION,
)
from thesis.research.indicators import (
    IndicatorRef,
    IndicatorScope,
    IndicatorStore,
    WorkspaceIndicator,
    WorkspacePathProvider,
)

MODEL_DIRECTORY = "models"
_HASH_DOMAIN = b"fca:thesis-research-model:v1\0"
_WORKSPACE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class ResearchModelError(ValueError):
    """A research model, dependency, or publication is invalid."""


def _workspace_id(value: str) -> str:
    if not isinstance(value, str) or _WORKSPACE_ID.fullmatch(value) is None:
        raise ResearchModelError("workspace_id must be a safe workspace identifier")
    return value


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ResearchModelError("research model values must be JSON-shaped") from error


def _mapping(value: Mapping[str, object], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ResearchModelError(f"{label} must be a string-keyed mapping")
    _canonical_bytes(value)
    return MappingProxyType(dict(value))


@dataclass(frozen=True, slots=True)
class IndicatorBinding:
    """One explicitly scoped indicator value supplied to a research model."""

    alias: str
    scope: IndicatorScope
    name: str
    symbol: str
    parameters: Mapping[str, object] = field(default_factory=dict)
    content_sha256: str | None = None
    definition: WorkspaceIndicator | None = None

    def __post_init__(self) -> None:
        for value, label in (
            (self.alias, "indicator alias"),
            (self.symbol, "indicator symbol"),
        ):
            if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
                raise ResearchModelError(f"{label} must be a safe identifier")
        if not isinstance(self.name, str) or _NAME.fullmatch(self.name) is None:
            raise ResearchModelError("indicator name must match ^[a-z][a-z0-9_]*$")
        if not isinstance(self.scope, IndicatorScope):
            raise ResearchModelError("indicator scope must be global or workspace")
        object.__setattr__(self, "parameters", _mapping(self.parameters, "parameters"))
        if self.scope is IndicatorScope.GLOBAL:
            if self.content_sha256 is not None or self.definition is not None:
                raise ResearchModelError(
                    "global indicator bindings cannot carry workspace content"
                )
            return
        if self.definition is not None:
            if not isinstance(self.definition, WorkspaceIndicator):
                raise ResearchModelError("definition must be a WorkspaceIndicator")
            if self.definition.name != self.name:
                raise ResearchModelError(
                    "workspace indicator binding name does not match"
                )
            if self.content_sha256 not in (None, self.definition.content_sha256):
                raise ResearchModelError(
                    "workspace indicator content hash does not match"
                )
            object.__setattr__(self, "content_sha256", self.definition.content_sha256)
        if (
            self.content_sha256 is None
            or _DIGEST.fullmatch(self.content_sha256) is None
        ):
            raise ResearchModelError(
                "workspace indicator binding requires a definition or content_sha256"
            )

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the location-independent binding document."""
        return {
            "alias": self.alias,
            "contentSha256": self.content_sha256,
            "definition": (
                None if self.definition is None else self.definition.canonical_document
            ),
            "name": self.name,
            "parameters": dict(self.parameters),
            "scope": self.scope.value,
            "symbol": self.symbol,
        }

    def resolve(self, workspace_id: str, store: IndicatorStore) -> IndicatorRef:
        """Publish or resolve this binding in the selected workspace."""
        if self.scope is IndicatorScope.GLOBAL:
            return IndicatorRef.global_indicator(self.name)
        if self.definition is not None:
            reference = store.publish(workspace_id, self.definition)
        else:
            reference = IndicatorRef.workspace_indicator(
                workspace_id, self.name, self.content_sha256 or ""
            )
        store.load(workspace_id, reference)
        return reference


@dataclass(frozen=True, slots=True)
class ResearchModel:
    """One immutable statistical model and its declared observations.

    ``fit`` estimates parameters and calibration bounds from historical data.
    ``observe`` consumes those frozen parameters and produces current model
    observations. Layer B never calls ``fit``.
    """

    model_id: str
    version: str
    description: str
    source: str
    outputs: tuple[str, ...]
    harness_configuration: Mapping[str, object]
    fit_entrypoint: str = "fit"
    observe_entrypoint: str = "observe"
    indicators: tuple[IndicatorBinding, ...] = ()
    tests: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.model_id, str)
            or _IDENTIFIER.fullmatch(self.model_id) is None
        ):
            raise ResearchModelError("model_id must be a safe identifier")
        if (
            not isinstance(self.version, str)
            or _VERSION.fullmatch(self.version) is None
        ):
            raise ResearchModelError("model version must use semantic versioning")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ResearchModelError("model description must be non-empty")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ResearchModelError("model source must be non-empty")
        if len(self.source.encode("utf-8")) > 1_048_576:
            raise ResearchModelError("model source exceeds the 1 MiB limit")
        for entrypoint, label in (
            (self.fit_entrypoint, "fit_entrypoint"),
            (self.observe_entrypoint, "observe_entrypoint"),
        ):
            if (
                not isinstance(entrypoint, str)
                or _PYTHON_IDENTIFIER.fullmatch(entrypoint) is None
            ):
                raise ResearchModelError(f"{label} must be a Python identifier")
        try:
            tree = ast.parse(self.source, filename=f"<{self.model_id}>", mode="exec")
        except SyntaxError as error:
            raise ResearchModelError(f"model source does not parse: {error}") from None
        if not any(
            isinstance(node, ast.FunctionDef)
            and node.name in {self.fit_entrypoint, self.observe_entrypoint}
            for node in tree.body
        ):
            raise ResearchModelError(
                "model source must define fit(context) and observe(fit, context)"
            )
        defined = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        missing = [
            entrypoint
            for entrypoint in (self.fit_entrypoint, self.observe_entrypoint)
            if entrypoint not in defined
        ]
        if missing:
            raise ResearchModelError(
                f"model source is missing entrypoints: {sorted(missing)}"
            )
        outputs = tuple(self.outputs)
        if not outputs or any(
            not isinstance(output, str) or _IDENTIFIER.fullmatch(output) is None
            for output in outputs
        ):
            raise ResearchModelError("model outputs must be safe identifiers")
        if len(set(outputs)) != len(outputs):
            raise ResearchModelError("model outputs must be unique")
        indicators = tuple(self.indicators)
        if not all(isinstance(item, IndicatorBinding) for item in indicators):
            raise ResearchModelError("model indicators must be IndicatorBinding values")
        if len({item.alias for item in indicators}) != len(indicators):
            raise ResearchModelError("model indicator aliases must be unique")
        if (
            not isinstance(self.tests, Mapping)
            or not self.tests
            or not all(
                isinstance(path, str) and path and isinstance(content, str)
                for path, content in self.tests.items()
            )
        ):
            raise ResearchModelError(
                "model tests must be a non-empty path-to-text mapping"
            )
        object.__setattr__(self, "description", self.description.strip())
        object.__setattr__(self, "outputs", outputs)
        object.__setattr__(self, "indicators", indicators)
        object.__setattr__(
            self,
            "harness_configuration",
            _mapping(self.harness_configuration, "harness_configuration"),
        )
        object.__setattr__(self, "tests", MappingProxyType(dict(self.tests)))

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the location-independent model artifact."""
        return {
            "description": self.description,
            "fitEntrypoint": self.fit_entrypoint,
            "harnessConfiguration": dict(self.harness_configuration),
            "indicators": [item.canonical_document for item in self.indicators],
            "modelId": self.model_id,
            "outputs": list(self.outputs),
            "schema": "fca-thesis-research-model-v1",
            "source": self.source,
            "observeEntrypoint": self.observe_entrypoint,
            "tests": dict(self.tests),
            "version": self.version,
        }

    @property
    def content_sha256(self) -> str:
        """Return the domain-separated content identity."""
        return hashlib.sha256(
            _HASH_DOMAIN + _canonical_bytes(self.canonical_document)
        ).hexdigest()

    def reference(self, workspace_id: str) -> ResearchModelRef:
        """Bind this location-independent model to one workspace."""
        return ResearchModelRef(workspace_id, self.model_id, self.content_sha256)


@dataclass(frozen=True, slots=True)
class ResearchModelRef:
    """One immutable workspace-owned research model reference."""

    workspace_id: str
    model_id: str
    content_sha256: str

    def __post_init__(self) -> None:
        _workspace_id(self.workspace_id)
        if _IDENTIFIER.fullmatch(self.model_id) is None:
            raise ResearchModelError("model_id must be a safe identifier")
        if _DIGEST.fullmatch(self.content_sha256) is None:
            raise ResearchModelError("content_sha256 must be a lowercase SHA-256")

    @property
    def key(self) -> str:
        """Return the portable workspace-local model reference."""
        return (
            f"workspace-model:{self.workspace_id}/{self.model_id}@{self.content_sha256}"
        )

    @property
    def relative_path(self) -> str:
        """Return the workspace-relative publication directory."""
        return f"{MODEL_DIRECTORY}/{self.model_id}/{self.content_sha256}"


@dataclass(frozen=True, slots=True)
class FittedModelRef:
    """A frozen Layer A fit bound to one model and workspace."""

    workspace_id: str
    model_id: str
    model_content_sha256: str
    fit_sha256: str

    def __post_init__(self) -> None:
        _workspace_id(self.workspace_id)
        if _IDENTIFIER.fullmatch(self.model_id) is None:
            raise ResearchModelError("model_id must be a safe identifier")
        if _DIGEST.fullmatch(self.model_content_sha256) is None:
            raise ResearchModelError("model_content_sha256 must be a lowercase SHA-256")
        if _DIGEST.fullmatch(self.fit_sha256) is None:
            raise ResearchModelError("fit_sha256 must be a lowercase SHA-256")

    @property
    def key(self) -> str:
        """Return the portable fitted-model reference."""
        return (
            f"workspace-fit:{self.workspace_id}/{self.model_id}@"
            f"{self.model_content_sha256}/{self.fit_sha256}"
        )

    @property
    def relative_path(self) -> str:
        """Return the workspace-relative fit artifact path."""
        return (
            f"{MODEL_DIRECTORY}/{self.model_id}/{self.model_content_sha256}/"
            f"fits/{self.fit_sha256}.json"
        )


class ResearchModelStore(Protocol):
    """Workspace-scoped model publication boundary."""

    def publish(self, workspace_id: str, model: ResearchModel) -> ResearchModelRef:
        """Publish one immutable model in a workspace."""

    def load(self, workspace_id: str, reference: ResearchModelRef) -> ResearchModel:
        """Resolve a model only inside its owning workspace."""

    def record_run(
        self,
        workspace_id: str,
        reference: ResearchModelRef,
        run: Mapping[str, object],
        indicator_references: Sequence[IndicatorRef],
    ) -> str:
        """Publish one model-run evidence record and return its workspace path."""

    def publish_fit(
        self,
        workspace_id: str,
        reference: ResearchModelRef,
        fit: Mapping[str, float],
    ) -> FittedModelRef:
        """Publish immutable Layer A fit parameters."""

    def load_fit(
        self, workspace_id: str, reference: FittedModelRef
    ) -> Mapping[str, float]:
        """Load a frozen fit only inside its owning workspace."""


class InMemoryResearchModelStore:
    """Deterministic workspace-scoped model store used by tests."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], ResearchModel] = {}
        self._runs: dict[tuple[str, str], bytes] = {}
        self._fits: dict[tuple[str, str], Mapping[str, float]] = {}

    def publish(self, workspace_id: str, model: ResearchModel) -> ResearchModelRef:
        workspace_id = _workspace_id(workspace_id)
        if not isinstance(model, ResearchModel):
            raise TypeError("model must be a ResearchModel")
        reference = model.reference(workspace_id)
        self._items[(workspace_id, model.content_sha256)] = model
        return reference

    def load(self, workspace_id: str, reference: ResearchModelRef) -> ResearchModel:
        _require_model_reference(workspace_id, reference)
        try:
            return self._items[(workspace_id, reference.content_sha256)]
        except KeyError as error:
            raise ResearchModelError(
                f"research model {reference.key!r} was not found"
            ) from error

    def record_run(
        self,
        workspace_id: str,
        reference: ResearchModelRef,
        run: Mapping[str, object],
        indicator_references: Sequence[IndicatorRef],
    ) -> str:
        _require_model_reference(workspace_id, reference)
        self.load(workspace_id, reference)
        path, payload = _run_record(workspace_id, reference, run, indicator_references)
        self._runs[(workspace_id, path)] = payload
        return path

    def run_artifact(self, workspace_id: str, path: str) -> bytes:
        """Return a recorded run artifact for deterministic tests."""
        try:
            return self._runs[(workspace_id, path)]
        except KeyError as error:
            raise ResearchModelError(
                "research model run artifact was not found"
            ) from error

    def publish_fit(
        self,
        workspace_id: str,
        reference: ResearchModelRef,
        fit: Mapping[str, float],
    ) -> FittedModelRef:
        _require_model_reference(workspace_id, reference)
        self.load(workspace_id, reference)
        normalized = _fit_mapping(fit)
        fit_reference = _fit_reference(workspace_id, reference, normalized)
        self._fits[(workspace_id, fit_reference.fit_sha256)] = MappingProxyType(
            normalized
        )
        return fit_reference

    def load_fit(
        self, workspace_id: str, reference: FittedModelRef
    ) -> Mapping[str, float]:
        _require_fit_reference(workspace_id, reference)
        try:
            return self._fits[(workspace_id, reference.fit_sha256)]
        except KeyError as error:
            raise ResearchModelError("fitted model artifact was not found") from error


class WorkspaceResearchModelStore:
    """Filesystem model publication under a confined workspace resolver."""

    def __init__(self, workspaces: WorkspacePathProvider) -> None:
        if not callable(getattr(workspaces, "resolve_path", None)):
            raise TypeError("workspaces must implement WorkspacePathProvider")
        self._workspaces = workspaces

    def publish(self, workspace_id: str, model: ResearchModel) -> ResearchModelRef:
        workspace_id = _workspace_id(workspace_id)
        if not isinstance(model, ResearchModel):
            raise TypeError("model must be a ResearchModel")
        reference = model.reference(workspace_id)
        directory = self._workspaces.resolve_path(workspace_id, reference.relative_path)
        if directory.exists():
            existing = self.load(workspace_id, reference)
            if existing.canonical_document != model.canonical_document:
                raise ResearchModelError("immutable model path has different content")
            return reference
        directory.mkdir(parents=True, exist_ok=False)
        manifest = {
            "contentSha256": model.content_sha256,
            "model": model.canonical_document,
            "schema": "fca-thesis-research-model-publication-v1",
            "workspaceId": workspace_id,
        }
        (directory / "definition.json").write_bytes(_canonical_bytes(manifest) + b"\n")
        (directory / "model.py").write_text(model.source, encoding="utf-8")
        for path, content in model.tests.items():
            target = _confined_path(directory / "tests", path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return reference

    def load(self, workspace_id: str, reference: ResearchModelRef) -> ResearchModel:
        _require_model_reference(workspace_id, reference)
        path = self._workspaces.resolve_path(
            workspace_id, f"{reference.relative_path}/definition.json", must_exist=True
        )
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            model = _model_from_document(document["model"])
        except (
            KeyError,
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise ResearchModelError(
                "workspace research model artifact is invalid"
            ) from error
        if document.get("workspaceId") != workspace_id:
            raise ResearchModelError(
                "research model artifact belongs to another workspace"
            )
        if document.get("contentSha256") != model.content_sha256:
            raise ResearchModelError(
                "research model artifact content hash does not match"
            )
        if model.content_sha256 != reference.content_sha256:
            raise ResearchModelError(
                "research model reference content hash does not match"
            )
        return model

    def record_run(
        self,
        workspace_id: str,
        reference: ResearchModelRef,
        run: Mapping[str, object],
        indicator_references: Sequence[IndicatorRef],
    ) -> str:
        _require_model_reference(workspace_id, reference)
        self.load(workspace_id, reference)
        relative, payload = _run_record(
            workspace_id, reference, run, indicator_references
        )
        path = self._workspaces.resolve_path(workspace_id, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_bytes() != payload:
            raise ResearchModelError("immutable model run path has different content")
        path.write_bytes(payload)
        return relative

    def publish_fit(
        self,
        workspace_id: str,
        reference: ResearchModelRef,
        fit: Mapping[str, float],
    ) -> FittedModelRef:
        _require_model_reference(workspace_id, reference)
        self.load(workspace_id, reference)
        normalized = _fit_mapping(fit)
        fit_reference = _fit_reference(workspace_id, reference, normalized)
        path = self._workspaces.resolve_path(workspace_id, fit_reference.relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = _fit_payload(workspace_id, reference, fit_reference, normalized)
        if path.exists() and path.read_bytes() != payload:
            raise ResearchModelError(
                "immutable fitted model path has different content"
            )
        path.write_bytes(payload)
        return fit_reference

    def load_fit(
        self, workspace_id: str, reference: FittedModelRef
    ) -> Mapping[str, float]:
        _require_fit_reference(workspace_id, reference)
        path = self._workspaces.resolve_path(
            workspace_id, reference.relative_path, must_exist=True
        )
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            fit = _fit_mapping(document["fit"])
        except (
            KeyError,
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise ResearchModelError("fitted model artifact is invalid") from error
        if document.get("workspaceId") != workspace_id:
            raise ResearchModelError("fitted model belongs to another workspace")
        if document.get("fitSha256") != reference.fit_sha256:
            raise ResearchModelError("fitted model hash does not match")
        return MappingProxyType(fit)


def _model_from_document(document: Mapping[str, object]) -> ResearchModel:
    raw_indicators = cast("Sequence[Mapping[str, object]]", document["indicators"])
    indicators = tuple(_binding_from_document(item) for item in raw_indicators)
    return ResearchModel(
        model_id=cast("str", document["modelId"]),
        version=cast("str", document["version"]),
        description=cast("str", document["description"]),
        source=cast("str", document["source"]),
        outputs=tuple(cast("Sequence[str]", document["outputs"])),
        harness_configuration=cast(
            "Mapping[str, object]", document["harnessConfiguration"]
        ),
        fit_entrypoint=cast("str", document.get("fitEntrypoint", "fit")),
        observe_entrypoint=cast("str", document.get("observeEntrypoint", "observe")),
        indicators=indicators,
        tests=cast("Mapping[str, str]", document["tests"]),
    )


def _binding_from_document(document: Mapping[str, object]) -> IndicatorBinding:
    definition_document = document.get("definition")
    if definition_document is None:
        definition = None
    elif isinstance(definition_document, Mapping):
        definition = WorkspaceIndicator(
            name=cast("str", definition_document["name"]),
            version=cast("str", definition_document["version"]),
            description=cast("str", definition_document["description"]),
            source=cast("str", definition_document["source"]),
            entrypoint=cast("str", definition_document["entrypoint"]),
            parameter_schema=cast(
                "Mapping[str, object]", definition_document["parameterSchema"]
            ),
            tests=cast("Mapping[str, str]", definition_document["tests"]),
        )
    else:
        raise ResearchModelError("workspace indicator definition must be a mapping")
    return IndicatorBinding(
        alias=cast("str", document["alias"]),
        scope=IndicatorScope(cast("str", document["scope"])),
        name=cast("str", document["name"]),
        symbol=cast("str", document["symbol"]),
        parameters=cast("Mapping[str, object]", document["parameters"]),
        content_sha256=cast("str | None", document.get("contentSha256")),
        definition=definition,
    )


def _require_model_reference(workspace_id: str, reference: ResearchModelRef) -> None:
    workspace_id = _workspace_id(workspace_id)
    if not isinstance(reference, ResearchModelRef):
        raise ResearchModelError("reference must be a ResearchModelRef")
    if reference.workspace_id != workspace_id:
        raise ResearchModelError(
            "research model reference belongs to another workspace"
        )


def _run_record(
    workspace_id: str,
    reference: ResearchModelRef,
    run: Mapping[str, object],
    indicator_references: Sequence[IndicatorRef],
) -> tuple[str, bytes]:
    if not isinstance(run, Mapping):
        raise ResearchModelError("research model run must be a mapping")
    run_id = run.get("jobId")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ResearchModelError("research model run requires a jobId")
    references = tuple(indicator_references)
    if not all(isinstance(item, IndicatorRef) for item in references):
        raise ResearchModelError(
            "indicator_references must contain IndicatorRef values"
        )
    body = {
        "indicatorReferences": [item.as_dict() for item in references],
        "modelReference": reference.key,
        "run": dict(run),
        "schema": "fca-thesis-research-model-run-v1",
        "workspaceId": workspace_id,
    }
    digest = hashlib.sha256(_canonical_bytes(body)).hexdigest()
    document = {**body, "contentSha256": digest}
    relative = f"{MODEL_DIRECTORY}/runs/{digest[:24]}.json"
    return relative, _canonical_bytes(document) + b"\n"


def _fit_mapping(value: Mapping[str, float]) -> dict[str, float]:
    if not isinstance(value, Mapping) or not value:
        raise ResearchModelError("model fit must be a non-empty mapping")
    normalized: dict[str, float] = {}
    for name, number in value.items():
        if not isinstance(name, str) or not name.isidentifier():
            raise ResearchModelError("fit parameter names must be identifiers")
        if isinstance(number, bool) or not isinstance(number, int | float):
            raise ResearchModelError(f"fit parameter {name!r} must be numeric")
        converted = float(number)
        if not math.isfinite(converted):
            raise ResearchModelError(f"fit parameter {name!r} must be finite")
        normalized[name] = converted
    return normalized


def _fit_reference(
    workspace_id: str,
    model_reference: ResearchModelRef,
    fit: Mapping[str, float],
) -> FittedModelRef:
    fit_sha256 = hashlib.sha256(
        _HASH_DOMAIN
        + _canonical_bytes({"fit": dict(fit), "modelReference": model_reference.key})
    ).hexdigest()
    return FittedModelRef(
        workspace_id,
        model_reference.model_id,
        model_reference.content_sha256,
        fit_sha256,
    )


def _fit_payload(
    workspace_id: str,
    model_reference: ResearchModelRef,
    fit_reference: FittedModelRef,
    fit: Mapping[str, float],
) -> bytes:
    return (
        _canonical_bytes(
            {
                "fit": dict(fit),
                "fitSha256": fit_reference.fit_sha256,
                "modelReference": model_reference.key,
                "schema": "fca-thesis-fitted-research-model-v1",
                "workspaceId": workspace_id,
            }
        )
        + b"\n"
    )


def _require_fit_reference(workspace_id: str, reference: FittedModelRef) -> None:
    _workspace_id(workspace_id)
    if not isinstance(reference, FittedModelRef):
        raise ResearchModelError("reference must be a FittedModelRef")
    if reference.workspace_id != workspace_id:
        raise ResearchModelError("fitted model belongs to another workspace")


def _confined_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve(strict=False)
    boundary = root.resolve(strict=False)
    if not candidate.is_relative_to(boundary):
        raise ResearchModelError("model test path escapes its artifact")
    return candidate


__all__ = [
    "MODEL_DIRECTORY",
    "FittedModelRef",
    "InMemoryResearchModelStore",
    "IndicatorBinding",
    "ResearchModel",
    "ResearchModelError",
    "ResearchModelRef",
    "ResearchModelStore",
    "WorkspaceResearchModelStore",
]
