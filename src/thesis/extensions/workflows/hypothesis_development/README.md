# thesis/extensions/workflows/hypothesis_development

The concrete FCA research workflow. Its endpoint owns native domain state, services and phase behavior; DevelopmentActivities supplies scoped effects without exposing the host engine.

Runtime contract 5.0.0 uses the generic fixed graph projection with a singleton
frontier. The phase flow, prompts and tool policies remain unchanged.

Subdirectories: `composers/`, `contracts/`, `phases/`, `prompts/`. Each documents its local boundary.

- `__init__.py`: Falsification-first hypothesis-development workflow package.
- `activities.py`: Provides execution-scoped artifact, job, event and result capabilities to FCA phase implementations without exposing the workflow engine.
- `artifacts.py`: Durable artifact projections for the workflow state.
- `callbacks.py`: Declares FCA-local executable phase callbacks and projects provider-independent host phase metadata.
- `design.py`: The frozen research design: what the development evidence committed the run to.
- `doctrine.py`: The hypothesis-development doctrine: the data the general engine reads.
- `endpoint.py`: Owns the hypothesis workflow JSON endpoint, FCA retrieval policy and native request/state, validation, phase execution, seeding and reports.
- `errors.py`: Workflow error contracts.
- `exposure.py`: Durable experiment-ledger recording for the hypothesis-development workflow.
- `jobs.py`: Workflow-owned tool jobs.
- `launch_schema.py`: Hypothesis-development launch input schema.
- `piping.py`: Typed phase-to-phase piping for the hypothesis-development state.
- `primitives.py`: Shared constants, validation, and small helpers.
- `provenance.py`: Provenance checks that bind every reported job to the run that executed it.
- `rebuild.py`: The execution-store rebuild fold (REQ-B4).
- `services.py`: Composed domain services for the hypothesis-development workflow.
- `session_services.py`: Hypothesis workflow policy, discovery-owned `set_research_window` declaration, scoped program access and completion services for conversation tools.
- `spec.py`: Assemble the hypothesis-development workflow specification and object.
- `state.py`: Workflow state and outcome records.

Discovery develops executable training prototypes with parameter/interaction search
and suitable robustness evidence; formal falsification and forward validation remain
later phases. Sketch.prototype retains the exact tested policy, parameters and search
references. The single research critic checks strategy/decision fidelity and correctness.
