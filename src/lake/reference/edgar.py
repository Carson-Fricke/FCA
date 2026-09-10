"""SEC EDGAR XBRL company facts parsing.

Facts are one archive row per XBRL fact entry: ``companyfacts`` JSON expands
to all taxonomy, tag, and unit entries for one CIK; ``companyconcept`` JSON
expands to the single taxonomy and tag it names; the nightly
``companyfacts.zip`` bundles per-CIK facts for bulk backfill. The ``filed``
date is the point-in-time knowledge key: a fact filed after an as-of instant
must stay invisible to as-of queries, including restated values of the same
XBRL tag and period.
"""

import io
import json
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final


class EdgarParseError(ValueError):
    """The payload is not a parsable SEC XBRL facts document."""


@dataclass(frozen=True, slots=True)
class XbrlFact:
    """One XBRL fact archive row, keyed by its full identity."""

    cik: int
    taxonomy: str
    tag: str
    unit: str
    period_end: date
    fy: int | None
    fp: str | None
    form: str
    accn: str
    filed: date
    frame: str | None
    val: Decimal


_FACT_SORT_KEY: Final = (
    "cik",
    "taxonomy",
    "tag",
    "unit",
    "period_end",
    "filed",
    "accn",
    "fy",
    "fp",
    "form",
    "frame",
    "val",
)


def parse_companyfacts(payload: bytes | Mapping[str, object]) -> tuple[XbrlFact, ...]:
    """Expand a companyfacts document into sorted, deduplicated facts."""
    document = _document(payload)
    cik = _cik_of(document)
    facts_by_taxonomy = _mapping_of(document, "facts", "companyfacts")
    facts: list[XbrlFact] = []
    for taxonomy, tags in facts_by_taxonomy.items():
        if not isinstance(tags, Mapping):
            raise EdgarParseError(
                f"companyfacts taxonomy {taxonomy!r} must be an object"
            )
        for tag, node in tags.items():
            facts.extend(_taxonomy_facts(cik, str(taxonomy), str(tag), node))
    return _archive_order(facts)


def parse_companyconcept(
    payload: bytes | Mapping[str, object],
) -> tuple[XbrlFact, ...]:
    """Expand a companyconcept document into sorted, deduplicated facts."""
    document = _document(payload)
    cik = _cik_of(document)
    taxonomy = _text_of(document, "taxonomy", "companyconcept")
    tag = _text_of(document, "tag", "companyconcept")
    facts: list[XbrlFact] = []
    for unit_name, node in _mapping_of(document, "units", "companyconcept").items():
        facts.extend(_unit_facts(cik, taxonomy, tag, str(unit_name), node))
    return _archive_order(facts)


def read_companyfacts_zip(
    payload: bytes,
) -> tuple[tuple[int, tuple[XbrlFact, ...]], ...]:
    """Read a companyfacts zip into per-CIK fact bundles, sorted by CIK.

    Entries that are not per-CIK facts documents (for example the bundled
    ``company_tickers.json``) are skipped rather than failing the archive.
    """
    bundles: list[tuple[int, tuple[XbrlFact, ...]]] = []
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for name in sorted(archive.namelist()):
            if not name.upper().endswith(".JSON"):
                continue
            document = _document(archive.read(name))
            if "facts" not in document:
                continue
            cik = _cik_of(document)
            bundles.append((cik, parse_companyfacts(document)))
    return tuple(bundles)


def company_facts_url(template: str, cik: int) -> str:
    """Format a companyfacts request URL from the configured template."""
    return _format_template(template, cik=cik)


def company_concept_url(template: str, cik: int, taxonomy: str, tag: str) -> str:
    """Format a companyconcept request URL from the configured template."""
    return _format_template(template, cik=cik, taxonomy=taxonomy, tag=tag)


def deduplicate_facts(facts: tuple[XbrlFact, ...]) -> tuple[XbrlFact, ...]:
    """Collapse exact-duplicate rows while preserving archive order."""
    return _archive_order(list(facts))


def _archive_order(facts: list[XbrlFact]) -> tuple[XbrlFact, ...]:
    ordered = tuple(sorted(set(facts), key=_fact_key))
    return ordered


def _fact_key(fact: XbrlFact) -> tuple[object, ...]:
    return (
        fact.cik,
        fact.taxonomy,
        fact.tag,
        fact.unit,
        fact.period_end,
        fact.filed,
        fact.accn,
        _or_zero(fact.fy),
        fact.fp or "",
        fact.form,
        fact.frame or "",
        fact.val,
    )


def _taxonomy_facts(cik: int, taxonomy: str, tag: str, node: object) -> list[XbrlFact]:
    if not isinstance(node, Mapping):
        raise EdgarParseError(f"companyfacts tag {taxonomy}:{tag} must be an object")
    facts: list[XbrlFact] = []
    units = node.get("units")
    if not isinstance(units, Mapping):
        raise EdgarParseError(
            f"companyfacts tag {taxonomy}:{tag} is missing its units object"
        )
    for unit_name, entries in units.items():
        facts.extend(_unit_facts(cik, taxonomy, tag, str(unit_name), entries))
    return facts


def _unit_facts(
    cik: int, taxonomy: str, tag: str, unit: str, entries: object
) -> list[XbrlFact]:
    if not isinstance(entries, list):
        raise EdgarParseError(
            f"companyfacts unit {taxonomy}:{tag} {unit!r} must be an array"
        )
    facts: list[XbrlFact] = []
    for entry_number, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise EdgarParseError(
                f"companyfacts entry {taxonomy}:{tag} {unit!r}[{entry_number}] "
                "must be an object"
            )
        facts.append(_entry_fact(cik, taxonomy, tag, unit, entry, entry_number))
    return facts


def _entry_fact(
    cik: int,
    taxonomy: str,
    tag: str,
    unit: str,
    entry: Mapping[str, object],
    entry_number: int,
) -> XbrlFact:
    where = f"{taxonomy}:{tag} {unit!r}[{entry_number}]"
    return XbrlFact(
        cik=cik,
        taxonomy=taxonomy,
        tag=tag,
        unit=unit,
        period_end=_date_of(entry, "end", where),
        fy=_optional_integer(entry, "fy", where),
        fp=_optional_text(entry, "fp", where),
        form=_required_text(entry, "form", where),
        accn=_required_text(entry, "accn", where),
        filed=_date_of(entry, "filed", where),
        frame=_optional_text(entry, "frame", where),
        val=_decimal_of(entry, "val", where),
    )


def _or_zero(value: int | None) -> int:
    return 0 if value is None else value


def _document(payload: bytes | Mapping[str, object]) -> dict[str, object]:
    if isinstance(payload, Mapping):
        document = payload
    elif isinstance(payload, bytes):
        document = json.loads(payload.decode("utf-8"))
    else:
        raise TypeError("payload must be bytes or a mapping")
    if not isinstance(document, dict):
        raise EdgarParseError("SEC facts payload root must be an object")
    return document


def _cik_of(document: dict[str, object]) -> int:
    value = document.get("cik")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise EdgarParseError("SEC facts payload is missing a positive integer cik")
    return value


def _mapping_of(document: dict[str, object], key: str, where: str) -> dict[str, object]:
    value = document.get(key)
    if not isinstance(value, dict):
        raise EdgarParseError(f"{where} payload is missing the {key!r} object")
    return value


def _format_template(template: str, **values: object) -> str:
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError) as error:
        raise ValueError(f"invalid SEC facts URL template: {template!r}") from error


def _required_text(entry: Mapping[str, object], key: str, where: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EdgarParseError(f"SEC entry {where} is missing {key!r}")
    return value.strip()


def _optional_text(entry: Mapping[str, object], key: str, where: str) -> str | None:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise EdgarParseError(f"SEC entry {where} has an invalid {key!r}")
    return value.strip()


def _optional_integer(entry: Mapping[str, object], key: str, where: str) -> int | None:
    value = entry.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise EdgarParseError(f"SEC entry {where} has a non-integer {key!r}")
    return value


def _decimal_of(entry: Mapping[str, object], key: str, where: str) -> Decimal:
    value = entry.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise EdgarParseError(f"SEC entry {where} has a non-numeric {key!r}")
    text = repr(value) if isinstance(value, float) else value
    try:
        decimal_value = Decimal(text)
    except (InvalidOperation, ValueError) as error:
        raise EdgarParseError(f"SEC entry {where} has a non-numeric {key!r}") from error
    if not decimal_value.is_finite():
        raise EdgarParseError(f"SEC entry {where} has a non-finite {key!r}")
    return decimal_value


def _date_of(entry: Mapping[str, object], key: str, where: str) -> date:
    value = entry.get(key)
    if not isinstance(value, str):
        raise EdgarParseError(f"SEC entry {where} is missing {key!r}")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as error:
        raise EdgarParseError(f"SEC entry {where} has an invalid {key!r}") from error


def _text_of(document: dict[str, object], key: str, where: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EdgarParseError(f"{where} payload is missing {key!r}")
    return value.strip()


__all__ = [
    "EdgarParseError",
    "XbrlFact",
    "company_concept_url",
    "company_facts_url",
    "deduplicate_facts",
    "parse_companyconcept",
    "parse_companyfacts",
    "read_companyfacts_zip",
]
