"""Content-addressed statistical research models published per workspace.

A research model is not a strategy. It declares historical data configuration,
an authored Rust study program source, and the finite named observations it
promises to publish. The workspace store owns source, tests, and fitted parameters under
``models/``; execution is a separate injected workflow port.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, cast

from thesis.paths import InvalidRelativePath, confined_relative_path
from thesis.research.grammar import (
    IDENTIFIER as _IDENTIFIER,
)
from thesis.research.grammar import (
    VERSION as _VERSION,
)


class WorkspacePathProvider(Protocol):
    """Workspace path resolver boundary for filesystem-backed stores."""

    def resolve_path(
        self, workspace_id: str, relative: str, *, must_exist: bool = False
    ) -> Path: ...


MODEL_DIRECTORY = "models"

#: Bar intervals accepted by each explicit bars subscription.
BAR_INTERVALS: tuple[str, ...] = ("1min", "1d")
MARKET_SYMBOL_PATTERN = r"^[A-Z0-9][A-Z0-9._/:-]*$"

MARKET_SUBSCRIPTIONS_SCHEMA: dict[str, object] = {
    "type": "array",
    "minItems": 1,
    "items": {
        "anyOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "data_type",
                    "symbols",
                    *(["interval"] if kind == "bars" else []),
                ],
                "properties": {
                    "data_type": {"type": "string", "enum": [kind]},
                    "symbols": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "pattern": MARKET_SYMBOL_PATTERN},
                    },
                    **(
                        {"interval": {"type": "string", "enum": list(BAR_INTERVALS)}}
                        if kind == "bars"
                        else {}
                    ),
                },
            }
            for kind in ("bars", "quote", "trade")
        ]
    },
}

_HASH_DOMAIN = b"fca:thesis-research-model:v1\0"
_WORKSPACE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class ResearchModelError(ValueError):
    """A research model, dependency, or publication is invalid."""


def validate_market_subscriptions(value: object) -> None:
    """Validate an exact subscription echo without normalizing or widening it."""
    if not isinstance(value, list) or not value:
        raise ResearchModelError("subscriptions must be a non-empty list")
    intervals: set[str] = set()
    for index, item in enumerate(value):
        label = f"subscriptions[{index}]"
        if not isinstance(item, Mapping):
            raise ResearchModelError(f"{label} must be an object")
        kind = item.get("data_type")
        if not isinstance(kind, str) or kind not in ("bars", "quote", "trade"):
            raise ResearchModelError(f"{label}.data_type must be bars, quote or trade")
        expected = {"data_type", "symbols"}
        if kind == "bars":
            expected.add("interval")
        if set(item) != expected:
            raise ResearchModelError(
                f"{label} must contain exactly {', '.join(sorted(expected))}"
            )
        symbols = item["symbols"]
        if (
            not isinstance(symbols, list)
            or not symbols
            or any(
                not isinstance(symbol, str)
                or not symbol.strip()
                or symbol != symbol.strip()
                or re.fullmatch(MARKET_SYMBOL_PATTERN, symbol) is None
                for symbol in symbols
            )
        ):
            raise ResearchModelError(
                f"{label}.symbols must be a non-empty list of canonical uppercase "
                "market symbols (letters, digits and ._/:-); no padding or normalization"
            )
        if kind == "bars" and item["interval"] not in BAR_INTERVALS:
            raise ResearchModelError(
                f"{label}.interval must be one of {', '.join(BAR_INTERVALS)}"
            )
        if kind == "bars":
            intervals.add(item["interval"])
    if len(intervals) > 1:
        raise ResearchModelError("subscriptions must use a single bars interval")


def validate_market_configuration(configuration: Mapping[str, object]) -> None:
    """Reject legacy fetch fields that contradict an explicit subscription echo."""
    data = configuration.get("data", {})
    if not isinstance(data, Mapping):
        if "subscriptions" in configuration:
            raise ResearchModelError(
                "data must be an object with explicit subscriptions"
            )
        return
    if "subscriptions" not in configuration and "subscriptions" not in data:
        return
    subscriptions = configuration.get("subscriptions", data.get("subscriptions"))
    validate_market_subscriptions(subscriptions)
    assert isinstance(subscriptions, list)
    if "subscriptions" in configuration and "subscriptions" in data:
        validate_market_subscriptions(data["subscriptions"])
        if data["subscriptions"] != subscriptions:
            raise ResearchModelError(
                "data.subscriptions conflicts with model subscriptions"
            )
    symbols = {symbol for item in subscriptions for symbol in item["symbols"]}
    intervals = {
        item["interval"] for item in subscriptions if item["data_type"] == "bars"
    }
    for fields, prefix in ((configuration, ""), (data, "data.")):
        for key in ("symbols", "requiredSymbols", "required_symbols"):
            if key not in fields:
                continue
            declared = fields[key]
            if (
                not isinstance(declared, list)
                or any(not isinstance(symbol, str) for symbol in declared)
                or set(declared) != symbols
            ):
                raise ResearchModelError(
                    f"{prefix}{key} must equal the exact subscription symbol union"
                )
        for key in ("interval", "bar_interval"):
            if key in fields and (
                not isinstance(fields[key], str) or intervals != {fields[key]}
            ):
                raise ResearchModelError(
                    f"{prefix}{key} must match the single subscribed bars interval; "
                    "tick-only subscriptions cannot declare an interval"
                )


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
class ResearchModel:
    """One immutable authored Rust study program and its declared outputs.

    ``source`` is a Rust program authored against ``harness-strategy-sdk``;
    the harness JIT-compiles it into a no-order study program at run time.
    """

    model_id: str
    version: str
    description: str
    source: str
    outputs: tuple[str, ...]
    harness_configuration: Mapping[str, object]
    tests: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.model_id, str)
            or _IDENTIFIER.fullmatch(self.model_id) is None
        ):
            raise ResearchModelError("model_id must be a safe identifier")
        if not isinstance(self.version, str) or (
            _VERSION.fullmatch(self.version) is None
            and _DIGEST.fullmatch(self.version) is None
        ):
            raise ResearchModelError(
                "model version must be a content hash or historical semantic version"
            )
        if not isinstance(self.description, str) or not self.description.strip():
            raise ResearchModelError("model description must be non-empty")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ResearchModelError("model source must be non-empty")
        if len(self.source.encode("utf-8")) > 1_048_576:
            raise ResearchModelError("model source exceeds the 1 MiB limit")
        if "harness_strategy_sdk" not in self.source:
            raise ResearchModelError(
                "model source must be authored against the harness_strategy_sdk "
                "crate (Cargo dependency name harness-strategy-sdk): import it "
                "with `use harness_strategy_sdk::...`"
            )
        if "export_study_program!" not in self.source:
            raise ResearchModelError(
                "model source must export its program with a real "
                "export_study_program!(Type) invocation"
            )
        outputs = tuple(self.outputs)
        if not outputs or any(
            not isinstance(output, str) or _IDENTIFIER.fullmatch(output) is None
            for output in outputs
        ):
            raise ResearchModelError("model outputs must be safe identifiers")
        if len(set(outputs)) != len(outputs):
            raise ResearchModelError("model outputs must be unique")
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
        for path in self.tests:
            try:
                confined_relative_path(path, f"model test path {path!r}")
            except InvalidRelativePath as error:
                raise ResearchModelError(str(error)) from error
        object.__setattr__(self, "description", self.description.strip())
        object.__setattr__(self, "outputs", outputs)
        object.__setattr__(
            self,
            "harness_configuration",
            _mapping(self.harness_configuration, "harness_configuration"),
        )
        object.__setattr__(self, "tests", MappingProxyType(dict(self.tests)))
        interval = self.harness_configuration.get("interval")
        validate_market_configuration(self.harness_configuration)
        data = self.harness_configuration.get("data")
        explicit = "subscriptions" in self.harness_configuration or (
            isinstance(data, Mapping) and "subscriptions" in data
        )
        if (not explicit or "interval" in self.harness_configuration) and (
            not isinstance(interval, str) or interval not in BAR_INTERVALS
        ):
            raise ResearchModelError(
                "model harness_configuration must declare its bar interval as "
                f"one of {', '.join(BAR_INTERVALS)}"
            )

    @property
    def bar_interval(self) -> str | None:
        """Return the declared bar interval, or None for tick-only subscriptions."""
        subscriptions = self.harness_configuration.get("subscriptions")
        if "subscriptions" not in self.harness_configuration:
            data = self.harness_configuration.get("data")
            subscriptions = (
                data.get("subscriptions") if isinstance(data, Mapping) else None
            )
        if isinstance(subscriptions, list):
            intervals = {
                item["interval"]
                for item in subscriptions
                if item["data_type"] == "bars"
            }
            return next(iter(intervals)) if len(intervals) == 1 else None
        return str(self.harness_configuration["interval"])

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the location-independent model artifact."""
        return {
            "description": self.description,
            "harnessConfiguration": dict(self.harness_configuration),
            "modelId": self.model_id,
            "outputs": list(self.outputs),
            "schema": "fca-thesis-research-model-v2",
            "source": self.source,
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
    ) -> str:
        _require_model_reference(workspace_id, reference)
        self.load(workspace_id, reference)
        path, payload = _run_record(workspace_id, reference, run)
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
        (directory / "program.rs").write_text(model.source, encoding="utf-8")
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
    ) -> str:
        _require_model_reference(workspace_id, reference)
        self.load(workspace_id, reference)
        relative, payload = _run_record(workspace_id, reference, run)
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
    return ResearchModel(
        model_id=cast("str", document["modelId"]),
        version=cast("str", document["version"]),
        description=cast("str", document["description"]),
        source=cast("str", document["source"]),
        outputs=tuple(cast("Sequence[str]", document["outputs"])),
        harness_configuration=cast(
            "Mapping[str, object]", document["harnessConfiguration"]
        ),
        tests=cast("Mapping[str, str]", document["tests"]),
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
) -> tuple[str, bytes]:
    if not isinstance(run, Mapping):
        raise ResearchModelError("research model run must be a mapping")
    run_id = run.get("jobId")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ResearchModelError("research model run requires a jobId")
    body = {
        "modelReference": reference.key,
        "run": dict(run),
        "schema": "fca-thesis-research-model-run-v2",
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
    "BAR_INTERVALS",
    "MODEL_DIRECTORY",
    "MARKET_SUBSCRIPTIONS_SCHEMA",
    "MARKET_SYMBOL_PATTERN",
    "FittedModelRef",
    "InMemoryResearchModelStore",
    "ResearchModel",
    "ResearchModelError",
    "ResearchModelRef",
    "ResearchModelStore",
    "WorkspaceResearchModelStore",
    "validate_market_subscriptions",
    "validate_market_configuration",
]
