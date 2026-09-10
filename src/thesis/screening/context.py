"""Concrete point-in-time screener context over the lake and harness.

:class:`LakeScreenerContext` binds the engine's
:class:`~thesis.screening.engine.ScreenerContext` protocol to the
repository's point-in-time surfaces:

- universe membership from an injected
  :class:`~harness.data.universe_view.UniverseView` (equity listings under
  the security master's point-in-time rules, clipped to the injected engine
  clock head);
- listing lifecycle from :class:`~harness.data.security_master.SecurityMasterView`
  (rename-aware earliest listing date for the listing-age metric);
- daily OHLCV windows through an injected :class:`~thesis.screening.indicators.OhlcvPort`;
- classification facts through an injected classification port, backed in
  production by :class:`LakeClassificationFacts` over
  :func:`lake.reference.query.classification_snapshot`;
- fundamentals facts through an injected fundamentals port, backed in
  production by :class:`LakeFundamentalsFacts` over
  :func:`lake.reference.query.latest_fundamentals` with filed-date
  point-in-time semantics (a restatement filed later never leaks into an
  earlier view);
- technical indicator values computed through the harness indicator registry
  over the OHLCV window at the as-of instant (filters and scripts never
  hand-roll the math).

Like the engine, the context performs no I/O beyond the injected ports, reads
no wall clock, and draws no randomness; an as-of instant newer than the clock
head fails closed with :class:`~harness.data.history.LookaheadError`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from os import PathLike
from typing import Final, Protocol

from harness.core.clock import Clock
from harness.core.symbols import canonical_symbol
from harness.data.security_master import SecurityMasterView
from harness.data.universe_view import UniverseView
from thesis.screening.engine import MarketBar, ScreenerError, UniverseEntry
from thesis.screening.indicators import (
    OhlcvPort,
    clip_bars,
    compute_indicator_value,
    indicator_fetch_start,
)

_MIDNIGHT: Final = time(0, 0)


class ClassificationPort(Protocol):
    """Point-in-time classification facts, keyed by canonical symbol."""

    def facts(self, as_of: date) -> Mapping[str, Mapping[str, str]]:
        """Return the classification fact sheet known on ``as_of``."""
        ...


class FundamentalsPort(Protocol):
    """Filed-date point-in-time fundamentals for one SEC CIK."""

    def facts(self, cik: int, as_of: date) -> Mapping[str, str]:
        """Return the fundamentals fact sheet visible on ``as_of``."""
        ...


class CikIndex(Protocol):
    """Resolves a trading symbol to its SEC CIK as known on a date."""

    def cik_for(self, symbol: str, as_of: date) -> int | None:
        """Return the CIK filing for ``symbol``, or ``None`` when unmapped."""
        ...


@dataclass(frozen=True, slots=True)
class StaticCikIndex:
    """A workspace-supplied symbol-to-CIK mapping (order-free, deterministic)."""

    mapping: Mapping[str, int]

    def cik_for(self, symbol: str, as_of: date) -> int | None:
        del as_of
        try:
            return self.mapping.get(canonical_symbol(symbol))
        except (TypeError, ValueError):
            return None


def _require_date(as_of: date) -> None:
    if isinstance(as_of, datetime) or not isinstance(as_of, date):
        raise TypeError("as_of must be a date, not a datetime")


def _plain_decimal(value: object) -> str:
    """Return one decimal in plain notation without exponent artifacts."""
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return str(value)


def _as_of_moment(as_of: date) -> datetime:
    """Return the UTC instant the screener resolves an as-of date at."""
    return datetime.combine(as_of, _MIDNIGHT, tzinfo=UTC)


class LakeScreenerContext:
    """The production :class:`ScreenerContext` over lake and harness views."""

    def __init__(
        self,
        *,
        clock: Clock,
        universe_view: UniverseView,
        security_master: SecurityMasterView,
        ohlcv: OhlcvPort,
        classification: ClassificationPort,
        fundamentals: FundamentalsPort,
        cik_index: CikIndex,
        history_days: int = 60,
    ) -> None:
        if not isinstance(universe_view, UniverseView):
            raise TypeError("universe_view must be a UniverseView")
        if not isinstance(security_master, SecurityMasterView):
            raise TypeError("security_master must be a SecurityMasterView")
        if history_days < 1:
            raise ValueError("history_days must be a positive integer")
        self._clock = clock
        self._universe_view = universe_view
        self._security_master = security_master
        self._ohlcv = ohlcv
        self._classification = classification
        self._fundamentals = fundamentals
        self._cik_index = cik_index
        self._history_days = history_days

    # -- engine ports -----------------------------------------------------

    def revision(self, as_of: date) -> str:
        """Return the deterministic universe revision identifier."""
        _require_date(as_of)
        return f"universe:{as_of.isoformat()}"

    def universe(self, as_of: date) -> tuple[UniverseEntry, ...]:
        """Return the point-in-time equity universe, one entry per listing."""
        _require_date(as_of)
        snapshot = self._universe_view.resolve(_as_of_moment(as_of))
        entries: list[UniverseEntry] = []
        for member in snapshot.members:
            listing = member.listing
            if listing is None:
                continue
            entries.append(
                UniverseEntry(
                    symbol=member.symbol,
                    listing_date=self._listing_date(member.symbol, as_of, listing),
                )
            )
        return tuple(sorted(entries, key=lambda entry: entry.symbol))

    def market_data(self, symbol: str, window_end: date) -> tuple[MarketBar, ...]:
        """Return the trailing daily bars the engine screens over."""
        _require_date(window_end)
        start = date.fromordinal(window_end.toordinal() - (self._history_days - 1))
        bars = self._ohlcv.daily_bars(canonical_symbol(symbol), start, window_end)
        visible = clip_bars(bars, window_start=start, window_end=window_end)
        return tuple(
            MarketBar(
                bar_date=bar.bar_date,
                volume=int(bar.volume),
                close=str(bar.close),
                dollar_volume=str(bar.dollar_volume),
            )
            for bar in visible
        )

    def classification(self, symbol: str, as_of: date) -> Mapping[str, str]:
        """Return the point-in-time classification facts for one symbol."""
        _require_date(as_of)
        facts = self._classification.facts(as_of)
        return facts.get(canonical_symbol(symbol), {})

    def fundamentals(self, symbol: str, as_of: date) -> Mapping[str, str]:
        """Return the filed-date point-in-time fundamentals for one symbol."""
        _require_date(as_of)
        cik = self._cik_index.cik_for(symbol, as_of)
        if cik is None:
            return {}
        return dict(self._fundamentals.facts(cik, as_of))

    def indicator_value(
        self,
        name: str,
        params: Mapping[str, object],
        symbol: str,
        *,
        window_start: date,
        window_end: date,
    ) -> object:
        """Compute one registry indicator's value at the as-of instant."""
        _require_date(window_end)
        if window_start > window_end:
            raise ScreenerError("indicator window start must not exceed its end")
        fetch_start = indicator_fetch_start(name, params, window_start=window_start)
        bars = self._ohlcv.daily_bars(canonical_symbol(symbol), fetch_start, window_end)
        return compute_indicator_value(
            name,
            dict(params),
            symbol,
            bars,
            window_start=fetch_start,
            window_end=window_end,
        )

    def _listing_date(self, symbol: str, as_of: date, fallback: object) -> date:
        """Return the entity's earliest known listing date on ``as_of``.

        The security master resolves the entity's full rename chain, so a
        renamed entity reports the age of the entity rather than the age of
        its current symbol; a symbol the master does not know falls back to
        the universe member's own listing record.
        """
        snapshot = self._security_master.resolve(symbol, _as_of_moment(as_of))
        if snapshot is not None:
            return snapshot.listed_date
        listed = getattr(fallback, "listed_date", None)
        if isinstance(listed, date) and not isinstance(listed, datetime):
            return listed
        raise ScreenerError(f"{symbol}: no listing date known on the as-of date")


class LakeClassificationFacts:
    """Classification fact sheets over ``lake.reference`` snapshot objects."""

    def __init__(self, root: str | PathLike[str]) -> None:
        self._root = root
        self._cache: dict[date, dict[str, dict[str, str]]] = {}

    def facts(self, as_of: date) -> Mapping[str, Mapping[str, str]]:
        _require_date(as_of)
        cached = self._cache.get(as_of)
        if cached is not None:
            return cached
        from lake.reference.query import classification_snapshot

        sheet: dict[str, dict[str, str]] = {}
        for row in classification_snapshot(self._root, as_of):
            symbol = str(row.get("symbol", "")).strip().upper()
            if not symbol:
                continue
            facts: dict[str, str] = {
                "security_type": "etf" if bool(row.get("etf")) else "stock",
                "exchange": str(row.get("listing_exchange", "")).strip().upper(),
                "security_name": str(row.get("security_name", "")).strip(),
                "market_category": str(row.get("market_category", "")).strip(),
                "round_lot_size": str(row.get("round_lot_size", "")),
            }
            # SIC fields ride along when a reference snapshot carries them;
            # the current classification schema has no SIC columns, so they
            # stay absent until the reference ingest adds them.
            for source, target in (
                ("sic", "sic_code"),
                ("sic_industry", "sic_industry"),
            ):
                value = row.get(source)
                if value is not None:
                    facts[target] = str(value).strip()
            sheet[symbol] = facts
        self._cache[as_of] = sheet
        return sheet


DEFAULT_FUNDAMENTAL_TAGS: Final[Mapping[str, tuple[str, tuple[str, ...], str]]] = {
    "shares_outstanding": ("dei", ("EntityCommonStockSharesOutstanding",), "shares"),
    "revenue": (
        "us-gaap",
        ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        "USD",
    ),
    "net_income": ("us-gaap", ("NetIncomeLoss",), "USD"),
    "eps": ("us-gaap", ("EarningsPerShareDiluted",), "USD/shares"),
}


class LakeFundamentalsFacts:
    """Filed-date point-in-time fundamentals mapped to screener fields.

    Each screener field resolves from the latest visible XBRL fact among the
    field's candidate tags; among visible candidates the greatest
    ``(filed, accn, period_end)`` wins, mirroring the lake's restatement
    rule.  Per-field filing dates ride along as ``<field>_filed`` facts.
    """

    def __init__(
        self,
        root: str | PathLike[str],
        *,
        field_specs: Mapping[str, tuple[str, tuple[str, ...], str]] | None = None,
    ) -> None:
        self._root = root
        self._specs = dict(field_specs or DEFAULT_FUNDAMENTAL_TAGS)
        self._cache: dict[tuple[int, date], dict[str, str]] = {}

    def facts(self, cik: int, as_of: date) -> Mapping[str, str]:
        _require_date(as_of)
        cached = self._cache.get((cik, as_of))
        if cached is not None:
            return dict(cached)
        from lake.reference.query import latest_fundamentals

        resolved: dict[str, str] = {}
        for field, (taxonomy, tags, unit) in self._specs.items():
            best: Mapping[str, object] | None = None
            best_key: tuple[date, str, date] | None = None
            for tag in tags:
                for fact in latest_fundamentals(
                    self._root, as_of, cik=cik, taxonomy=taxonomy, tag=tag, unit=unit
                ):
                    filed = fact.get("filed")
                    period_end = fact.get("period_end")
                    if (
                        not isinstance(filed, date)
                        or isinstance(filed, datetime)
                        or not isinstance(period_end, date)
                        or isinstance(period_end, datetime)
                    ):
                        continue
                    key = (filed, str(fact.get("accn")), period_end)
                    if best_key is None or key > best_key:
                        best_key = key
                        best = fact
            if best is None:
                continue
            resolved[field] = _plain_decimal(best["val"])
            assert best_key is not None
            resolved[f"{field}_filed"] = best_key[0].isoformat()
        self._cache[(cik, as_of)] = resolved
        return dict(resolved)


__all__ = [
    "DEFAULT_FUNDAMENTAL_TAGS",
    "CikIndex",
    "ClassificationPort",
    "FundamentalsPort",
    "LakeClassificationFacts",
    "LakeFundamentalsFacts",
    "LakeScreenerContext",
    "StaticCikIndex",
]
