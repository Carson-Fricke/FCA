"""Thesis workspace screening: point-in-time screener engine, preset and
programmatic filters, and workspace-artifact persistence for definitions and
saved studies.

The engine evaluates screener definitions over the as-of universe snapshot
with no lookahead: market-data windows end at the screening date and a bar
after ``as_of`` is invisible. Custom screeners are user-authored Python
programs executed under a restricted compilation boundary (allowlisted
imports, one ``screen(ctx)`` function, restricted builtins, no network,
no wall clock, no file I/O). Results are deterministic and carry full
provenance: universe revision, data window, and definition hash.
"""

from thesis.screening.artifacts import (
    ArtifactError,
    SavedStudy,
    load_study,
    save_study,
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
from thesis.screening.presets import (
    FilterError,
    FilterEvaluator,
    and_filters,
    resolve_filter,
)
from thesis.screening.programs import (
    Program,
    ProgramViolationError,
    ScreenContext,
    compile_program,
    run_program,
)

__all__ = [
    "ArtifactError",
    "FilterError",
    "FilterEvaluator",
    "MarketBar",
    "Program",
    "ProgramViolationError",
    "ScreenerContext",
    "ScreenerDefinition",
    "ScreenerError",
    "ScreenerHit",
    "ScreenerPortError",
    "SavedStudy",
    "ScreenContext",
    "UniverseEntry",
    "and_filters",
    "compile_program",
    "load_study",
    "resolve_filter",
    "run_program",
    "run_screener",
    "save_study",
]
