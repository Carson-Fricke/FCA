"""Strategy-design phase executor."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from thesis.experiments.ledger import AttemptKind
from thesis.strategies.validation import (
    validate_strategy_bundle,
)
from thesis.workflows.research_models import (
    study_window,
)

from ..contracts.creation import HypothesisDevelopmentRequest
from ..errors import (
    WorkflowError,
)
from ..ledger import append_attempt
from ..piping import reset_backtests
from ..primitives import (
    _STRATEGY_DESIGN_SCHEMA,
)
from ..prompts.strategy import _hypothesis_context_lines
from ..services import DevelopmentEngine, services_of
from ..state import HypothesisDevelopmentState


def advance_design(
    workflow: DevelopmentEngine,
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
) -> HypothesisDevelopmentState:
    document = state.hypothesis
    model_reference = state.research_model_reference
    fitted_reference = state.fitted_model_reference
    fitted_parameters = state.fitted_parameters
    runtime_configuration = state.research_model_runtime_configuration
    if (
        document is None
        or model_reference is None
        or fitted_reference is None
        or state.draft is None
        or fitted_parameters is None
        or runtime_configuration is None
    ):
        raise WorkflowError("strategy design requires a hypothesis")
    prompt_for = workflow._prompt_builder(
        workspace_scope=request.workspace_id,
        role=f"Thesis {services_of(workflow).coding_role} strategy author",
        context_id=f"{request.execution_id}-design",
    )
    revision_context: list[str] = []
    if state.rca:
        latest = state.rca[-1]
        revision_context = [
            "",
            "Strategy RCA handoff from the previous failed backtest:",
            f"Failure class: {latest.failure_class}",
            f"Diagnosis: {latest.rationale}",
            "Required revisions:",
            *[f"- {item}" for item in latest.follow_ups],
        ]
    draft, _ = workflow._compose_with_repair(
        request,
        services_of(workflow).coding_role,
        base_task="\n".join(
            [
                "Implement a strategy for the surviving hypothesis below. The "
                "workspace research model is a referenced dependency for the "
                "entry logic and Layer B monitor; do not copy or encode the "
                "indicator/model as a strategy. Load and use the frozen Layer A "
                "fit; never call fit again or estimate replacement parameters. "
                "Enter when its observations say "
                "the regularity is present, "
                "and declare exit criteria that re-check the declared "
                "falsifiers (by falsifierId) on a trailing window, so that a "
                "live breakdown of the historical regularity closes the "
                "position. Publish each current observation under the exact "
                "falsifier observable with context.publish_metric(name, value) "
                "so the run record and exit decision share evidence. Make "
                "the source define the declared Python strategy entrypoint. "
                "Sizing and risk limits must be explicit. PnL is a guardrail, never an "
                "exit criterion.",
                "",
                *_hypothesis_context_lines(
                    document, model_reference.key, fitted_reference.key
                ),
                *revision_context,
            ]
        ),
        prompt_for=prompt_for,
        compose=services_of(workflow).designer.compose,
        response_schema=_STRATEGY_DESIGN_SCHEMA,
        emit_output=False,
    )
    monitor = dict(runtime_configuration)
    strategy_parameters: Mapping[str, object] = {}
    configured_instances = draft.content.harness_configuration.get("strategy_instances")
    if (
        isinstance(configured_instances, Sequence)
        and not isinstance(configured_instances, str)
        and configured_instances
    ):
        first_instance = configured_instances[0]
        if isinstance(first_instance, Mapping) and isinstance(
            first_instance.get("parameters"), Mapping
        ):
            strategy_parameters = dict(first_instance["parameters"])
    # The harness reads symbols and the replay window from the strategy
    # configuration itself, so materialize both instead of relying on the
    # design agent to declare them (an absent window would otherwise fall
    # back to the harness's two-day synthetic default).
    strategy_configuration = dict(draft.content.harness_configuration)
    strategy_symbols = list(document.front_matter.universe)
    declared_data = strategy_configuration.get("data")
    strategy_data = dict(declared_data) if isinstance(declared_data, Mapping) else {}
    strategy_data["symbols"] = strategy_symbols
    window_start, window_end = study_window(strategy_configuration)
    draft = replace(
        draft,
        content=replace(
            draft.content,
            harness_configuration={
                **strategy_configuration,
                "data": strategy_data,
                "start": window_start,
                "end": window_end,
                "strategy_instances": [
                    {
                        "implementation": "harness.strategy.adapter:StrategyBundleAdapter",
                        "instance_id": draft.content.strategy_id,
                        "bundle_hash": "0" * 64,
                        "capital_fraction": "1.0",
                        "symbols": strategy_symbols,
                        "parameters": {
                            "strategy_source": dict(draft.content.source),
                            "strategy_entrypoint": draft.content.entrypoint,
                            "strategy_parameters": dict(strategy_parameters),
                            "model_monitor": monitor,
                            "exit_criteria": [
                                {
                                    **criterion.as_dict(),
                                    "threshold": str(criterion.threshold),
                                }
                                for criterion in draft.exit_criteria
                            ],
                        },
                    }
                ],
            },
            dependencies=tuple(
                dict.fromkeys(
                    (
                        *draft.content.dependencies,
                        model_reference.key,
                        fitted_reference.key,
                    )
                )
            ),
        ),
    )
    bundle = draft.content.bundle()
    validator = services_of(workflow).validator
    assert validator is not None
    validate_strategy_bundle(bundle, validator)
    append_attempt(
        workflow,
        request,
        state,
        AttemptKind.CANDIDATE,
        "sandbox-validated strategy designed",
        {
            "strategyId": bundle.strategy_id,
            "version": bundle.version,
            "bundleSha256": bundle.bundle_sha256,
            "exitCriteria": [criterion.as_dict() for criterion in draft.exit_criteria],
        },
    )
    return replace(
        reset_backtests(state),
        strategy_bundle=bundle,
        exit_criteria=draft.exit_criteria,
        strategy_cycles=state.strategy_cycles + 1,
    )
