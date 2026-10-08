"""Orchestrated Thesis workflows: the falsification-first hypothesis pipeline.

The retired sibling workflows (strategy development, autonomous discovery, and
the standalone RCA revision loop) were removed. The falsification-first
hypothesis-development pipeline is the canonical research workflow, and it owns
strategy design, backtesting, and RCA as internal phases.
"""

from thesis.workflows.backtests import (
    BacktestMode,
    BacktestRunError,
    BacktestRunner,
    BacktestRunRequest,
    BacktestRunResult,
    HarnessBacktestRunner,
    HarnessError,
)
from thesis.workflows.evidence import (
    EVIDENCE_GRADE,
    EXPLORATORY_DISCLOSURE,
    HISTORICAL_PHASE_LABEL,
    EvidenceReport,
    EvidenceReportError,
    build_evidence_report,
)
from thesis.workflows.local import (
    McpBacktestRunner,
    McpStudyRunner,
    build_local_workflow_runtime,
)
from thesis.workflows.registry.hypothesis_development import (
    HYPOTHESIS_DEVELOPMENT_PHASES,
    HYPOTHESIS_DEVELOPMENT_WORKFLOW,
    VERDICT_INCONCLUSIVE,
    VERDICT_REFUTED,
    VERDICT_SUPPORTED,
    FalsificationPlan,
    FalsificationTrial,
    FalsificationVerdict,
    FalsifierTest,
    HypothesisDevelopmentOutcome,
    HypothesisDevelopmentPhase,
    HypothesisDevelopmentRequest,
    HypothesisDevelopmentState,
    HypothesisDraft,
    PlaceboControl,
    RcaDiagnosis,
    StrategyBacktest,
)
from thesis.workflows.research_models import (
    BacktestResearchModelRunner,
    ResearchModelRunner,
    ResearchModelRunRequest,
    ResearchModelRunResult,
)
from thesis.workflows.runtime import (
    WorkflowRuntime,
    WorkflowRuntimeError,
    build_model_router,
    build_workflow_runtime,
)

__all__ = [
    "EVIDENCE_GRADE",
    "EXPLORATORY_DISCLOSURE",
    "StrategyBacktest",
    "FalsificationPlan",
    "FalsificationTrial",
    "FalsificationVerdict",
    "FalsifierTest",
    "HISTORICAL_PHASE_LABEL",
    "HYPOTHESIS_DEVELOPMENT_PHASES",
    "HYPOTHESIS_DEVELOPMENT_WORKFLOW",
    "HypothesisDevelopmentOutcome",
    "HypothesisDevelopmentPhase",
    "HypothesisDevelopmentRequest",
    "HypothesisDevelopmentState",
    "HypothesisDraft",
    "PlaceboControl",
    "RcaDiagnosis",
    "BacktestMode",
    "BacktestResearchModelRunner",
    "BacktestRunError",
    "BacktestRunRequest",
    "BacktestRunResult",
    "BacktestRunner",
    "EvidenceReport",
    "EvidenceReportError",
    "HarnessBacktestRunner",
    "HarnessError",
    "McpBacktestRunner",
    "McpStudyRunner",
    "ResearchModelRunner",
    "ResearchModelRunRequest",
    "ResearchModelRunResult",
    "VERDICT_INCONCLUSIVE",
    "VERDICT_REFUTED",
    "VERDICT_SUPPORTED",
    "WorkflowRuntime",
    "WorkflowRuntimeError",
    "build_evidence_report",
    "build_local_workflow_runtime",
    "build_model_router",
    "build_workflow_runtime",
]
