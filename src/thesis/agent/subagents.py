"""Fresh, workspace-scoped subagent context creation."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from thesis.agent.context import (
    ContextBoundaryError,
    PromptContext,
    PublishedArtifact,
    UntrustedData,
    WorkspaceSummary,
    assemble_context,
)
from thesis.agent.skills import ThesisSkill


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True, slots=True)
class SubagentContext:
    """One fresh subagent envelope containing no inherited transcript."""

    context_id: str
    role: str
    workspace_scope: str
    created_at: datetime
    prompt: PromptContext
    parent_invocation_id: str | None = None

    def __post_init__(self) -> None:
        if not self.context_id or not self.role:
            raise ContextBoundaryError("subagent context identity is required")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ContextBoundaryError(
                "subagent clock must return a timezone-aware timestamp"
            )
        if self.prompt.workspace_scope != self.workspace_scope:
            raise ContextBoundaryError(
                "subagent prompt scope does not match envelope scope"
            )
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))

    @property
    def messages(self) -> tuple[dict[str, str], ...]:
        """Return only the detached prompt messages sent to a provider."""
        return self.prompt.as_messages()

    @property
    def transcript(self) -> None:
        """Subagent contexts deliberately have no transcript field."""
        return None


class SubagentContextFactory:
    """Construct independently identified contexts using injected boundaries."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[], str] = _new_id,
    ) -> None:
        self._clock = clock
        self._id_factory = id_factory
        self._context_ids: set[str] = set()

    def create(
        self,
        *,
        role: str,
        workspace_scope: str,
        task: str,
        skills: Iterable[ThesisSkill] = (),
        workspace_summary: WorkspaceSummary | None = None,
        published_artifacts: Iterable[PublishedArtifact] = (),
        untrusted_data: Iterable[UntrustedData] = (),
        parent_invocation_id: str | None = None,
        transcript: object = None,
    ) -> SubagentContext:
        """Create a context from scoped inputs, never from a parent transcript."""
        if transcript is not None:
            raise ContextBoundaryError(
                "subagent contexts cannot inherit unrestricted transcripts"
            )
        context_id = self._id_factory()
        if (
            not isinstance(context_id, str)
            or not context_id
            or context_id in self._context_ids
        ):
            raise ContextBoundaryError("subagent context ID must be fresh")
        self._context_ids.add(context_id)
        created_at = self._clock()
        prompt = assemble_context(
            workspace_scope=workspace_scope,
            task=task,
            role=role,
            skills=skills,
            workspace_summary=workspace_summary,
            published_artifacts=published_artifacts,
            untrusted_data=untrusted_data,
            context_id=context_id,
        )
        return SubagentContext(
            context_id=context_id,
            role=role,
            workspace_scope=workspace_scope,
            created_at=created_at,
            prompt=prompt,
            parent_invocation_id=parent_invocation_id,
        )


def create_subagent_context(
    *,
    role: str,
    workspace_scope: str,
    task: str,
    skills: Iterable[ThesisSkill] = (),
    workspace_summary: WorkspaceSummary | None = None,
    published_artifacts: Iterable[PublishedArtifact] = (),
    untrusted_data: Iterable[UntrustedData] = (),
    parent_invocation_id: str | None = None,
    transcript: object = None,
) -> SubagentContext:
    """Convenience constructor using the default injected boundaries."""
    return SubagentContextFactory().create(
        role=role,
        workspace_scope=workspace_scope,
        task=task,
        skills=skills,
        workspace_summary=workspace_summary,
        published_artifacts=published_artifacts,
        untrusted_data=untrusted_data,
        parent_invocation_id=parent_invocation_id,
        transcript=transcript,
    )


__all__ = [
    "SubagentContext",
    "SubagentContextFactory",
    "create_subagent_context",
]
