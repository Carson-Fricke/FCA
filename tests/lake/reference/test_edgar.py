import io
import json
import zipfile
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from lake.reference.edgar import (
    EdgarParseError,
    company_concept_url,
    company_facts_url,
    deduplicate_facts,
    parse_companyconcept,
    parse_companyfacts,
    read_companyfacts_zip,
)

FIXTURES = Path(__file__).parent / "fixtures"
COMPANY_FACTS = FIXTURES / "companyfacts.json"

_FILINGS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:0>10}.json"
_CONCEPT_URL = (
    "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:0>10}/{taxonomy}/{tag}.json"
)


def _document() -> dict[str, object]:
    return json.loads(COMPANY_FACTS.read_text(encoding="utf-8"))


def test_companyfacts_document_expands_every_taxonomy_tag_and_unit() -> None:
    facts = parse_companyfacts(COMPANY_FACTS.read_bytes())

    assert len(facts) == 4
    assert {fact.taxonomy for fact in facts} == {"dei", "us-gaap"}
    assert {fact.cik for fact in facts} == {320193}


def test_companyfacts_rows_carry_exact_identity_and_values() -> None:
    facts = parse_companyfacts(_document())

    shares = next(fact for fact in facts if fact.taxonomy == "dei")
    assert shares.tag == "EntityCommonStockSharesOutstanding"
    assert shares.unit == "shares"
    assert shares.period_end.isoformat() == "2023-10-20"
    assert shares.fy == 2023
    assert shares.fp == "FY"
    assert shares.form == "10-K"
    assert shares.accn == "0000320193-23-000106"
    assert shares.filed.isoformat() == "2023-11-03"
    assert shares.frame == "CY2023Q3I"
    assert shares.val == Decimal("15464661000")

    revenues = [fact for fact in facts if fact.tag == "Revenues"]
    assert [fact.filed.isoformat() for fact in revenues] == [
        "2024-02-02",
        "2024-05-03",
    ]
    assert [fact.val for fact in revenues] == [
        Decimal("119575000000"),
        Decimal("119999000000"),
    ]
    assert [fact.frame for fact in revenues] == ["CY2023Q4", None]


def test_restatement_rows_share_the_tag_and_period_but_not_the_filing() -> None:
    facts = [fact for fact in parse_companyfacts(_document()) if fact.tag == "Revenues"]

    original, restatement = facts
    assert original.period_end == restatement.period_end
    assert original.filed < restatement.filed
    assert original.accn != restatement.accn
    assert original.val != restatement.val


def test_mapping_payloads_are_accepted_alongside_bytes() -> None:
    assert parse_companyfacts(_document()) == parse_companyfacts(
        COMPANY_FACTS.read_bytes()
    )


def test_duplicate_rows_collapse_while_archive_order_is_preserved() -> None:
    facts = parse_companyfacts(_document())

    duplicated = deduplicate_facts(facts + facts)

    assert duplicated == facts


def test_companyconcept_document_expands_to_its_single_taxonomy_and_tag() -> None:
    document = {
        "cik": 320193,
        "taxonomy": "us-gaap",
        "tag": "Revenues",
        "label": "Revenue",
        "units": {
            "USD": [
                {
                    "end": "2023-12-30",
                    "val": "119575000000",
                    "accn": "0000320193-24-000006",
                    "fy": 2024,
                    "fp": "Q1",
                    "form": "10-Q",
                    "filed": "2024-02-02",
                }
            ]
        },
    }

    facts = parse_companyconcept(document)

    assert len(facts) == 1
    fact = facts[0]
    assert (fact.cik, fact.taxonomy, fact.tag, fact.unit) == (
        320193,
        "us-gaap",
        "Revenues",
        "USD",
    )
    assert fact.val == Decimal("119575000000")
    assert fact.frame is None


def test_companyfacts_zip_yields_sorted_per_cik_bundles_and_skips_extras() -> None:
    cik_a = {**_document(), "cik": 320193}
    cik_b = {**_document(), "cik": 789019}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("companyfacts/CIK0000789019.json", json.dumps(cik_b))
        archive.writestr("company_tickers.json", json.dumps({"0": {"ticker": "AAPL"}}))
        archive.writestr("companyfacts/CIK0000320193.json", json.dumps(cik_a))
        archive.writestr("companyfacts/readme.txt", "not json")

    bundles = read_companyfacts_zip(buffer.getvalue())

    assert [cik for cik, _ in bundles] == [320193, 789019]
    assert all(fact.cik == cik for cik, facts in bundles for fact in facts)
    assert sum(len(facts) for _, facts in bundles) == 8


def test_url_templates_format_the_pinned_sec_shapes() -> None:
    assert company_facts_url(_FILINGS_URL, 320193) == (
        "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
    )
    assert company_concept_url(_CONCEPT_URL, 320193, "us-gaap", "Revenues") == (
        "https://data.sec.gov/api/xbrl/companyconcept/CIK0000320193/us-gaap/"
        "Revenues.json"
    )


def test_invalid_url_templates_are_rejected() -> None:
    for template in ("https://example.test/{unknown}", "https://example.test/{}"):
        with pytest.raises(ValueError, match="invalid SEC facts URL template"):
            company_facts_url(template, 320193)


def test_missing_or_invalid_cik_is_rejected() -> None:
    for document in ({}, {"cik": "320193"}, {"cik": 0}, {"cik": True}, {"cik": -1}):
        with pytest.raises(EdgarParseError, match="cik"):
            parse_companyfacts(document)


def test_document_with_missing_facts_or_bad_shape_is_rejected() -> None:
    with pytest.raises(EdgarParseError, match="missing the 'facts' object"):
        parse_companyfacts({"cik": 320193})
    with pytest.raises(EdgarParseError, match="must be an object"):
        parse_companyfacts({"cik": 320193, "facts": {"us-gaap": "nope"}})
    with pytest.raises(EdgarParseError, match="missing its units object"):
        parse_companyfacts({"cik": 320193, "facts": {"us-gaap": {"Assets": {}}}})
    with pytest.raises(EdgarParseError, match="must be an array"):
        parse_companyfacts(
            {"cik": 320193, "facts": {"us-gaap": {"Assets": {"units": {"USD": {}}}}}}
        )
    with pytest.raises(EdgarParseError, match="must be an object"):
        parse_companyfacts(
            {
                "cik": 320193,
                "facts": {"us-gaap": {"Assets": {"units": {"USD": ["nope"]}}}},
            }
        )
    with pytest.raises(EdgarParseError, match="root must be an object"):
        parse_companyfacts(b"[1, 2, 3]")
    with pytest.raises(TypeError, match="bytes or a mapping"):
        parse_companyfacts(cast("bytes | dict[str, object]", 320193))


def test_entry_field_validation_is_rejected_with_context() -> None:
    entry = {
        "end": "2023-12-30",
        "val": 1,
        "accn": "0000320193-24-000006",
        "form": "10-Q",
        "filed": "2024-02-02",
    }
    with pytest.raises(EdgarParseError, match="is missing 'end'"):
        parse_companyconcept(
            {
                "cik": 320193,
                "taxonomy": "us-gaap",
                "tag": "Assets",
                "units": {
                    "USD": [
                        {key: value for key, value in entry.items() if key != "end"}
                    ]
                },
            }
        )
    with pytest.raises(EdgarParseError, match="is missing 'form'"):
        parse_companyconcept(
            {
                "cik": 320193,
                "taxonomy": "us-gaap",
                "tag": "Assets",
                "units": {
                    "USD": [
                        {key: value for key, value in entry.items() if key != "form"}
                    ]
                },
            }
        )
    with pytest.raises(EdgarParseError, match="is missing 'accn'"):
        parse_companyconcept(
            {
                "cik": 320193,
                "taxonomy": "us-gaap",
                "tag": "Assets",
                "units": {
                    "USD": [
                        {key: value for key, value in entry.items() if key != "accn"}
                    ]
                },
            }
        )
    with pytest.raises(EdgarParseError, match="invalid 'filed'"):
        parse_companyconcept(
            {
                "cik": 320193,
                "taxonomy": "us-gaap",
                "tag": "Assets",
                "units": {"USD": [{**entry, "filed": "not-a-date"}]},
            }
        )
    with pytest.raises(EdgarParseError, match="non-numeric 'val'"):
        parse_companyconcept(
            {
                "cik": 320193,
                "taxonomy": "us-gaap",
                "tag": "Assets",
                "units": {"USD": [{**entry, "val": "many"}]},
            }
        )
    with pytest.raises(EdgarParseError, match="non-finite 'val'"):
        parse_companyconcept(
            {
                "cik": 320193,
                "taxonomy": "us-gaap",
                "tag": "Assets",
                "units": {"USD": [{**entry, "val": "NaN"}]},
            }
        )
    with pytest.raises(EdgarParseError, match="non-integer 'fy'"):
        parse_companyconcept(
            {
                "cik": 320193,
                "taxonomy": "us-gaap",
                "tag": "Assets",
                "units": {"USD": [{**entry, "fy": "2024"}]},
            }
        )


def test_float_values_keep_exact_decimal_text() -> None:
    facts = parse_companyconcept(
        {
            "cik": 320193,
            "taxonomy": "us-gaap",
            "tag": "WeightedAverageNumberOfSharesOutstandingBasic",
            "units": {"shares": [{**_entry(), "val": 0.1}]},
        }
    )

    assert facts[0].val == Decimal("0.1")


def _entry() -> dict[str, object]:
    return {
        "end": "2023-12-30",
        "val": 100,
        "accn": "0000320193-24-000006",
        "fy": 2024,
        "fp": "Q1",
        "form": "10-Q",
        "filed": "2024-02-02",
    }
