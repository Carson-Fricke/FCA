"""CI lint-sweep coverage for the strategy sandbox gate.

The CI workflow runs the sandbox lint (``harness.strategy.lint``) over the
shipped strategy sources as a quality gate alongside Ruff and ty, so the
lint is defense-in-depth rather than load-bearing in the run path.  These
tests keep that gate honest from both directions: the swept directories must
stay clean, and the gate must demonstrably fail on the intentionally dirty
negative-control fixtures.
"""

from __future__ import annotations

from pathlib import Path

from harness.strategy.lint import (
    ViolationCategory,
    iter_strategy_files,
    lint_strategy_directory,
    lint_strategy_file,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# Directories the CI sweep covers: every shipped strategy source.  The
# intentionally violating fixtures under tests/strategy_lint/fixtures are
# negative controls for the gate itself and are excluded by design.
SWEEP_ROOTS = (REPO_ROOT / "strategies",)

FIXTURES = Path(__file__).parent / "fixtures"
CLEAN_FIXTURE = FIXTURES / "clean_strategy.py"


def test_swept_strategy_directories_are_sandbox_clean() -> None:
    for root in SWEEP_ROOTS:
        report = lint_strategy_directory(root)

        assert report.ok, report.render()


def test_sweep_covers_the_shipped_reference_strategies() -> None:
    files = iter_strategy_files(REPO_ROOT / "strategies")

    assert len(files) >= 4
    assert all(path.suffix == ".py" for path in files)
    assert {path.name for path in files} >= {
        "buy_and_hold.py",
        "intraday_momentum.py",
        "mean_reversion.py",
    }


def test_clean_fixture_the_gate_relies_on_stays_clean() -> None:
    report = lint_strategy_file(CLEAN_FIXTURE)

    assert report.ok, report.render()


def test_sweep_gate_fails_on_the_dirty_fixture_controls() -> None:
    report = lint_strategy_directory(FIXTURES)

    assert not report.ok
    assert report.categories == set(ViolationCategory)
    assert "sandbox violation" in report.render()
