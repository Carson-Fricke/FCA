"""Focused tests for the AST-based strategy sandbox lint."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

from harness.strategy import ReferenceStrategy
from harness.strategy.lint import (
    ASYNC_SYNTAX_RULE,
    RULES,
    LintReport,
    SandboxViolationError,
    StrategyLintError,
    StrategyTarget,
    ViolationCategory,
    assert_sandbox_clean,
    iter_rules,
    iter_strategy_files,
    lint_strategy,
    lint_strategy_class,
    lint_strategy_directory,
    lint_strategy_file,
    lint_strategy_module,
    lint_strategy_source,
)

FIXTURES = Path(__file__).parent / "fixtures"
CLEAN_FIXTURE = FIXTURES / "clean_strategy.py"

CLEAN_SOURCE = """
from decimal import Decimal
from random import Random

from harness.core.types import QuoteEvent
from harness.strategy.context import StrategyContext
from harness.strategy.contracts import Strategy


class InlineCleanStrategy(Strategy):
    def __init__(self, threshold: Decimal = Decimal("0.01")) -> None:
        self.threshold = threshold
        self._jitter = Random(1729)
        self.seen: list[str] = []

    def on_start(self, context: StrategyContext) -> None:
        context.state["threshold"] = self.threshold
        context.log.info("ready")
        context.emit("ready", ok=True)

    def on_quote(self, event: QuoteEvent) -> None:
        jitter = Decimal(self._jitter.random())
        spread = event.ask_price - event.bid_price
        if spread + jitter >= self.threshold:
            self.seen.append(str(event.symbol))
"""

CONTEXT_BOUNDARY_SOURCE = """
from harness.strategy.context import StrategyContext
from harness.strategy.contracts import Strategy


class ContextBoundaryStrategy(Strategy):
    def on_start(self, context: StrategyContext) -> None:
        now = context.clock.now()
        context.state["started_at"] = now
        if context.data is not None:
            context.data.read(now, "AAPL")
        context.random.random()
        context.random.randint(1, 10)
        context.log.info("started", at=str(now))
        context.emit("started", at=str(now))

    def on_quote(self, event: QuoteEvent) -> None:
        self.seen.append(str(event.symbol))
"""

SEEDED_RANDOM_SOURCE = """
from random import Random

from harness.strategy.contracts import Strategy


class SeededRandomStrategy(Strategy):
    def __init__(self, seed: int = 1729) -> None:
        self._generator = Random(seed)

    def on_start(self, context) -> None:
        self._draw = self._generator.random()
"""

WALL_CLOCK_SOURCE = """
import time
from datetime import datetime

from harness.strategy.contracts import Strategy


class WallClockSource(Strategy):
    def on_start(self, context) -> None:
        self._stamp = time.time()
        self._now = datetime.now()
"""

SLEEPING_SOURCE = """
import time

from harness.strategy.contracts import Strategy


class SleepingSource(Strategy):
    def on_start(self, context) -> None:
        time.sleep(0.1)
"""

FROM_IMPORT_SLEEP_SOURCE = """
from time import sleep

from harness.strategy.contracts import Strategy


class FromImportSleepSource(Strategy):
    def on_start(self, context) -> None:
        sleep(0.1)
"""

NETWORK_SOURCE = """
import socket

from harness.strategy.contracts import Strategy


class NetworkSource(Strategy):
    def on_start(self, context) -> None:
        self._connection = socket.socket()
"""

NETWORK_FROM_IMPORT_SOURCE = """
from httpx import get

from harness.strategy.contracts import Strategy


class NetworkFromImportSource(Strategy):
    def on_start(self, context) -> None:
        self._response = get("https://example.invalid")
"""

SUBPROCESS_SOURCE = """
import subprocess

from harness.strategy.contracts import Strategy


class SubprocessSource(Strategy):
    def on_start(self, context) -> None:
        subprocess.run(["echo", "started"], check=True)
"""

SUBPROCESS_OS_SOURCE = """
import os

from harness.strategy.contracts import Strategy


class OsSubprocessSource(Strategy):
    def on_start(self, context) -> None:
        os.system("echo started")
"""

UNSEEDED_RANDOM_SOURCE = """
import os
import random
import secrets
import uuid

from harness.strategy.contracts import Strategy


class UnseededRandomSource(Strategy):
    def on_start(self, context) -> None:
        self._draw = random.random()
        self._size = random.randint(1, 10)
        self._system = random.SystemRandom()
        self._token = secrets.token_hex(8)
        self._salt = os.urandom(4)
        self._tag = uuid.uuid4()
"""

ASYNC_SOURCE = """
import asyncio

from harness.strategy.contracts import Strategy


class AsyncSource(Strategy):
    async def on_start(self, context) -> None:
        await asyncio.sleep(0.1)
"""

ASYNC_COMPREHENSION_SOURCE = """
from harness.strategy.contracts import Strategy


class AsyncComprehensionSource(Strategy):
    def on_start(self, context) -> None:
        self._values = [value async for value in context.stream()]
"""

BARE_ATTRIBUTE_SOURCE = """
import datetime

now = datetime.datetime.now
"""

CONCURRENCY_SOURCE = """
import threading
from concurrent.futures import ThreadPoolExecutor

from harness.strategy.contracts import Strategy


class ConcurrencySource(Strategy):
    def on_start(self, context) -> None:
        self._worker = threading.Thread(target=self._poll)
        self._pool = ThreadPoolExecutor(max_workers=1)

    def _poll(self) -> None:
        self._worker = None
"""

FIXTURE_CATEGORIES: dict[str, ViolationCategory] = {
    "wall_clock_strategy.py": ViolationCategory.WALL_CLOCK,
    "sleeping_strategy.py": ViolationCategory.SLEEPING,
    "networking_strategy.py": ViolationCategory.NETWORKING,
    "subprocess_strategy.py": ViolationCategory.SUBPROCESS,
    "unseeded_random_strategy.py": ViolationCategory.UNSEEDED_RANDOM,
    "asyncio_strategy.py": ViolationCategory.ASYNCIO,
    "concurrency_strategy.py": ViolationCategory.CONCURRENCY,
}


def _load_fixture_module(name: str) -> ModuleType:
    """Load one failing fixture so class and module targets can be exercised."""
    path = FIXTURES / name
    spec = importlib.util.spec_from_file_location(
        f"strategy_lint_fixture_{path.stem}", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_fixture_class(name: str, attribute: str) -> type:
    """Return one strategy class from a failing fixture module."""
    strategy_class = getattr(_load_fixture_module(name), attribute)
    assert isinstance(strategy_class, type)
    return strategy_class


def test_clean_fixture_file_passes() -> None:
    report = lint_strategy_file(CLEAN_FIXTURE)

    assert report.ok
    assert report.violations == ()
    assert report.render() == f"{CLEAN_FIXTURE}: strategy sandbox clean"


def test_clean_inline_source_passes() -> None:
    report = lint_strategy_source(CLEAN_SOURCE, filename="<clean>")

    assert report.ok
    assert report.filename == "<clean>"


def test_reference_strategy_class_passes() -> None:
    report = lint_strategy_class(ReferenceStrategy)

    assert report.ok, report.render()


def test_context_boundaries_are_allowed() -> None:
    report = lint_strategy_source(CONTEXT_BOUNDARY_SOURCE)

    assert report.ok, report.render()


def test_seeded_random_is_allowed() -> None:
    report = lint_strategy_source(SEEDED_RANDOM_SOURCE)

    assert report.ok, report.render()


def test_wall_clock_reads_are_rejected() -> None:
    report = lint_strategy_source(WALL_CLOCK_SOURCE)

    assert not report.ok
    assert report.categories == {ViolationCategory.WALL_CLOCK}
    assert len(report.violations) == 2


def test_sleeping_is_rejected() -> None:
    report = lint_strategy_source(SLEEPING_SOURCE)

    assert report.categories == {ViolationCategory.SLEEPING}
    assert report.codes == ("SB100",)


def test_prohibited_from_imports_are_rejected() -> None:
    report = lint_strategy_source(FROM_IMPORT_SLEEP_SOURCE)

    assert report.categories == {ViolationCategory.SLEEPING}
    assert report.codes == ("SB100",)
    assert report.violations[0].symbol == "time.sleep"


def test_networking_module_imports_are_rejected() -> None:
    report = lint_strategy_source(NETWORK_SOURCE)

    assert report.categories == {ViolationCategory.NETWORKING}
    assert report.violations[0].symbol == "socket"


def test_networking_from_imports_are_rejected() -> None:
    report = lint_strategy_source(NETWORK_FROM_IMPORT_SOURCE)

    assert report.categories == {ViolationCategory.NETWORKING}
    assert report.violations[0].symbol == "httpx.get"


def test_subprocess_modules_are_rejected() -> None:
    report = lint_strategy_source(SUBPROCESS_SOURCE)

    assert report.categories == {ViolationCategory.SUBPROCESS}
    assert report.violations[0].symbol == "subprocess"


def test_os_process_helpers_are_rejected() -> None:
    report = lint_strategy_source(SUBPROCESS_OS_SOURCE)

    assert report.categories == {ViolationCategory.SUBPROCESS}
    assert report.violations[0].symbol == "os.system"


def test_unseeded_random_is_rejected() -> None:
    report = lint_strategy_source(UNSEEDED_RANDOM_SOURCE)

    assert report.categories == {ViolationCategory.UNSEEDED_RANDOM}
    assert len(report.violations) == 6


def test_async_syntax_is_rejected() -> None:
    report = lint_strategy_source(ASYNC_SOURCE)

    assert report.categories == {ViolationCategory.ASYNCIO}
    assert ASYNC_SYNTAX_RULE.code in report.codes
    assert len(report.matching(code=ASYNC_SYNTAX_RULE.code)) == 2


def test_async_comprehensions_are_rejected() -> None:
    report = lint_strategy_source(ASYNC_COMPREHENSION_SOURCE)

    assert report.categories == {ViolationCategory.ASYNCIO}
    assert [violation.symbol for violation in report.violations] == ["async for"]
    assert report.violations[0].lineno > 0


def test_bare_wall_clock_attribute_is_rejected() -> None:
    report = lint_strategy_source(BARE_ATTRIBUTE_SOURCE)

    assert report.codes == ("SB002",)
    assert report.violations[0].symbol == "datetime.datetime.now"


def test_concurrency_is_rejected() -> None:
    report = lint_strategy_source(CONCURRENCY_SOURCE)

    assert report.categories == {ViolationCategory.CONCURRENCY}
    assert len(report.violations) == 2


@pytest.mark.parametrize("name", sorted(FIXTURE_CATEGORIES))
def test_failing_fixture_reports_its_category(name: str) -> None:
    expected = FIXTURE_CATEGORIES[name]
    report = lint_strategy_file(FIXTURES / name)

    assert not report.ok
    assert expected in report.categories
    assert report.violations


def test_every_prohibited_category_has_a_failing_fixture() -> None:
    covered: set[ViolationCategory] = set()
    for name in FIXTURE_CATEGORIES:
        covered.update(lint_strategy_file(FIXTURES / name).categories)

    assert covered == set(ViolationCategory)


def test_every_category_has_at_least_one_rule() -> None:
    for category in ViolationCategory:
        assert tuple(iter_rules(category))


def test_rule_codes_are_unique() -> None:
    codes = [rule.code for rule in RULES] + [ASYNC_SYNTAX_RULE.code]

    assert len(codes) == len(set(codes))


def test_every_violation_is_actionable() -> None:
    report = lint_strategy_source(UNSEEDED_RANDOM_SOURCE + ASYNC_SOURCE, filename="<x>")

    assert report.violations
    for violation in report.violations:
        assert violation.code.startswith("SB")
        assert violation.category in ViolationCategory
        assert violation.filename == "<x>"
        assert violation.lineno > 0
        assert violation.col_offset >= 0
        assert violation.message.startswith(violation.symbol)
        assert "context." in violation.remediation
        assert violation.location == f"<x>:{violation.lineno}:{violation.col_offset}"
        assert violation.code in violation.format()


def test_violation_line_numbers_point_at_the_offending_source() -> None:
    report = lint_strategy_source(SLEEPING_SOURCE)
    expected = next(
        index
        for index, line in enumerate(SLEEPING_SOURCE.splitlines(), start=1)
        if "time.sleep" in line
    )

    assert [violation.lineno for violation in report.violations] == [expected]


def test_mixed_violations_are_all_reported() -> None:
    report = lint_strategy_file(FIXTURES / "mixed_violations_strategy.py")

    assert report.categories == {
        ViolationCategory.NETWORKING,
        ViolationCategory.SLEEPING,
        ViolationCategory.WALL_CLOCK,
    }
    assert len(report.violations) == 3


def test_dispatcher_accepts_text_path_class_and_module() -> None:
    from_text = lint_strategy(SLEEPING_SOURCE)
    from_path = lint_strategy(FIXTURES / "sleeping_strategy.py")
    from_path_string = lint_strategy(str(FIXTURES / "sleeping_strategy.py"))
    module = _load_fixture_module("sleeping_strategy.py")
    from_module = lint_strategy(module)
    from_class = lint_strategy(
        _load_fixture_class("sleeping_strategy.py", "SleepingStrategy")
    )

    assert from_text.categories == {ViolationCategory.SLEEPING}
    assert from_path.categories == {ViolationCategory.SLEEPING}
    assert from_path_string.categories == {ViolationCategory.SLEEPING}
    assert from_module.categories == {ViolationCategory.SLEEPING}
    assert from_class.categories == {ViolationCategory.SLEEPING}


def test_class_target_reports_positions_in_the_original_file() -> None:
    strategy_class = _load_fixture_class("wall_clock_strategy.py", "WallClockStrategy")
    report = lint_strategy_class(strategy_class)
    source_lines = (
        (FIXTURES / "wall_clock_strategy.py").read_text(encoding="utf-8").splitlines()
    )
    expected = [
        index
        for index, line in enumerate(source_lines, start=1)
        if "datetime.now()" in line or "time.time()" in line
    ]

    assert report.filename.endswith("wall_clock_strategy.py")
    assert [violation.lineno for violation in report.violations] == sorted(expected)


def test_module_target_lints_the_test_module_itself() -> None:
    report = lint_strategy_module(sys.modules[__name__])

    assert report.ok, report.render()


def test_render_lists_every_violation() -> None:
    report = lint_strategy_source(SLEEPING_SOURCE, filename="<sleep>")
    rendered = report.render()

    assert rendered.startswith("<sleep>: 1 sandbox violation(s)")
    assert report.violations[0].format() in rendered


def test_matching_filters_by_category_and_code() -> None:
    report = lint_strategy_file(FIXTURES / "mixed_violations_strategy.py")

    assert len(report.matching(ViolationCategory.SLEEPING)) == 1
    assert report.matching(ViolationCategory.NETWORKING, "SB100") == ()
    assert report.matching(code="SB100")[0].category is ViolationCategory.SLEEPING


def test_directory_lint_aggregates_every_offending_file() -> None:
    report = lint_strategy_directory(FIXTURES)

    assert not report.ok
    assert report.categories == set(ViolationCategory)
    files = {violation.filename for violation in report.violations}
    assert any(name in path for path in files for name in FIXTURE_CATEGORIES)


def test_iter_strategy_files_is_sorted_and_skips_caches() -> None:
    files = iter_strategy_files(FIXTURES)

    assert files == tuple(sorted(files))
    assert all(path.suffix == ".py" for path in files)
    assert CLEAN_FIXTURE in files


def test_linting_is_deterministic_and_ordered() -> None:
    first = lint_strategy_file(FIXTURES / "mixed_violations_strategy.py")
    second = lint_strategy_file(FIXTURES / "mixed_violations_strategy.py")

    assert first == second
    assert [violation.lineno for violation in first.violations] == sorted(
        violation.lineno for violation in first.violations
    )


def test_unparsable_source_raises_strategy_lint_error() -> None:
    with pytest.raises(StrategyLintError, match="not valid Python"):
        lint_strategy_source("def broken(")


def test_unsupported_target_raises_type_error() -> None:
    with pytest.raises(TypeError, match="source text, a file path"):
        lint_strategy(cast(StrategyTarget, 17))


def test_missing_directory_raises_strategy_lint_error() -> None:
    with pytest.raises(StrategyLintError, match="is not a directory"):
        lint_strategy_directory(FIXTURES / "missing")


def test_assert_sandbox_clean_accepts_clean_targets() -> None:
    report = assert_sandbox_clean(CLEAN_FIXTURE)

    assert isinstance(report, LintReport)
    assert report.ok


def test_assert_sandbox_clean_raises_with_the_rendered_report() -> None:
    with pytest.raises(SandboxViolationError) as caught:
        assert_sandbox_clean(FIXTURES / "sleeping_strategy.py")

    error = caught.value
    assert error.report.categories == {ViolationCategory.SLEEPING}
    assert str(error) == error.report.render()
    assert "SB100" in str(error)


def test_report_raise_for_status_raises_once() -> None:
    report = lint_strategy_file(CLEAN_FIXTURE)

    report.raise_for_status()

    with pytest.raises(SandboxViolationError):
        lint_strategy_file(FIXTURES / "sleeping_strategy.py").raise_for_status()
