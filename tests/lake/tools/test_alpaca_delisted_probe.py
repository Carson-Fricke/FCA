import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from lake.providers.alpaca import AlpacaClient, AlpacaHTTPError
from lake.tools.delisted_probe import (
    DEFAULT_SYMBOLS,
    ProbeConfig,
    ProbeSymbol,
    main,
    probe_symbols,
    serialize_report,
)


@dataclass(frozen=True, slots=True)
class FakeRecord:
    timestamp: datetime


class FakeProvider:
    def __init__(self, outcomes: Mapping[str, Sequence[FakeRecord] | BaseException]):
        self.outcomes = dict(outcomes)
        self.calls: list[dict[str, object]] = []

    async def get_trades(
        self,
        symbol: str,
        start: date | datetime | str,
        end: date | datetime | str,
        *,
        feed: str = "sip",
        adjustment: str = "raw",
        limit: int = 10_000,
        max_records: int | None = None,
    ) -> Sequence[FakeRecord]:
        self.calls.append(
            {
                "symbol": symbol,
                "start": start,
                "end": end,
                "feed": feed,
                "adjustment": adjustment,
                "limit": limit,
                "max_records": max_records,
            }
        )
        outcome = self.outcomes[symbol]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def get_quotes(
        self,
        symbol: str,
        start: date | datetime | str,
        end: date | datetime | str,
        *,
        feed: str = "sip",
        adjustment: str = "raw",
        limit: int = 10_000,
        max_records: int | None = None,
    ) -> Sequence[FakeRecord]:
        return await self.get_trades(
            symbol,
            start,
            end,
            feed=feed,
            adjustment=adjustment,
            limit=limit,
            max_records=max_records,
        )


class FakeResponse:
    status_code = 200
    headers: Mapping[str, str] = {}

    def __init__(self, payload: object):
        self.payload = payload

    def json(self) -> object:
        return self.payload


class FakeTransport:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str | int],
        headers: Mapping[str, str],
        timeout: float,
    ) -> FakeResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": dict(params),
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        return self.response


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_default_cohorts_are_30_unique_symbols_with_all_requested_cases() -> None:
    assert len(DEFAULT_SYMBOLS) == 30
    assert len({item.symbol for item in DEFAULT_SYMBOLS}) == 30
    assert {item.cohort for item in DEFAULT_SYMBOLS} == {
        "acquisition",
        "bankruptcy",
        "delisted",
        "reused",
    }


def test_fake_provider_is_bounded_safe_and_recommends_supplemental_data() -> None:
    secret = "api-secret-must-not-appear"
    provider = FakeProvider(
        {
            "EMPTY": (),
            "ERROR": AlpacaHTTPError(401, f"https://{secret}@alpaca.invalid"),
            "FOUND": (
                FakeRecord(datetime(2024, 1, 2, 15, tzinfo=UTC)),
                FakeRecord(datetime(2024, 1, 2, 16, tzinfo=UTC)),
            ),
        }
    )
    config = ProbeConfig(
        symbols=(
            ProbeSymbol("found", "reused", 2022),
            "empty",
            "error",
        ),
        start="2020-01-01T00:00:00Z",
        end="2025-01-01T00:00:00Z",
        max_records=1,
    )

    report = _run(probe_symbols(provider, config))
    serialized = serialize_report(report)
    result_by_symbol = {result.symbol: result for result in report.results}

    assert [call["symbol"] for call in provider.calls] == ["EMPTY", "ERROR", "FOUND"]
    assert all(call["max_records"] == 1 for call in provider.calls)
    assert result_by_symbol["FOUND"].record_count == 1
    assert result_by_symbol["FOUND"].first_timestamp == "2024-01-02T15:00:00Z"
    assert result_by_symbol["EMPTY"].status == "empty"
    assert result_by_symbol["ERROR"].error_code == "http_401"
    assert report.recommendation == "go"
    assert secret not in serialized
    assert "api-secret" not in serialized


def test_serialization_is_deterministic_and_does_not_have_run_timestamp() -> None:
    provider = FakeProvider(
        {"AAPL": (FakeRecord(datetime(2024, 1, 2, 15, tzinfo=UTC)),)}
    )
    config = ProbeConfig(symbols=("aapl",), data_type="quotes")
    report = _run(probe_symbols(provider, config))

    first = serialize_report(report)
    second = serialize_report(report)

    assert first == second
    assert first.endswith("\n")
    assert '"data_type": "quotes"' in first
    assert "generated_at" not in first


def test_existing_alpaca_boundary_accepts_injected_transport() -> None:
    transport = FakeTransport(
        FakeResponse(
            {
                "trades": {
                    "OLD": [
                        {
                            "t": "2024-01-02T15:00:00Z",
                            "x": "V",
                            "p": "1.25",
                            "s": 3,
                            "c": [],
                            "i": 7,
                            "z": "C",
                        }
                    ]
                },
                "next_page_token": None,
            }
        )
    )
    client = AlpacaClient(transport=transport, max_attempts=1)
    config = ProbeConfig(symbols=("old",), limit=7, max_records=1)

    report = _run(probe_symbols(client, config))
    _run(client.aclose())

    assert report.results[0].status == "records"
    assert report.results[0].record_count == 1
    assert transport.calls[0]["params"] == {
        "start": "2010-01-01T00:00:00Z",
        "end": "2025-01-01T00:00:00Z",
        "limit": 7,
        "sort": "asc",
        "symbols": "OLD",
        "feed": "sip",
    }
    assert transport.calls[0]["headers"] == {"Accept": "application/json"}


def test_cli_requires_explicit_live_flag_without_injected_provider(capsys: Any) -> None:
    assert main([]) == 2
    captured = capsys.readouterr()
    assert "without --live" in captured.err


def test_cli_writes_injected_provider_report(tmp_path: Path) -> None:
    provider = FakeProvider(
        {"OLD": (FakeRecord(datetime(2024, 1, 2, 15, tzinfo=UTC)),)}
    )
    output = tmp_path / "report.json"

    assert (
        main(
            [
                "--symbols",
                "old",
                "--output",
                str(output),
            ],
            provider=provider,
        )
        == 0
    )

    text = output.read_text(encoding="utf-8")
    assert '"symbol": "OLD"' in text
    assert '"status": "records"' in text
