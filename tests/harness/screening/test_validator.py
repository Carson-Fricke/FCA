"""Static boundary validation: malicious and malformed scripts fail closed."""

from __future__ import annotations

import pytest

from harness.screening import (
    FilterErrorKind,
    FilterEvaluationError,
    assert_filter_source,
    lint_filter_source,
)

VIOLATION_CASES: tuple[tuple[str, FilterErrorKind], ...] = (
    ("import os", FilterErrorKind.IMPORT_BLOCKED),
    ("import subprocess", FilterErrorKind.IMPORT_BLOCKED),
    ("import socket", FilterErrorKind.IMPORT_BLOCKED),
    ("import urllib.request", FilterErrorKind.IMPORT_BLOCKED),
    ("import ctypes", FilterErrorKind.IMPORT_BLOCKED),
    ("import math as m", FilterErrorKind.IMPORT_BLOCKED),
    ("from os import system", FilterErrorKind.IMPORT_BLOCKED),
    ("from math import *", FilterErrorKind.IMPORT_BLOCKED),
    ("from math import sqrt as square_root", FilterErrorKind.IMPORT_BLOCKED),
    ("from . import sibling", FilterErrorKind.IMPORT_BLOCKED),
    ("x = 1", FilterErrorKind.STRUCTURE_VIOLATION),
    ("", FilterErrorKind.STRUCTURE_VIOLATION),
    (
        "def other(ctx):\n    return True",
        FilterErrorKind.STRUCTURE_VIOLATION,
    ),
    (
        "def screen(ctx):\n    return True\n\n\ndef extra(ctx):\n    return False",
        FilterErrorKind.STRUCTURE_VIOLATION,
    ),
    (
        "class Wrapper:\n\n    pass\n\n\ndef screen(ctx):\n    return True",
        FilterErrorKind.STRUCTURE_VIOLATION,
    ),
    (
        "def screen(ctx):\n    class Inner:\n        pass\n    return True",
        FilterErrorKind.STRUCTURE_VIOLATION,
    ),
    (
        "def screen(ctx):\n    return ctx.__class__",
        FilterErrorKind.DUNDER_ACCESS,
    ),
    (
        "def screen(ctx):\n    return bars.__dict__",
        FilterErrorKind.DUNDER_ACCESS,
    ),
    (
        "def screen(ctx):\n    return __import__('os')",
        FilterErrorKind.DUNDER_ACCESS,
    ),
    (
        "def screen(ctx):\n    return __builtins__",
        FilterErrorKind.DUNDER_ACCESS,
    ),
    (
        "def screen(ctx):\n    return list.append.__globals__",
        FilterErrorKind.DUNDER_ACCESS,
    ),
    (
        "def screen(ctx):\n    return open('/etc/passwd')",
        FilterErrorKind.DANGEROUS_CALL,
    ),
    ("def screen(ctx):\n    return eval('1+1')", FilterErrorKind.DANGEROUS_CALL),
    ("def screen(ctx):\n    return exec('x=1')", FilterErrorKind.DANGEROUS_CALL),
    (
        "def screen(ctx):\n    return compile('1', '<s>', 'eval')",
        FilterErrorKind.DANGEROUS_CALL,
    ),
    (
        "def screen(ctx):\n    return getattr(ctx, 'facts')",
        FilterErrorKind.DANGEROUS_CALL,
    ),
    ("def screen(ctx):\n    return vars(ctx)", FilterErrorKind.DANGEROUS_CALL),
    ("def screen(ctx):\n    return globals()", FilterErrorKind.DANGEROUS_CALL),
    ("def screen(ctx):\n    return input()", FilterErrorKind.DANGEROUS_CALL),
    (
        "def screen(ctx):\n    return time.time() > 0",
        FilterErrorKind.WALL_CLOCK,
    ),
    (
        "def screen(ctx):\n    return datetime.now()",
        FilterErrorKind.WALL_CLOCK,
    ),
    (
        "def screen(ctx):\n    time.sleep(10)\n    return True",
        FilterErrorKind.SLEEPING,
    ),
    (
        "def screen(ctx):\n    return socket.socket()",
        FilterErrorKind.NETWORKING,
    ),
    (
        "def screen(ctx):\n    return urllib.request.urlopen('http://x')",
        FilterErrorKind.NETWORKING,
    ),
    (
        "def screen(ctx):\n    import httpx\n    return True",
        FilterErrorKind.IMPORT_BLOCKED,
    ),
    (
        "def screen(ctx):\n    return os.system('dir')",
        FilterErrorKind.SUBPROCESS,
    ),
    (
        "def screen(ctx):\n    return subprocess.run(['dir'])",
        FilterErrorKind.SUBPROCESS,
    ),
    (
        "def screen(ctx):\n    return threading.Thread(target=None)",
        FilterErrorKind.CONCURRENCY,
    ),
    (
        "def screen(ctx):\n    return asyncio.run(main())",
        FilterErrorKind.CONCURRENCY,
    ),
    (
        "async def screen(ctx):\n    return True",
        FilterErrorKind.ASYNC_SYNTAX,
    ),
    (
        "def screen(ctx):\n    return [x async for x in []]",
        FilterErrorKind.ASYNC_SYNTAX,
    ),
    ("def screen(ctx", FilterErrorKind.SYNTAX_ERROR),
)


@pytest.mark.parametrize(("source", "expected"), VIOLATION_CASES)
def test_violation_kinds(source: str, expected: FilterErrorKind) -> None:
    report = lint_filter_source(source)
    assert not report.ok
    assert expected in report.kinds
    assert report.render()


@pytest.mark.parametrize(("source", "expected"), VIOLATION_CASES)
def test_evaluation_fails_closed_before_execution(
    source: str, expected: FilterErrorKind, make_context
) -> None:
    from harness.screening import evaluate_filter

    with pytest.raises(FilterEvaluationError) as raised:
        evaluate_filter(source, make_context())
    record = raised.value.error
    assert record.kind is expected
    assert record.script_hash


def test_clean_sources_pass_lint() -> None:
    clean_sources = (
        """
def screen(ctx):
    return True
""",
        """
import math
import statistics


def screen(ctx):
    closes = [bar["close"] for bar in ctx["bars"]]
    if len(closes) < 2:
        return False
    return math.isfinite(statistics.pstdev(closes))
""",
        """
def screen(ctx):
    average = sum(bar["close"] for bar in ctx["bars"]) / max(len(ctx["bars"]), 1)
    return ctx["facts"]["security_type"] == "common_stock" and average > 0
""",
    )
    for source in clean_sources:
        report = lint_filter_source(source)
        assert report.ok, report.render()
        assert_filter_source(source)


def test_non_string_source_is_a_violation() -> None:
    report = lint_filter_source(12345)
    assert not report.ok
    assert report.source_hash == ""


def test_dunder_violation_reports_line_number() -> None:
    report = lint_filter_source("def screen(ctx):\n    return ctx.__class__")
    dunder = [v for v in report.violations if v.code == "HS200"]
    assert dunder
    assert dunder[0].lineno == 2


def test_nested_imports_are_rejected_statically() -> None:
    report = lint_filter_source(
        "def screen(ctx):\n    import subprocess\n    return True"
    )
    assert not report.ok
    assert FilterErrorKind.IMPORT_BLOCKED in report.kinds
    assert "HS100" in report.codes


def test_violation_message_carries_remediation() -> None:
    report = lint_filter_source("import os\n\ndef screen(ctx):\n    return True")
    violation = report.violations[0]
    assert "fix:" in violation.format()
    assert violation.remediation


def test_first_violation_is_deterministic() -> None:
    source = "import os\nimport socket\n\ndef screen(ctx):\n    return True"
    first = lint_filter_source(source).violations[0]
    for _ in range(5):
        assert lint_filter_source(source).violations[0] == first
