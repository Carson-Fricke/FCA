"""FCA implementation of the host's JSON workflow extension protocol."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast

from thesis.knowledge.service import KnowledgeScope, validate_scope
from thesis_runtime.contracts.codec import record_from_payload
from thesis_runtime.contracts.records import WorkflowExecution
from thesis_runtime.conversations.service import ChatService
from thesis_runtime.workflows.execution_store import ExecutionStore, StoreTransition
from thesis_runtime.workflows.phase import HandoffContext

from .activities import DevelopmentActivities, bind_activities
from .callbacks import ConversationPhase, DeterministicPhase, OneShotPhase
from .contracts.creation import HypothesisDevelopmentRequest
from .rebuild import rebuild_state
from .services import HypothesisDevelopmentServices
from .state import HypothesisDevelopmentState


@dataclass(frozen=True, slots=True)
class HypothesisExtension:
    services: HypothesisDevelopmentServices
    chat: ChatService | None
    execution_store: ExecutionStore | None
    clock: Callable[[], datetime]
    id_factory: Callable[[str], str]
    knowledge_scope: KnowledgeScope = "both"
    phase_knowledge: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_scope(self.knowledge_scope)
        collections = {}
        for phase, allowed in self.phase_knowledge.items():
            if not isinstance(phase, str) or not phase.strip():
                raise ValueError("knowledge policy requires a phase name")
            if isinstance(allowed, str) or any(
                not isinstance(item, str) or not item.strip() for item in allowed
            ):
                raise ValueError("knowledge collections must be non-empty strings")
            collections[phase] = tuple(dict.fromkeys(allowed))
        object.__setattr__(self, "phase_knowledge", MappingProxyType(collections))

    def activities(
        self, request: HypothesisDevelopmentRequest
    ) -> DevelopmentActivities:
        return bind_activities(
            request,
            self.services,
            self.chat,
            self.execution_store,
            self.clock,
            self.id_factory,
        )

    @staticmethod
    def state(
        request: HypothesisDevelopmentRequest, history: tuple[StoreTransition, ...]
    ) -> HypothesisDevelopmentState:
        from .phases import _DISCOVERY
        from .spec import piping

        return rebuild_state(
            request.workspace_id,
            request.execution_id,
            history,
            initial_phase=_DISCOVERY,
            pipes=piping(),
        )

    def invoke(
        self, operation: str, payload: Mapping[str, object]
    ) -> Mapping[str, object]:
        from .doctrine import (
            _phase_report,
            _phase_summary,
            _request_of,
            _validate_launch,
        )
        from .spec import _start_handoff, phase_declarations

        if payload.get("protocolVersion") != 2:
            raise ValueError("unsupported workflow extension protocol")
        if operation == "validate_launch":
            _validate_launch(
                cast(Mapping[str, object], payload["inputs"]),
                cast(str | None, payload["triggerMessageId"]),
                cast(str | None, payload["anchorSubAgentId"]),
            )
            return {}
        if operation == "request":
            execution = cast(
                WorkflowExecution,
                record_from_payload(
                    WorkflowExecution, cast(Mapping[str, object], payload["execution"])
                ),
            )
            return _request_of(
                execution, cast(Mapping[str, object], payload["inputs"])
            ).as_dict()
        declarations = {item.spec.phase: item.spec for item in phase_declarations()}
        if operation == "validate":
            validator = declarations[str(payload["phase"])].validate
            return {
                "failure": None
                if validator is None
                else validator(cast(Mapping[str, object], payload["completion"]))
            }
        request = HypothesisDevelopmentRequest.from_payload(
            cast(Mapping[str, object], payload["request"])
        )
        history = tuple(
            StoreTransition.from_dict(item)
            for item in cast(list[Mapping[str, object]], payload["history"])
        )
        state = self.state(request, history)
        if operation == "state":
            return {"phase": state.phase.value, "intentEpoch": state.intent_epoch}
        activities = self.activities(request)
        if operation == "report":
            active_phase = payload.get("activePhase")
            active = (
                None if active_phase is None else SimpleNamespace(phase=active_phase)
            )
            return {
                "artifact": _phase_report(activities, state, active),
                "summary": None
                if active_phase is None
                else _phase_summary(state, active_phase),
            }
        raw = cast(Mapping[str, Any], payload["handoff"])
        handoff = HandoffContext(
            source=raw["source"],
            target=raw["target"],
            completion=raw["completion"],
            trigger_message_id=raw["triggerMessageId"],
            sub_agent_id=raw["subAgentId"],
        )
        phase = declarations[state.phase.value]
        if operation == "seed":
            start = (
                _start_handoff(activities, request, handoff)
                if handoff.source is None
                else None
            )
            seed = (
                phase.seed
                if isinstance(phase, ConversationPhase | OneShotPhase)
                else None
            )
            return {
                "text": start
                or (None if seed is None else seed(activities, request, state, handoff))
            }
        if operation == "execute" and isinstance(phase, DeterministicPhase):
            return {"completion": phase.contract(activities, request, state, handoff)}
        raise ValueError(f"unknown workflow extension operation: {operation}")
