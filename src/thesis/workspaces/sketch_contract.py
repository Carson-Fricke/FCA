"""Pure contracts for evolving, committed strategy sketches.

The sketch is the research argument, not an executable strategy or a second
copy of job results. Components and evidence are immutable references. Unknown
ideas remain explicit until investigation supplies evidence.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

type _NonemptyText = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
type _Identifier = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
]
type _ArtifactIdentifier = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
]

type _Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
type _Commit = Annotated[
    str, StringConstraints(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
]


class _ClosedDocument(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, allow_inf_nan=False
    )


class _ReferencePayload(_ClosedDocument):
    sketch_id: _ArtifactIdentifier = Field(alias="sketchId")
    version: _Digest
    git_commit: _Commit = Field(alias="gitCommit")


class _EvidencePayload(_ClosedDocument):
    job_id: _Identifier = Field(alias="jobId")
    artifact_name: _NonemptyText | None = Field(default=None, alias="artifactName")


class _ComponentPayload(_ClosedDocument):
    kind: Literal["indicator", "study", "strategy"]
    program_id: _ArtifactIdentifier = Field(alias="programId")
    version: _Digest
    git_commit: _Commit = Field(alias="gitCommit")


class _StrategyComponentPayload(_ComponentPayload):
    kind: Literal["strategy"]


class _PrototypePayload(_ClosedDocument):
    """The tested policy selection, with evidence remaining in immutable jobs."""

    strategy: _StrategyComponentPayload
    parameters: dict[str, str | bool | int | float]
    candidate_job_id: _Identifier = Field(alias="candidateJobId")
    search_evidence: list[_EvidencePayload] = Field(
        alias="searchEvidence", min_length=1, max_length=512
    )


class _RepresentationPayload(_ClosedDocument):
    node_id: _Identifier = Field(alias="nodeId")
    description: _NonemptyText
    inputs: list[_Identifier] = Field(
        description=(
            "Upstream representation nodeIds, not prose or market symbols. "
            "Represent a raw market input as its own node with inputs=[]."
        )
    )
    timeframes: list[_NonemptyText] = Field(
        description=(
            "Declared bar cadences or derived observation windows. Tick input "
            "nodes may use an empty list; ticks have no bar timeframe."
        )
    )
    information_time: _NonemptyText = Field(alias="informationTime")
    component_reference: _ComponentPayload | None = Field(
        default=None, alias="componentReference"
    )
    knowledge_references: list[_NonemptyText] | None = Field(
        default=None, alias="knowledgeReferences"
    )
    fit_scope: _NonemptyText | None = Field(default=None, alias="fitScope")


class _AssumptionPayload(_ClosedDocument):
    assumption_id: _Identifier = Field(alias="assumptionId")
    claim: _NonemptyText
    status: Literal["untested", "supported", "refuted", "inconclusive"]
    investigation: _NonemptyText
    evidence: list[_EvidencePayload]
    depends_on: list[_Identifier] | None = Field(default=None, alias="dependsOn")


class _SketchPayload(_ClosedDocument):
    title: _NonemptyText
    premise: _NonemptyText
    mechanism: _NonemptyText
    profit_source: _NonemptyText = Field(alias="profitSource")
    representations: list[_RepresentationPayload] = Field(min_length=1)
    conditioning: _NonemptyText
    entry: _NonemptyText
    exit: _NonemptyText
    abstention: _NonemptyText
    assumptions: list[_AssumptionPayload] = Field(min_length=1)
    alternatives: list[_NonemptyText]
    limitations: _NonemptyText
    prototype: _PrototypePayload | None = None


def _inline_sketch_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Expose this closed document without references unsupported by tool validation."""
    schema = model.model_json_schema(by_alias=True)
    definitions = schema.pop("$defs", {})

    def inline(value: Any) -> Any:
        if isinstance(value, list):
            return [inline(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            reference = value["$ref"]
            prefix = "#/$defs/"
            if not isinstance(reference, str) or not reference.startswith(prefix):
                raise ValueError("sketch schema must contain only local definitions")
            definition = definitions[reference[len(prefix) :]]
            return inline(
                {
                    **definition,
                    **{key: item for key, item in value.items() if key != "$ref"},
                }
            )
        return {key: inline(item) for key, item in value.items()}

    return cast(dict[str, Any], inline(schema))


SKETCH_REFERENCE_SCHEMA = _inline_sketch_schema(_ReferencePayload)
SKETCH_SCHEMA = _inline_sketch_schema(_SketchPayload)


@dataclass(frozen=True, slots=True)
class StrategySketchReference:
    """An exact committed sketch identity, suitable for phase handoffs."""

    sketch_id: str
    version: str
    git_commit: str

    @classmethod
    def parse(cls, value: Mapping[str, object]) -> StrategySketchReference:
        """Reject malformed references before resolving repository content."""
        _ReferencePayload.model_validate(dict(value))
        return cls(
            cast(str, value["sketchId"]),
            cast(str, value["version"]),
            cast(str, value["gitCommit"]),
        )

    def document(self) -> dict[str, str]:
        """Return the reference-only wire representation."""
        return {
            "sketchId": self.sketch_id,
            "version": self.version,
            "gitCommit": self.git_commit,
        }

    as_dict = document


def validate_sketch(document: Mapping[str, object]) -> dict[str, object]:
    """Validate a connected, acyclic research argument and detach its JSON."""
    normalized = _SketchPayload.model_validate(dict(document)).model_dump(
        mode="json", by_alias=True, exclude_unset=True, exclude_none=True
    )
    nodes = cast(list[dict[str, Any]], normalized["representations"])
    assumptions = cast(list[dict[str, Any]], normalized["assumptions"])
    for node in nodes:
        if len(node["timeframes"]) != len(set(node["timeframes"])):
            raise ValueError("sketch representation timeframes must be unique")
    for values, id_key, dependency_key, label in (
        (nodes, "nodeId", "inputs", "representation"),
        (assumptions, "assumptionId", "dependsOn", "assumption"),
    ):
        identities = [item[id_key] for item in values]
        if len(set(identities)) != len(identities):
            raise ValueError(f"sketch {label} identifiers must be unique")
        edges = {item[id_key]: item.get(dependency_key, []) for item in values}
        if any(
            len(dependencies) != len(set(dependencies))
            for dependencies in edges.values()
        ):
            raise ValueError(f"sketch {label} dependencies must be unique")
        for identity, dependencies in edges.items():
            unknown = set(dependencies) - edges.keys()
            if unknown:
                raise ValueError(
                    f"sketch {label} {identity!r} references unknown dependencies {sorted(unknown)}"
                )
        pending = {
            identity: set(dependencies) for identity, dependencies in edges.items()
        }
        while pending:
            ready = {
                identity
                for identity, dependencies in pending.items()
                if not dependencies
            }
            if not ready:
                raise ValueError(f"sketch {label} dependencies contain a cycle")
            pending = {
                identity: dependencies - ready
                for identity, dependencies in pending.items()
                if identity not in ready
            }
    return normalized


@dataclass(frozen=True, slots=True)
class StrategySketch:
    """Validated sketch content; storage owns identity and revision history."""

    _document: Mapping[str, object]

    @classmethod
    def parse(cls, value: Mapping[str, object]) -> StrategySketch:
        """Compose the complete sketch without filling unknowns silently."""
        return cls(validate_sketch(value))

    def document(self) -> dict[str, object]:
        """Return a detached JSON copy suitable for storage and inspection."""
        return cast(dict[str, object], json.loads(json.dumps(self._document)))

    @staticmethod
    def json_schema() -> dict[str, Any]:
        """Return the authoring schema used by tools and validators."""
        return SKETCH_SCHEMA
