"""Sandboxed evaluation fail-closed behavior: output shape, errors, timeout."""

from __future__ import annotations

import time
from datetime import date
from decimal import Decimal
from typing import Any, cast

import pytest

from harness.screening import (
    DEFAULT_TIMEOUT_SECONDS,
    BarRecord,
    FilterBatchResult,
    FilterContext,
    FilterErrorKind,
    FilterEvaluationError,
    FilterResult,
    evaluate_filter,
    evaluate_universe,
)
from harness.screening._worker import script_hash

SIMPLE_TRUE = "\ndef screen(ctx):\n    return True\n"
SIMPLE_FALSE = "\ndef screen(ctx):\n    return False\n"
DICT_TRUE = '\ndef screen(ctx):\n    return {ctx["facts"]["symbol"]: True}\n'


def test_single_symbol_result(make_context) -> None:
    result = evaluate_filter(SIMPLE_TRUE, make_context(symbol="AAPL"))
    assert isinstance(result, FilterResult)
    assert result == FilterResult("AAPL", True, script_hash(SIMPLE_TRUE))


def test_batch_result_covers_every_symbol_in_order(make_context) -> None:
    contexts = (
        make_context(symbol="AAPL"),
        make_context(symbol="MSFT"),
        make_context(symbol="ETFA"),
    )
    batch = evaluate_universe(SIMPLE_FALSE, contexts)
    assert isinstance(batch, FilterBatchResult)
    assert batch.decisions == {"AAPL": False, "MSFT": False, "ETFA": False}
    assert list(batch.decisions) == ["AAPL", "MSFT", "ETFA"]
    assert batch.passed_symbols == ()
    assert batch.rejected_symbols == ("AAPL", "MSFT", "ETFA")
    assert batch.script_hash == script_hash(SIMPLE_FALSE)


def test_dict_return_shape_is_accepted(make_context) -> None:
    batch = evaluate_universe(
        DICT_TRUE, (make_context(symbol="AAPL"), make_context(symbol="MSFT"))
    )
    assert batch.decisions == {"AAPL": True, "MSFT": True}


@pytest.mark.parametrize(
    ("script", "description"),
    [
        ("\ndef screen(ctx):\n    return None\n", "none"),
        ("\ndef screen(ctx):\n    return 1\n", "int"),
        ("\ndef screen(ctx):\n    return 'yes'\n", "string"),
        ("\ndef screen(ctx):\n    return [True]\n", "list"),
        ("\ndef screen(ctx):\n    return {}\n", "empty mapping"),
        (
            "\ndef screen(ctx):\n    return {ctx['facts']['symbol']: 1}\n",
            "non-bool decision",
        ),
        (
            "\ndef screen(ctx):\n    return {'OTHER': True}\n",
            "missing current symbol",
        ),
        (
            "\ndef screen(ctx):\n"
            "    return {'UNKNOWN': True, ctx['facts']['symbol']: True}\n",
            "unknown symbol",
        ),
        ("\ndef screen(ctx):\n    return\n", "implicit none"),
    ],
)
def test_output_shape_violations_fail_closed(
    script: str, description: str, make_context
) -> None:
    with pytest.raises(FilterEvaluationError) as raised:
        evaluate_filter(script, make_context())
    assert raised.value.error.kind is FilterErrorKind.OUTPUT_SHAPE, description
    assert raised.value.error.message


def test_duplicate_symbol_decision_fails_closed(make_context) -> None:
    script = (
        "\ndef screen(ctx):\n"
        "    symbol = ctx['facts']['symbol']\n"
        "    return {'MSFT': True, symbol: True}\n"
    )
    with pytest.raises(FilterEvaluationError) as raised:
        evaluate_universe(
            script, (make_context(symbol="AAPL"), make_context(symbol="MSFT"))
        )
    assert raised.value.error.kind is FilterErrorKind.OUTPUT_SHAPE


def test_script_runtime_error_fails_closed(make_context) -> None:
    with pytest.raises(FilterEvaluationError) as raised:
        evaluate_filter("\ndef screen(ctx):\n    return 1 / 0\n", make_context())
    assert raised.value.error.kind is FilterErrorKind.SCRIPT_ERROR


def test_missing_name_fails_closed_as_script_error(make_context) -> None:
    with pytest.raises(FilterEvaluationError) as raised:
        evaluate_filter(
            "\ndef screen(ctx):\n    return undefined_name\n", make_context()
        )
    assert raised.value.error.kind is FilterErrorKind.SCRIPT_ERROR


def test_runtime_import_failure_fails_closed(make_context) -> None:
    script = "\ndef screen(ctx):\n    import os\n    return True\n"
    with pytest.raises(FilterEvaluationError) as raised:
        evaluate_filter(script, make_context())
    assert raised.value.error.kind is FilterErrorKind.IMPORT_BLOCKED


def test_infinite_loop_is_killed_at_wall_clock(make_context) -> None:
    timeout = 3.0
    started = time.perf_counter()
    with pytest.raises(FilterEvaluationError) as raised:
        evaluate_filter(
            "\ndef screen(ctx):\n    while True:\n        pass\n    return True\n",
            make_context(),
            timeout_seconds=timeout,
        )
    elapsed = time.perf_counter() - started
    record = raised.value.error
    assert record.kind is FilterErrorKind.TIMEOUT
    assert record.script_hash
    assert elapsed >= timeout * 0.9
    assert elapsed < 30.0


def test_busy_loop_with_work_is_killed(make_context) -> None:
    script = (
        "\ndef screen(ctx):\n"
        "    value = 0\n"
        "    while True:\n"
        "        value = (value + 1) % 7\n"
        "    return True\n"
    )
    with pytest.raises(FilterEvaluationError) as raised:
        evaluate_filter(script, make_context(), timeout_seconds=3.0)
    assert raised.value.error.kind is FilterErrorKind.TIMEOUT


def test_sleeping_import_is_blocked_statically(make_context) -> None:
    script = "import time\n\ndef screen(ctx):\n    time.sleep(30)\n    return True\n"
    with pytest.raises(FilterEvaluationError) as raised:
        evaluate_filter(script, make_context())
    assert raised.value.error.kind is FilterErrorKind.IMPORT_BLOCKED


@pytest.mark.parametrize("timeout", [0, -1.0, float("nan"), float("inf")])
def test_invalid_timeout_fails_closed(timeout: float, make_context) -> None:
    with pytest.raises(FilterEvaluationError) as raised:
        evaluate_filter(SIMPLE_TRUE, make_context(), timeout_seconds=timeout)
    assert raised.value.error.kind is FilterErrorKind.INPUT_INVALID


def test_non_string_script_fails_closed(make_context) -> None:
    with pytest.raises(FilterEvaluationError) as raised:
        evaluate_filter(cast(str, b"def screen(ctx):\n    return True"), make_context())
    assert raised.value.error.kind is FilterErrorKind.INPUT_INVALID


def test_non_filter_context_fails_closed() -> None:
    with pytest.raises(FilterEvaluationError) as raised:
        evaluate_universe(SIMPLE_TRUE, cast("tuple[FilterContext, ...]", (object(),)))
    assert raised.value.error.kind is FilterErrorKind.INPUT_INVALID


def test_single_context_passed_where_batch_expected_fails_closed(
    make_context,
) -> None:
    with pytest.raises(FilterEvaluationError) as raised:
        evaluate_universe(SIMPLE_TRUE, make_context())  # type: ignore[arg-type]
    assert raised.value.error.kind is FilterErrorKind.INPUT_INVALID


def test_duplicate_symbols_fail_closed(make_context) -> None:
    with pytest.raises(FilterEvaluationError) as raised:
        evaluate_universe(SIMPLE_TRUE, (make_context("AAPL"), make_context("AAPL")))
    assert raised.value.error.kind is FilterErrorKind.INPUT_INVALID


def test_unserializable_context_fails_closed(make_facts) -> None:
    bars = (
        BarRecord(
            date(2024, 5, 1),
            Decimal("1e999"),
            Decimal("1e999"),
            Decimal("1e999"),
            Decimal("1e999"),
            10,
        ),
    )
    context = FilterContext(make_facts(), bars, None)
    with pytest.raises(FilterEvaluationError) as raised:
        evaluate_filter(SIMPLE_TRUE, context)
    assert raised.value.error.kind is FilterErrorKind.INPUT_INVALID


def test_default_timeout_is_sixty_seconds() -> None:
    assert DEFAULT_TIMEOUT_SECONDS == 60.0


def test_batch_preserves_submission_order(make_context) -> None:
    symbols = ("ZZTOP", "AAPL", "MMM")
    batch = evaluate_universe(SIMPLE_TRUE, [make_context(symbol=s) for s in symbols])
    assert list(batch.decisions) == list(symbols)


def test_worker_spawn_failure_surfaces_as_worker_failure(
    monkeypatch, make_context
) -> None:
    from harness.screening import runner

    def explode(*args: object, **kwargs: object) -> None:
        raise OSError("spawn denied")

    monkeypatch.setattr(runner.subprocess, "Popen", explode)
    with pytest.raises(FilterEvaluationError) as raised:
        evaluate_filter(SIMPLE_TRUE, make_context())
    assert raised.value.error.kind is FilterErrorKind.WORKER_FAILURE


def test_worker_response_hash_mismatch_fails_closed(monkeypatch, make_context) -> None:
    import json as json_module

    from harness.screening import runner

    real_load = json_module.loads

    def tampered(data: str | bytes | bytearray, **kwargs: Any) -> Any:
        document = real_load(data, **kwargs)
        if (
            isinstance(document, dict)
            and document.get("protocol") == "fca-harness-screening-worker-v1"
        ):
            document["script_hash"] = "0" * 64
        return document

    monkeypatch.setattr(runner.json, "loads", tampered)
    with pytest.raises(FilterEvaluationError) as raised:
        evaluate_filter(SIMPLE_TRUE, make_context())
    assert raised.value.error.kind is FilterErrorKind.WORKER_FAILURE


def test_decimal_prices_round_trip_through_the_sandbox(make_facts) -> None:
    bars = (
        BarRecord(
            date(2024, 5, 1),
            Decimal("1.1"),
            Decimal("3.2"),
            Decimal("0.3"),
            Decimal("2.1"),
            10,
        ),
    )
    context = FilterContext(make_facts(), bars, None)
    script = (
        "\ndef screen(ctx):\n"
        "    bar = ctx['bars'][0]\n"
        "    return abs(bar['close'] + 0.1 + 0.2 - (bar['close'] + 0.3)) < 1e-9\n"
    )
    result = evaluate_filter(script, context)
    assert result.passed is True
