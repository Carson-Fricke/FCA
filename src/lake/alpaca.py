"""Async Alpaca market-data REST client.

The client deliberately keeps HTTP transport and sleeping injectable. This
allows callers to use an ``httpx.AsyncClient`` in production and lets tests
exercise pagination, retries, and cancellation without provider calls.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, Literal, Protocol, TypeVar, cast
from urllib.parse import quote

import httpx

from lake.catalog import ContractRecord, OptionType
from lake.settings import ProviderSettings

type QueryValue = str | int
type QueryParams = Mapping[str, QueryValue]
type Sleep = Callable[[float], Awaitable[None]]
type Clock = Callable[[], datetime]


class AsyncResponse(Protocol):
    """Small response surface required by the client and test fakes."""

    status_code: int
    headers: Mapping[str, str]

    def json(self) -> object:
        """Decode the response body."""


class AsyncTransport(Protocol):
    """Request surface implemented by ``httpx.AsyncClient`` and test fakes."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: QueryParams,
        headers: Mapping[str, str],
        timeout: float,
    ) -> AsyncResponse:
        """Send one asynchronous HTTP request."""


@dataclass(frozen=True, slots=True)
class NormalizedTrade:
    """Equity trade normalized to the T021 persisted field names."""

    timestamp: datetime
    symbol: str
    trade_id: int
    exchange: str
    price: Decimal
    size: int
    conditions: tuple[str, ...]
    tape: str
    update: str | None


@dataclass(frozen=True, slots=True)
class NormalizedQuote:
    """Equity quote normalized to the T021 persisted field names."""

    timestamp: datetime
    symbol: str
    bid_exchange: str
    bid_price: Decimal
    bid_size: int
    ask_exchange: str
    ask_price: Decimal
    ask_size: int
    conditions: tuple[str, ...]
    tape: str


@dataclass(frozen=True, slots=True)
class NormalizedOptionTrade:
    """OPRA option trade with its selected T024 contract metadata."""

    timestamp: datetime
    symbol: str
    underlying: str
    expiration_date: date
    strike_price: Decimal
    option_type: OptionType
    exchange: str
    price: Decimal
    size: int
    condition: str

    @property
    def osi(self) -> str:
        """Return the OSI contract symbol."""
        return self.symbol

    @property
    def expiry(self) -> date:
        """Return the option expiration date."""
        return self.expiration_date

    @property
    def strike(self) -> Decimal:
        """Return the option strike price."""
        return self.strike_price

    @property
    def right(self) -> OptionType:
        """Return the call or put right."""
        return self.option_type

    @property
    def call_put(self) -> OptionType:
        """Return the call or put metadata."""
        return self.option_type


@dataclass(frozen=True, slots=True)
class NormalizedOptionQuote:
    """OPRA option quote with its selected T024 contract metadata."""

    timestamp: datetime
    symbol: str
    underlying: str
    expiration_date: date
    strike_price: Decimal
    option_type: OptionType
    bid_exchange: str
    bid_price: Decimal
    bid_size: int
    ask_exchange: str
    ask_price: Decimal
    ask_size: int
    condition: str

    @property
    def osi(self) -> str:
        """Return the OSI contract symbol."""
        return self.symbol

    @property
    def expiry(self) -> date:
        """Return the option expiration date."""
        return self.expiration_date

    @property
    def strike(self) -> Decimal:
        """Return the option strike price."""
        return self.strike_price

    @property
    def right(self) -> OptionType:
        """Return the call or put right."""
        return self.option_type

    @property
    def call_put(self) -> OptionType:
        """Return the call or put metadata."""
        return self.option_type


@dataclass(frozen=True, slots=True)
class NormalizedCryptoTrade:
    """Crypto trade normalized to the T021 nullable crypto schema."""

    timestamp: datetime
    symbol: str
    trade_id: int | None
    exchange: str | None
    price: Decimal
    size: Decimal
    taker_side: str | None


@dataclass(frozen=True, slots=True)
class NormalizedCryptoQuote:
    """Crypto quote normalized to the T021 nullable crypto schema."""

    timestamp: datetime
    symbol: str
    bid_exchange: str | None
    bid_price: Decimal
    bid_size: Decimal
    ask_exchange: str | None
    ask_price: Decimal
    ask_size: Decimal


RecordT = TypeVar("RecordT")
OptionRecordT = TypeVar("OptionRecordT", NormalizedOptionTrade, NormalizedOptionQuote)


# Short aliases make the result types convenient for ingestion code while the
# explicit names document that these are normalized provider records.
Trade = NormalizedTrade
Quote = NormalizedQuote
OptionTrade = NormalizedOptionTrade
OptionQuote = NormalizedOptionQuote
CryptoTrade = NormalizedCryptoTrade
CryptoQuote = NormalizedCryptoQuote


class AlpacaHTTPError(RuntimeError):
    """A non-successful Alpaca response after the retry policy is exhausted."""

    def __init__(self, status_code: int, url: str) -> None:
        self.status_code = status_code
        self.url = url
        super().__init__(f"Alpaca request failed with HTTP {status_code}: {url}")


class AlpacaProtocolError(RuntimeError):
    """The provider returned a malformed response or a looping cursor."""


class AlpacaClient:
    """Fetch paginated Alpaca stock and crypto data asynchronously.

    ``transport`` is optional in application code and can be an
    ``httpx.AsyncClient``. A fake implementing :class:`AsyncTransport` can be
    supplied for tests. The client only owns and closes the default client it
    creates itself.
    """

    _RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        base_url: str = "https://data.alpaca.markets",
        max_concurrency: int = 4,
        timeout_seconds: float = 30.0,
        max_attempts: int = 5,
        honor_retry_after: bool = True,
        backoff_seconds: float = 0.25,
        max_backoff_seconds: float = 30.0,
        transport: AsyncTransport | None = None,
        sleep: Sleep | None = None,
        clock: Clock | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if backoff_seconds < 0:
            raise ValueError("backoff_seconds cannot be negative")
        if max_backoff_seconds < 0:
            raise ValueError("max_backoff_seconds cannot be negative")

        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self.max_attempts = max_attempts
        self.honor_retry_after = honor_retry_after
        self.backoff_seconds = backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self._sleep = asyncio.sleep if sleep is None else sleep
        self._clock = _utc_now if clock is None else clock
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._default_client: httpx.AsyncClient | None = None

        if transport is None:
            self._default_client = httpx.AsyncClient(timeout=self.timeout_seconds)
            self._transport: AsyncTransport = cast(AsyncTransport, self._default_client)
        else:
            self._transport = transport

        self._headers: dict[str, str] = {"Accept": "application/json"}
        if api_key is not None:
            self._headers["APCA-API-KEY-ID"] = api_key
        if api_secret is not None:
            self._headers["APCA-API-SECRET-KEY"] = api_secret

    @classmethod
    def from_settings(
        cls,
        settings: ProviderSettings,
        **kwargs: Any,
    ) -> AlpacaClient:
        """Construct a client from the T026 provider request settings."""
        return cls(
            api_key=(
                None
                if settings.api_key is None
                else settings.api_key.get_secret_value()
            ),
            api_secret=(
                None
                if settings.api_secret is None
                else settings.api_secret.get_secret_value()
            ),
            max_concurrency=settings.request.max_concurrency,
            timeout_seconds=settings.request.timeout_seconds,
            max_attempts=settings.request.retry.max_attempts,
            honor_retry_after=settings.request.retry.honor_retry_after,
            **kwargs,
        )

    async def __aenter__(self) -> AlpacaClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the internally-created HTTP client, if there is one."""
        if self._default_client is not None:
            client = self._default_client
            self._default_client = None
            await client.aclose()

    async def fetch_trades(
        self,
        symbols: str | Iterable[str],
        start: date | datetime | str,
        end: date | datetime | str,
        *,
        feed: Literal["sip"] = "sip",
        adjustment: Literal["raw"] = "raw",
        limit: int = 10_000,
    ) -> tuple[NormalizedTrade, ...]:
        """Fetch all pages of trades for each symbol without duplicate requests."""
        symbol_list = _normalize_symbols(symbols)
        _validate_query_options(feed, adjustment, limit)
        pages = await self._fetch_many(
            symbol_list,
            data_type="trades",
            start=start,
            end=end,
            feed=feed,
            adjustment=adjustment,
            limit=limit,
            parser=_normalize_trade,
        )
        return tuple(record for page in pages for record in page)

    async def fetch_quotes(
        self,
        symbols: str | Iterable[str],
        start: date | datetime | str,
        end: date | datetime | str,
        *,
        feed: Literal["sip"] = "sip",
        adjustment: Literal["raw"] = "raw",
        limit: int = 10_000,
    ) -> tuple[NormalizedQuote, ...]:
        """Fetch all pages of quotes for each symbol without duplicate requests."""
        symbol_list = _normalize_symbols(symbols)
        _validate_query_options(feed, adjustment, limit)
        pages = await self._fetch_many(
            symbol_list,
            data_type="quotes",
            start=start,
            end=end,
            feed=feed,
            adjustment=adjustment,
            limit=limit,
            parser=_normalize_quote,
        )
        return tuple(record for page in pages for record in page)

    async def get_trades(
        self,
        symbol: str,
        start: date | datetime | str,
        end: date | datetime | str,
        *,
        feed: Literal["sip"] = "sip",
        adjustment: Literal["raw"] = "raw",
        limit: int = 10_000,
    ) -> tuple[NormalizedTrade, ...]:
        """Fetch trades for one symbol."""
        return await self.fetch_trades(
            symbol,
            start,
            end,
            feed=feed,
            adjustment=adjustment,
            limit=limit,
        )

    async def get_quotes(
        self,
        symbol: str,
        start: date | datetime | str,
        end: date | datetime | str,
        *,
        feed: Literal["sip"] = "sip",
        adjustment: Literal["raw"] = "raw",
        limit: int = 10_000,
    ) -> tuple[NormalizedQuote, ...]:
        """Fetch quotes for one symbol."""
        return await self.fetch_quotes(
            symbol,
            start,
            end,
            feed=feed,
            adjustment=adjustment,
            limit=limit,
        )

    async def fetch_option_trades(
        self,
        contracts: Iterable[ContractRecord],
        start: date | datetime | str,
        end: date | datetime | str,
        *,
        feed: Literal["opra"] = "opra",
        limit: int = 1_000,
    ) -> tuple[NormalizedOptionTrade, ...]:
        """Fetch historical OPRA trades for selected option contracts."""
        contract_list = _normalize_contracts(contracts)
        _validate_option_query_options(feed, limit)
        return await self._fetch_option_data(
            contract_list,
            data_type="trades",
            start=start,
            end=end,
            feed=feed,
            limit=limit,
            parser=_normalize_option_trade,
        )

    async def fetch_option_quotes(
        self,
        contracts: Iterable[ContractRecord],
        start: date | datetime | str,
        end: date | datetime | str,
        *,
        feed: Literal["opra"] = "opra",
        limit: int = 1_000,
    ) -> tuple[NormalizedOptionQuote, ...]:
        """Fetch historical OPRA quotes for selected option contracts."""
        contract_list = _normalize_contracts(contracts)
        _validate_option_query_options(feed, limit)
        return await self._fetch_option_data(
            contract_list,
            data_type="quotes",
            start=start,
            end=end,
            feed=feed,
            limit=limit,
            parser=_normalize_option_quote,
        )

    async def get_option_trades(
        self,
        contracts: Iterable[ContractRecord],
        start: date | datetime | str,
        end: date | datetime | str,
        *,
        feed: Literal["opra"] = "opra",
        limit: int = 1_000,
    ) -> tuple[NormalizedOptionTrade, ...]:
        """Fetch historical OPRA trades for selected contracts."""
        return await self.fetch_option_trades(
            contracts, start, end, feed=feed, limit=limit
        )

    async def get_option_quotes(
        self,
        contracts: Iterable[ContractRecord],
        start: date | datetime | str,
        end: date | datetime | str,
        *,
        feed: Literal["opra"] = "opra",
        limit: int = 1_000,
    ) -> tuple[NormalizedOptionQuote, ...]:
        """Fetch historical OPRA quotes for selected contracts."""
        return await self.fetch_option_quotes(
            contracts, start, end, feed=feed, limit=limit
        )

    async def fetch_crypto_trades(
        self,
        pairs: str | Iterable[str],
        start: date | datetime | str,
        end: date | datetime | str,
        *,
        limit: int = 10_000,
    ) -> tuple[NormalizedCryptoTrade, ...]:
        """Fetch historical trades for configured crypto pairs."""
        pair_list = _normalize_crypto_pairs(pairs)
        _validate_limit(limit)
        pages = await self._fetch_many(
            pair_list,
            data_type="trades",
            start=start,
            end=end,
            limit=limit,
            market="crypto",
            parser=_normalize_crypto_trade,
        )
        return tuple(record for page in pages for record in page)

    async def fetch_crypto_quotes(
        self,
        pairs: str | Iterable[str],
        start: date | datetime | str,
        end: date | datetime | str,
        *,
        limit: int = 10_000,
    ) -> tuple[NormalizedCryptoQuote, ...]:
        """Fetch historical quotes for configured crypto pairs."""
        pair_list = _normalize_crypto_pairs(pairs)
        _validate_limit(limit)
        pages = await self._fetch_many(
            pair_list,
            data_type="quotes",
            start=start,
            end=end,
            limit=limit,
            market="crypto",
            parser=_normalize_crypto_quote,
        )
        return tuple(record for page in pages for record in page)

    async def get_crypto_trades(
        self,
        pair: str,
        start: date | datetime | str,
        end: date | datetime | str,
        *,
        limit: int = 10_000,
    ) -> tuple[NormalizedCryptoTrade, ...]:
        """Fetch historical trades for one crypto pair."""
        return await self.fetch_crypto_trades(pair, start, end, limit=limit)

    async def get_crypto_quotes(
        self,
        pair: str,
        start: date | datetime | str,
        end: date | datetime | str,
        *,
        limit: int = 10_000,
    ) -> tuple[NormalizedCryptoQuote, ...]:
        """Fetch historical quotes for one crypto pair."""
        return await self.fetch_crypto_quotes(pair, start, end, limit=limit)

    async def _fetch_many(
        self,
        symbols: Sequence[str],
        *,
        data_type: Literal["trades", "quotes"],
        start: date | datetime | str,
        end: date | datetime | str,
        limit: int,
        market: Literal["stocks", "crypto"] = "stocks",
        feed: Literal["sip"] = "sip",
        adjustment: Literal["raw"] = "raw",
        parser: Callable[[Mapping[str, object], str], RecordT],
    ) -> list[list[RecordT]]:
        tasks = [
            asyncio.create_task(
                self._fetch_symbol(
                    symbol,
                    data_type=data_type,
                    start=start,
                    end=end,
                    limit=limit,
                    market=market,
                    feed=feed,
                    adjustment=adjustment,
                    parser=parser,
                )
            )
            for symbol in symbols
        ]
        if not tasks:
            return []

        try:
            return list(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _fetch_symbol(
        self,
        symbol: str,
        *,
        data_type: Literal["trades", "quotes"],
        start: date | datetime | str,
        end: date | datetime | str,
        limit: int,
        market: Literal["stocks", "crypto"],
        feed: Literal["sip"],
        adjustment: Literal["raw"],
        parser: Callable[[Mapping[str, object], str], RecordT],
    ) -> list[RecordT]:
        page_token: str | None = None
        seen_tokens: set[str] = set()
        records: list[RecordT] = []

        while True:
            params: dict[str, QueryValue] = {
                "start": _format_bound(start),
                "end": _format_bound(end),
                "limit": limit,
                "sort": "asc",
            }
            if page_token is not None:
                params["page_token"] = page_token

            if market == "crypto":
                params["symbols"] = symbol
                path = f"/v1beta3/crypto/us/{data_type}"
            else:
                params.update({"feed": feed, "adjustment": adjustment})
                path = f"/v2/stocks/{quote(symbol, safe='')}/{data_type}"
            response = await self._request(path, params)
            payload = response.json()
            rows, next_page_token = _page_rows(payload, data_type, symbol)
            records.extend(parser(row, symbol) for row in rows)

            if not next_page_token:
                return records
            if next_page_token in seen_tokens:
                raise AlpacaProtocolError(
                    f"repeated Alpaca {data_type} page token for {symbol!r}"
                )
            seen_tokens.add(next_page_token)
            page_token = next_page_token

    async def _fetch_option_data(
        self,
        contracts: Sequence[ContractRecord],
        *,
        data_type: Literal["trades", "quotes"],
        start: date | datetime | str,
        end: date | datetime | str,
        feed: Literal["opra"],
        limit: int,
        parser: Callable[[Mapping[str, object], ContractRecord], OptionRecordT],
    ) -> tuple[OptionRecordT, ...]:
        records: list[OptionRecordT] = []
        for offset in range(0, len(contracts), 100):
            records.extend(
                await self._fetch_option_batch(
                    contracts[offset : offset + 100],
                    data_type=data_type,
                    start=start,
                    end=end,
                    feed=feed,
                    limit=limit,
                    parser=parser,
                )
            )
        return tuple(records)

    async def _fetch_option_batch(
        self,
        contracts: Sequence[ContractRecord],
        *,
        data_type: Literal["trades", "quotes"],
        start: date | datetime | str,
        end: date | datetime | str,
        feed: Literal["opra"],
        limit: int,
        parser: Callable[[Mapping[str, object], ContractRecord], OptionRecordT],
    ) -> list[OptionRecordT]:
        contracts_by_symbol = {contract.symbol: contract for contract in contracts}
        page_token: str | None = None
        seen_tokens: set[str] = set()
        records: list[OptionRecordT] = []

        while True:
            params: dict[str, QueryValue] = {
                "symbols": ",".join(contracts_by_symbol),
                "start": _format_bound(start),
                "end": _format_bound(end),
                "feed": feed,
                "limit": limit,
                "sort": "asc",
            }
            if page_token is not None:
                params["page_token"] = page_token

            response = await self._request(f"/v1beta1/options/{data_type}", params)
            payload = response.json()
            rows, next_page_token = _option_page_rows(
                payload, data_type, contracts_by_symbol
            )
            records.extend(parser(row, contract) for contract, row in rows)

            if not next_page_token:
                return records
            if next_page_token in seen_tokens:
                raise AlpacaProtocolError(
                    f"repeated Alpaca option {data_type} page token"
                )
            seen_tokens.add(next_page_token)
            page_token = next_page_token

    async def _request(
        self, path: str, params: Mapping[str, QueryValue]
    ) -> AsyncResponse:
        url = f"{self.base_url}{path}"
        for attempt in range(1, self.max_attempts + 1):
            try:
                async with self._semaphore:
                    response = await self._transport.request(
                        "GET",
                        url,
                        params=params,
                        headers=self._headers,
                        timeout=self.timeout_seconds,
                    )
            except (httpx.TransportError, TimeoutError):
                if attempt == self.max_attempts:
                    raise
                await self._sleep(self._backoff(attempt))
                continue

            if 200 <= response.status_code < 300:
                return response
            if response.status_code not in self._RETRYABLE_STATUS_CODES:
                raise AlpacaHTTPError(response.status_code, url)
            if attempt == self.max_attempts:
                raise AlpacaHTTPError(response.status_code, url)

            delay = self._backoff(attempt)
            if response.status_code == 429 and self.honor_retry_after:
                retry_after = _retry_after_seconds(response.headers, self._clock())
                if retry_after is not None:
                    delay = min(retry_after, self.max_backoff_seconds)
            await self._sleep(delay)

        raise AssertionError("retry loop must return or raise")

    def _backoff(self, attempt: int) -> float:
        return min(
            self.backoff_seconds * (2 ** (attempt - 1)), self.max_backoff_seconds
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _normalize_symbols(symbols: str | Iterable[str]) -> tuple[str, ...]:
    values = [symbols] if isinstance(symbols, str) else list(symbols)
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise TypeError("symbols must contain strings")
        symbol = value.strip().upper()
        if not symbol or any(character.isspace() for character in symbol):
            raise ValueError(f"invalid market symbol: {value!r}")
        if symbol not in seen:
            seen.add(symbol)
            result.append(symbol)
    return tuple(result)


def _normalize_crypto_pairs(pairs: str | Iterable[str]) -> tuple[str, ...]:
    values = [pairs] if isinstance(pairs, str) else list(pairs)
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise TypeError("crypto pairs must contain strings")
        pair = value.strip().upper()
        if re.fullmatch(r"[A-Z0-9]+/[A-Z0-9]+", pair) is None:
            raise ValueError(f"invalid crypto pair: {value!r}")
        if pair not in seen:
            seen.add(pair)
            result.append(pair)
    return tuple(result)


def _validate_query_options(feed: str, adjustment: str, limit: int) -> None:
    if feed != "sip":
        raise ValueError("Alpaca equity requests must use the SIP feed")
    if adjustment != "raw":
        raise ValueError("Alpaca equity requests must use raw adjustment")
    _validate_limit(limit)


def _validate_limit(limit: int) -> None:
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= 10_000
    ):
        raise ValueError("limit must be an integer from 1 through 10000")


def _validate_option_query_options(feed: str, limit: int) -> None:
    if feed != "opra":
        raise ValueError("Alpaca option requests must use the OPRA feed")
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= 10_000
    ):
        raise ValueError("limit must be an integer from 1 through 10000")


def _normalize_contracts(
    contracts: Iterable[ContractRecord],
) -> tuple[ContractRecord, ...]:
    result: list[ContractRecord] = []
    seen: set[str] = set()
    for contract in contracts:
        if not isinstance(contract, ContractRecord):
            raise TypeError("contracts must contain ContractRecord values")
        if contract.symbol not in seen:
            seen.add(contract.symbol)
            result.append(contract)
    return tuple(result)


def _format_bound(value: date | datetime | str) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("datetime bounds must be timezone-aware")
        normalized = value.astimezone(UTC)
    elif isinstance(value, date):
        normalized = datetime.combine(value, time.min, tzinfo=UTC)
    elif isinstance(value, str):
        return value
    else:
        raise TypeError("bounds must be dates, datetimes, or provider strings")

    if normalized.microsecond:
        return normalized.isoformat().replace("+00:00", "Z")
    return normalized.strftime("%Y-%m-%dT%H:%M:%SZ")


def _retry_after_seconds(headers: Mapping[str, str], now: datetime) -> float | None:
    value: str | None = None
    for key, header_value in headers.items():
        if key.lower() == "retry-after":
            value = header_value
            break
    if value is None:
        return None

    try:
        seconds = float(value.strip())
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        seconds = (retry_at.astimezone(UTC) - now.astimezone(UTC)).total_seconds()

    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


def _page_rows(
    payload: object,
    data_type: Literal["trades", "quotes"],
    symbol: str,
) -> tuple[list[Mapping[str, object]], str | None]:
    if not isinstance(payload, Mapping):
        raise AlpacaProtocolError("Alpaca response body must be an object")
    if data_type not in payload:
        raise AlpacaProtocolError(f"Alpaca response is missing {data_type!r}")

    raw_rows = payload.get(data_type)
    if isinstance(raw_rows, Mapping):
        symbol_rows = raw_rows.get(symbol, [])
    else:
        symbol_rows = raw_rows
    if symbol_rows is None:
        symbol_rows = []
    if not isinstance(symbol_rows, Sequence) or isinstance(symbol_rows, str):
        raise AlpacaProtocolError(f"Alpaca {data_type} rows must be an array")

    rows: list[Mapping[str, object]] = []
    for row in symbol_rows:
        if not isinstance(row, Mapping):
            raise AlpacaProtocolError(f"Alpaca {data_type} row must be an object")
        rows.append(row)

    raw_token = payload.get("next_page_token")
    if raw_token is not None and not isinstance(raw_token, str):
        raise AlpacaProtocolError("Alpaca next_page_token must be a string or null")
    return rows, raw_token


def _option_page_rows(
    payload: object,
    data_type: Literal["trades", "quotes"],
    contracts: Mapping[str, ContractRecord],
) -> tuple[list[tuple[ContractRecord, Mapping[str, object]]], str | None]:
    if not isinstance(payload, Mapping):
        raise AlpacaProtocolError("Alpaca option response body must be an object")
    if data_type not in payload:
        raise AlpacaProtocolError(f"Alpaca option response is missing {data_type!r}")

    raw_rows = payload.get(data_type)
    if not isinstance(raw_rows, Mapping):
        raise AlpacaProtocolError(f"Alpaca option {data_type} rows must be an object")

    rows_by_symbol: dict[str, list[Mapping[str, object]]] = {}
    for raw_symbol, symbol_rows in raw_rows.items():
        if not isinstance(raw_symbol, str):
            raise AlpacaProtocolError("Alpaca option response symbols must be strings")
        symbol = raw_symbol.strip().upper()
        contract = contracts.get(symbol)
        if contract is None:
            raise AlpacaProtocolError(
                f"Alpaca option response contains unselected symbol {raw_symbol!r}"
            )
        if not isinstance(symbol_rows, Sequence) or isinstance(symbol_rows, str):
            raise AlpacaProtocolError(
                f"Alpaca option {data_type} rows for {raw_symbol!r} must be an array"
            )
        rows: list[Mapping[str, object]] = []
        for row in symbol_rows:
            if not isinstance(row, Mapping):
                raise AlpacaProtocolError("Alpaca option row must be an object")
            rows.append(row)
        rows_by_symbol[contract.symbol] = rows

    rows_with_contract = [
        (contract, row)
        for contract in contracts.values()
        for row in rows_by_symbol.get(contract.symbol, [])
    ]
    raw_token = payload.get("next_page_token")
    if raw_token is not None and not isinstance(raw_token, str):
        raise AlpacaProtocolError(
            "Alpaca option next_page_token must be a string or null"
        )
    return rows_with_contract, raw_token


def _required(row: Mapping[str, object], key: str) -> object:
    try:
        return row[key]
    except KeyError as error:
        raise AlpacaProtocolError(f"Alpaca row is missing {key!r}") from error


def _text(row: Mapping[str, object], key: str) -> str:
    value = _required(row, key)
    if not isinstance(value, str) or not value:
        raise AlpacaProtocolError(f"Alpaca field {key!r} must be a non-empty string")
    return value


def _integer(
    row: Mapping[str, object],
    key: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    value = _required(row, key)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise AlpacaProtocolError(f"Alpaca field {key!r} must be an integer")
    return value


def _decimal(row: Mapping[str, object], key: str) -> Decimal:
    value = _required(row, key)
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise AlpacaProtocolError(f"Alpaca field {key!r} must be numeric")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise AlpacaProtocolError(f"Alpaca field {key!r} must be numeric") from error
    if not decimal_value.is_finite():
        raise AlpacaProtocolError(f"Alpaca field {key!r} must be finite")
    return decimal_value


def _timestamp(row: Mapping[str, object]) -> datetime:
    value = _text(row, "t")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise AlpacaProtocolError("Alpaca timestamp must be ISO-8601") from error
    if timestamp.tzinfo is None:
        raise AlpacaProtocolError("Alpaca timestamp must include a timezone")
    return timestamp.astimezone(UTC)


def _conditions(row: Mapping[str, object]) -> tuple[str, ...]:
    value = _required(row, "c")
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise AlpacaProtocolError("Alpaca conditions must be an array")
    if any(not isinstance(condition, str) for condition in value):
        raise AlpacaProtocolError("Alpaca conditions must contain strings")
    return tuple(cast(str, condition) for condition in value)


def _normalize_trade(row: Mapping[str, object], symbol: str) -> NormalizedTrade:
    update = row.get("u")
    if update is not None and (not isinstance(update, str) or not update):
        raise AlpacaProtocolError("Alpaca trade update must be a non-empty string")
    return NormalizedTrade(
        timestamp=_timestamp(row),
        symbol=symbol,
        trade_id=_integer(row, "i", maximum=2**64 - 1),
        exchange=_text(row, "x"),
        price=_decimal(row, "p"),
        size=_integer(row, "s", maximum=2**32 - 1),
        conditions=_conditions(row),
        tape=_text(row, "z"),
        update=update,
    )


def _normalize_quote(row: Mapping[str, object], symbol: str) -> NormalizedQuote:
    return NormalizedQuote(
        timestamp=_timestamp(row),
        symbol=symbol,
        bid_exchange=_text(row, "bx"),
        bid_price=_decimal(row, "bp"),
        bid_size=_integer(row, "bs", maximum=2**32 - 1),
        ask_exchange=_text(row, "ax"),
        ask_price=_decimal(row, "ap"),
        ask_size=_integer(row, "as", maximum=2**32 - 1),
        conditions=_conditions(row),
        tape=_text(row, "z"),
    )


def _normalize_option_trade(
    row: Mapping[str, object], contract: ContractRecord
) -> NormalizedOptionTrade:
    return NormalizedOptionTrade(
        timestamp=_timestamp(row),
        symbol=contract.symbol,
        underlying=contract.underlying,
        expiration_date=contract.expiration_date,
        strike_price=contract.strike_price,
        option_type=contract.option_type,
        exchange=_text(row, "x"),
        price=_decimal(row, "p"),
        size=_integer(row, "s", maximum=2**32 - 1),
        condition=_text(row, "c"),
    )


def _normalize_option_quote(
    row: Mapping[str, object], contract: ContractRecord
) -> NormalizedOptionQuote:
    return NormalizedOptionQuote(
        timestamp=_timestamp(row),
        symbol=contract.symbol,
        underlying=contract.underlying,
        expiration_date=contract.expiration_date,
        strike_price=contract.strike_price,
        option_type=contract.option_type,
        bid_exchange=_text(row, "bx"),
        bid_price=_decimal(row, "bp"),
        bid_size=_integer(row, "bs", maximum=2**32 - 1),
        ask_exchange=_text(row, "ax"),
        ask_price=_decimal(row, "ap"),
        ask_size=_integer(row, "as", maximum=2**32 - 1),
        condition=_text(row, "c"),
    )


def _optional_text(row: Mapping[str, object], key: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise AlpacaProtocolError(f"Alpaca field {key!r} must be a non-empty string")
    return value


def _optional_integer(
    row: Mapping[str, object],
    key: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int | None:
    if key not in row or row[key] is None:
        return None
    return _integer(row, key, minimum=minimum, maximum=maximum)


def _normalize_crypto_trade(
    row: Mapping[str, object], symbol: str
) -> NormalizedCryptoTrade:
    return NormalizedCryptoTrade(
        timestamp=_timestamp(row),
        symbol=symbol,
        trade_id=_optional_integer(row, "i", maximum=2**63 - 1),
        exchange=_optional_text(row, "x"),
        price=_decimal(row, "p"),
        size=_decimal(row, "s"),
        taker_side=_optional_text(row, "tks"),
    )


def _normalize_crypto_quote(
    row: Mapping[str, object], symbol: str
) -> NormalizedCryptoQuote:
    return NormalizedCryptoQuote(
        timestamp=_timestamp(row),
        symbol=symbol,
        bid_exchange=_optional_text(row, "bx"),
        bid_price=_decimal(row, "bp"),
        bid_size=_decimal(row, "bs"),
        ask_exchange=_optional_text(row, "ax"),
        ask_price=_decimal(row, "ap"),
        ask_size=_decimal(row, "as"),
    )


__all__ = [
    "AlpacaClient",
    "AlpacaHTTPError",
    "AlpacaProtocolError",
    "AsyncResponse",
    "AsyncTransport",
    "CryptoQuote",
    "CryptoTrade",
    "NormalizedOptionQuote",
    "NormalizedOptionTrade",
    "NormalizedCryptoQuote",
    "NormalizedCryptoTrade",
    "NormalizedQuote",
    "NormalizedTrade",
    "OptionQuote",
    "OptionTrade",
    "Quote",
    "Trade",
]
