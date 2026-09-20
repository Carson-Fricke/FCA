"""Coverage for the assembled hypothesis-development workflow spec."""

from __future__ import annotations


def test_registered_hypothesis_spec_mirrors_the_existing_definition() -> None:
    from thesis.workflows.core.phase import (
        ConversationPhase,
        DeterministicPhase,
        OneShotPhase,
    )
    from thesis.workflows.registry import build_hypothesis_development_spec
    from thesis.workflows.registry.hypothesis_development.phases import (
        _TRANSITIONS,
        HYPOTHESIS_DEVELOPMENT_PHASES,
    )

    spec = build_hypothesis_development_spec()

    assert spec.workflow_id == "hypothesis-development"
    assert spec.version == "1.0.0"
    assert set(HYPOTHESIS_DEVELOPMENT_PHASES) <= set(spec.phase_names)
    assert dict(spec.transitions) == {
        phase.value: frozenset(target.value for target in targets)
        for phase, targets in _TRANSITIONS.items()
    }
    assert spec.terminal_phases == ("completed", "failed")
    # Agent phases declare their prompt seed; the trials phase declares a
    # deterministic contract instead, so it owns no conversation at all.
    deterministic = {
        name
        for name, declared in spec.phase_specs.items()
        if isinstance(declared, DeterministicPhase)
    }
    conversational = {
        name
        for name, declared in spec.phase_specs.items()
        if isinstance(declared, ConversationPhase)
    }
    one_shot = {
        name
        for name, declared in spec.phase_specs.items()
        if isinstance(declared, OneShotPhase)
    }
    assert deterministic == {"falsification_trials"}
    assert conversational == {"hypothesis_creation", "verdict"}
    assert one_shot == set(spec.phase_names) - deterministic - conversational


def test_registry_resolves_to_the_general_workflow_object() -> None:
    from collections.abc import Mapping

    from thesis.workflows.core.registry import WorkflowRegistry
    from thesis.workflows.core.workflow import Workflow
    from thesis.workflows.registry import (
        HYPOTHESIS_DEVELOPMENT_WORKFLOW_ID,
        hypothesis_development_registration,
    )

    def execute(
        context: object,
        /,
        *,
        ideas: str,
        investigation_id: str | None = None,
    ) -> Mapping[str, object]:
        del context, investigation_id
        return {"executionId": ideas}

    registry = WorkflowRegistry()
    registry.register(hypothesis_development_registration(execute))

    resolved = registry.resolve(HYPOTHESIS_DEVELOPMENT_WORKFLOW_ID)
    workflow = resolved.workflow

    assert isinstance(workflow, Workflow)
    assert type(workflow) is Workflow
    assert workflow.spec.workflow_id == HYPOTHESIS_DEVELOPMENT_WORKFLOW_ID
    assert resolved.spec is workflow.spec


def test_hypothesis_development_package_defines_no_workflow_class() -> None:
    import importlib
    import pkgutil

    import pytest

    import thesis.workflows.registry.hypothesis_development as package

    defined: list[str] = []
    for module_info in pkgutil.walk_packages(
        package.__path__, prefix=f"{package.__name__}."
    ):
        module = importlib.import_module(module_info.name)
        for name, value in vars(module).items():
            if (
                isinstance(value, type)
                and "Workflow" in name
                and not issubclass(value, Exception)
                and getattr(value, "__module__", "").startswith(
                    "thesis.workflows.registry"
                )
            ):
                defined.append(f"{module_info.name}.{name}")
    assert defined == []
    assert not hasattr(package, "HypothesisDevelopmentWorkflow")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(
            "thesis.workflows.registry.hypothesis_development.workflow"
        )
