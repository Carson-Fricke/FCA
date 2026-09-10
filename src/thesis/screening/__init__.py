"""Thesis workspace screening: point-in-time screener engine, preset and
programmatic filters, and workspace-artifact persistence for definitions and
saved studies.

The engine evaluates screener definitions over the as-of universe snapshot
with no lookahead: market-data windows end at the screening date and a bar
after ``as_of`` is invisible.  Filters read a per-symbol fact sheet that
merges market metrics, point-in-time classification and filed-date
fundamentals facts, and technical indicator values precomputed through the
harness indicator registry.  Custom screeners are user-authored Python
programs executed under a restricted compilation boundary (allowlisted
imports, one ``screen(ctx)`` function, restricted builtins, no network, no
wall clock, no file I/O) behind a documented executor seam.  Results are
deterministic and carry full provenance: universe revision, data window, and
definition hash.  :mod:`thesis.screening.context` binds the protocol to the
lake and harness point-in-time surfaces; :mod:`thesis.screening.service`
serves definitions and results to the HTTP layer.
"""

from thesis.screening.artifacts import (
    ArtifactError,
    SavedStudy,
    load_study,
    save_study,
)
from thesis.screening.context import (
    CikIndex,
    ClassificationPort,
    FundamentalsPort,
    LakeClassificationFacts,
    LakeFundamentalsFacts,
    LakeScreenerContext,
    StaticCikIndex,
)
from thesis.screening.engine import (
    MarketBar,
    ScreenerContext,
    ScreenerDefinition,
    ScreenerError,
    ScreenerHit,
    ScreenerPortError,
    UniverseEntry,
    run_screener,
)
from thesis.screening.indicators import (
    IndicatorBridgeError,
    OhlcvBar,
    OhlcvPort,
    compute_indicator_value,
)
from thesis.screening.presets import (
    FilterError,
    FilterEvaluator,
    IndicatorRequest,
    and_filters,
    indicator_metric_key,
    resolve_filter,
)
from thesis.screening.programs import (
    InProcessProgramExecutor,
    Program,
    ProgramExecutor,
    ProgramViolationError,
    ScreenContext,
    compile_program,
    run_program,
)
from thesis.screening.service import (
    ScreenerDataUnavailableError,
    ScreenerService,
    ScreenerServiceError,
    UnknownScreenerError,
)

__all__ = [
    "ArtifactError",
    "CikIndex",
    "ClassificationPort",
    "FilterError",
    "FilterEvaluator",
    "FundamentalsPort",
    "InProcessProgramExecutor",
    "IndicatorBridgeError",
    "IndicatorRequest",
    "LakeClassificationFacts",
    "LakeFundamentalsFacts",
    "LakeScreenerContext",
    "MarketBar",
    "OhlcvBar",
    "OhlcvPort",
    "Program",
    "ProgramExecutor",
    "ProgramViolationError",
    "ScreenerContext",
    "ScreenerDataUnavailableError",
    "ScreenerDefinition",
    "ScreenerError",
    "ScreenerHit",
    "ScreenerPortError",
    "ScreenerService",
    "ScreenerServiceError",
    "SavedStudy",
    "ScreenContext",
    "StaticCikIndex",
    "UnknownScreenerError",
    "UniverseEntry",
    "and_filters",
    "compile_program",
    "compute_indicator_value",
    "indicator_metric_key",
    "load_study",
    "resolve_filter",
    "run_program",
    "run_screener",
    "save_study",
]
