from __future__ import annotations

import json
from datetime import date

import pytest

from thesis.screening import (
    MarketBar,
    ScreenerDefinition,
    ScreenerError,
    ScreenerPortError,
    UniverseEntry,
    and_filters,
    compile_program,
    load_study,
    resolve_filter,
    run_program,
    run_screener,
    save_study,
)
from thesis.screening.artifacts import SavedStudy
from thesis.screening.programs import (
    ProgramViolationError,
    ScreenContext,
)

AS_OF = date(2026, 9, 9)
UNIVERSE_REVISION = "universe-2026-09-09-v3"


def _context(
    *,
    symbols: tuple[str, ...] = ("AAA", "BBB", "CCC"),
    post_as_of_bar: bool = False,
    dollar_volumes: tuple[float, ...] = (5_000_000.0, 10_000_000.0, 2_000_000.0),
) -> ScreenContext:
    universe = tuple(
        UniverseEntry(symbol=symbol, listing_date=AS_OF.replace(year=2025))
        for symbol in symbols
    )
    bars: dict[str, tuple[MarketBar, ...]] = {}
    for index, symbol in enumerate(symbols):
        series = list(
            MarketBar(
                bar_date=date(2026, 8, 1 + offset),
                volume=1_000_000,
                close="10.00",
                dollar_volume=str(dollar_volumes[index % len(dollar_volumes)]),
            )
            for offset in range(20)
        )
        if post_as_of_bar and symbol == symbols[0]:
            series.append(
                MarketBar(
                    bar_date=date(2026, 9, 10),
                    volume=999_999_999,
                    close="999.00",
                    dollar_volume="999999999",
                )
            )
        bars[symbol] = tuple(series)
    return ScreenContext(
        symbols=symbols,
        universe=lambda _symbol: universe,
        market_data=lambda symbol, window: bars.get(symbol, ()),
    )


def _definition(**overrides):
    filters = overrides.pop(
        "filters",
        ({"kind": "avg_dollar_volume_min", "params": {"min": 1_000_000}},),
    )
    return ScreenerDefinition(
        name="liquid-large",
        universe_revision=UNIVERSE_REVISION,
        filters=filters,
        **overrides,
    )


# --------------------------------------------------------------------------
# Engine: point-in-time discipline and determinism
# --------------------------------------------------------------------------


def test_screen_excludes_bars_after_the_as_of_date() -> None:
    # AAA carries a post-as-of bar with an enormous dollar volume; if the
    # engine ever saw it, AAA would outrank BBB despite equal in-window data.
    context = _context(
        post_as_of_bar=True,
        dollar_volumes=(5_000_000.0, 5_000_000.0, 2_000_000.0),
    )
    result = run_screener(_definition(), context, as_of=AS_OF)

    aaa = next(hit for hit in result.hits if hit.symbol == "AAA")
    bbb = next(hit for hit in result.hits if hit.symbol == "BBB")
    assert aaa.metrics["avg_dollar_volume"] == bbb.metrics["avg_dollar_volume"]


def test_screen_returns_deterministic_ranking_with_symbol_tiebreak() -> None:
    context = _context(dollar_volumes=(5_000_000.0, 5_000_000.0, 10_000_000.0))
    result = run_screener(_definition(), context, as_of=AS_OF)

    assert [hit.symbol for hit in result.hits] == ["CCC", "AAA", "BBB"]
    assert [hit.rank for hit in result.hits] == [1, 2, 3]
    assert run_screener(_definition(), context, as_of=AS_OF).canonical_json() == (
        result.canonical_json()
    )


def test_screen_carries_provenance() -> None:
    definition = _definition()
    result = run_screener(definition, _context(), as_of=AS_OF)

    assert result.universe_revision == UNIVERSE_REVISION
    assert result.definition_hash == definition.definition_hash
    assert result.data_window[1] == AS_OF
    assert result.data_window[0] < AS_OF


def test_definition_rejects_invalid_declarations() -> None:
    with pytest.raises(ScreenerError, match="lookback"):
        _definition(lookback_days=0)
    with pytest.raises(ScreenerError, match="at least one filter"):
        _definition(filters=())
    with pytest.raises(ScreenerError, match="mapping"):
        _definition(filters=(("not-a-mapping",),))


# --------------------------------------------------------------------------
# Presets
# --------------------------------------------------------------------------


def test_presets_evaluate_and_compose() -> None:
    volume = resolve_filter("avg_dollar_volume_min", {"min": 6_000_000})
    price = resolve_filter("min_price", {"min": 11.0})
    composed = and_filters(volume, price)
    metrics = {
        "avg_dollar_volume": "10000000.0",
        "min_close": "9.0",
        "listing_age_days": 400,
        "avg_volume": "1000000.0",
    }

    assert volume.evaluate(metrics, percentile=0.5)
    assert not price.evaluate(metrics, percentile=0.5)
    assert not composed.evaluate(metrics, percentile=0.5)


def test_percentile_filter_uses_the_candidate_set_rank() -> None:
    percentile_filter = resolve_filter("avg_volume_percentile_min", {"min": 0.9})
    metrics = {"avg_dollar_volume": "1.0", "avg_volume": "1000000.0"}

    assert percentile_filter.evaluate(metrics, percentile=0.95)
    assert not percentile_filter.evaluate(metrics, percentile=0.4)


def test_invalid_filter_parameters_fail_at_declaration() -> None:
    with pytest.raises(ScreenerError, match="unknown filter kind"):
        resolve_filter("moon_phase", {})
    with pytest.raises(ScreenerError, match="must be a number"):
        resolve_filter("min_price", {"min": "lots"})
    with pytest.raises(ScreenerError, match="within"):
        resolve_filter("avg_volume_percentile_min", {"min": 1.5})


# --------------------------------------------------------------------------
# Programmatic screeners: the compilation boundary
# --------------------------------------------------------------------------


GOOD_PROGRAM = """
import math

def screen(ctx):
    rows = []
    for symbol in ctx.available_symbols():
        bars = ctx.market_data(symbol, "as_of")
        rows.append({"symbol": symbol, "count": len(bars)})
    return rows
"""


def test_compiled_program_runs_against_the_context() -> None:
    program = compile_program(GOOD_PROGRAM)
    context = _context()

    rows = run_program(program, context)

    assert [row["symbol"] for row in rows] == ["AAA", "BBB", "CCC"]


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ("import os\n\ndef screen(ctx):\n    return []\n", "outside the allowlist"),
        (
            "x = 5\n\ndef screen(ctx):\n    return []\n",
            "not permitted",
        ),
        (
            "def screen(ctx):\n    return 1\n\ndef helper():\n    return 2\n",
            "exactly one screen",
        ),
        (
            "def helper(ctx):\n    return []\n",
            "exactly one screen",
        ),
        (
            "def screen(ctx):\n    return ctx.__class__\n",
            "dunder",
        ),
        (
            "from json import loads\n\ndef screen(ctx):\n    return []\n",
            "outside the allowlist",
        ),
        ("import math as m\n\ndef screen(ctx):\n    return []\n", "allowlist"),
    ],
)
def test_program_violations_fail_closed(source: str, reason: str) -> None:
    with pytest.raises(ProgramViolationError, match=reason):
        compile_program(source)


def test_program_division_by_zero_fails_closed_with_the_script_error() -> None:
    program = compile_program("def screen(ctx):\n    return 1 / 0\n")

    with pytest.raises(ScreenerError, match="custom screener failed"):
        program.run(_context())


def test_program_with_wrong_return_shape_fails_closed() -> None:
    program = compile_program("def screen(ctx):\n    return 'rows'\n")

    with pytest.raises(ScreenerError, match="list of dicts"):
        program.run(_context())


def test_program_boundary_blocks_open_and_import_escape_hatches() -> None:
    blocked = compile_program("def screen(ctx):\n    return [open('secrets')]\n")
    with pytest.raises(ScreenerError, match="custom screener failed"):
        run_program(blocked, _context())

    importer = compile_program(
        "def screen(ctx):\n    return __import__('os').environ\n"
    )
    with pytest.raises(ScreenerError, match="custom screener failed"):
        run_program(importer, _context())


# --------------------------------------------------------------------------
# Artifacts: provenance and round-trip
# --------------------------------------------------------------------------


def test_saved_study_round_trips_with_provenance() -> None:
    definition = _definition()
    result = run_screener(definition, _context(), as_of=AS_OF)
    study = SavedStudy(
        name="liquid-200-september",
        definition=definition,
        result=result,
        git_revision="abc123",
        saved_at=AS_OF,
    )

    payload = save_study(study)
    restored = load_study(payload)

    assert restored.canonical_document == study.canonical_document
    assert restored.content_sha256 == study.content_sha256
    document = json.loads(payload.decode("utf-8"))
    assert document["definition_hash"] == definition.definition_hash
    assert document["git_revision"] == "abc123"


def test_study_rejects_a_tampered_payload() -> None:
    definition = _definition()
    result = run_screener(definition, _context(), as_of=AS_OF)
    study = SavedStudy(
        name="liquid-200",
        definition=definition,
        result=result,
        git_revision="abc",
        saved_at=AS_OF,
    )
    document = json.loads(save_study(study).decode("utf-8"))
    document["name"] = "renamed"

    with pytest.raises(ScreenerError, match="content hash"):
        load_study(json.dumps(document).encode("utf-8"))


def test_study_rejects_mismatched_definition_hashes() -> None:
    definition = _definition()
    result = run_screener(definition, _context(), as_of=AS_OF)
    other = _definition(lookback_days=40)
    with pytest.raises(ScreenerError, match="different definition hashes"):
        SavedStudy(
            name="mismatch",
            definition=other,
            result=result,
            git_revision="abc",
            saved_at=AS_OF,
        )


def test_port_failures_are_wrapped_as_screener_errors() -> None:
    class BrokenContext:
        def universe(self, as_of):
            raise OSError("backend down")

        def market_data(self, symbol, window_end):
            return ()

    with pytest.raises(ScreenerPortError, match="backend down"):
        run_screener(_definition(), BrokenContext(), as_of=AS_OF)
