from pathlib import Path

import pytest
from pydantic import ValidationError

from lake.reference.configuration import (
    ReferenceSettings,
    load_reference_settings,
    reference_config_dir,
)

ROOT = Path(__file__).parents[3]


def test_committed_reference_yaml_loads_with_sec_fair_access_defaults() -> None:
    settings = load_reference_settings(ROOT)

    assert settings.schema_version == 1
    assert settings.http.requests_per_second == 10
    assert settings.http.timeout_seconds == 30
    assert settings.http.retry.max_attempts == 5
    assert settings.http.retry.honor_retry_after is True
    assert settings.classification_url.endswith("nasdaqtraded.txt")
    assert settings.company_facts_url_template.endswith("CIK{cik:0>10}.json")
    assert "{taxonomy}" in settings.company_concept_url_template
    assert "{tag}" in settings.company_concept_url_template
    assert settings.bulk_company_facts_url.endswith("companyfacts.zip")
    assert "@" in settings.user_agent


def test_environment_overrides_win_without_process_globals() -> None:
    settings = load_reference_settings(
        ROOT,
        environ={
            "LAKE_REFERENCE__USER_AGENT": "Other Company contact@other.example",
            "LAKE_REFERENCE__HTTP__RETRY__MAX_ATTEMPTS": "2",
        },
    )

    assert settings.user_agent == "Other Company contact@other.example"
    assert settings.http.retry.max_attempts == 2
    assert settings.http.requests_per_second == 10


def test_committed_user_agent_is_a_non_secret_placeholder() -> None:
    settings = load_reference_settings(ROOT)

    assert "example.com" in settings.user_agent
    assert "key" not in settings.user_agent.lower()


def test_invalid_user_agent_is_rejected() -> None:
    with pytest.raises(ValidationError, match="user_agent"):
        ReferenceSettings.model_validate(_defaults(user_agent="anonymous"))


def test_templates_missing_placeholders_are_rejected() -> None:
    with pytest.raises(ValidationError, match="placeholder"):
        ReferenceSettings.model_validate(
            _defaults(company_facts_url_template="https://data.sec.gov/facts.json")
        )
    with pytest.raises(ValidationError, match="placeholder"):
        ReferenceSettings.model_validate(
            _defaults(
                company_concept_url_template=(
                    "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:0>10}.json"
                )
            )
        )
    with pytest.raises(ValidationError, match="invalid URL template"):
        ReferenceSettings.model_validate(
            _defaults(
                company_concept_url_template=(
                    "https://data.sec.gov/{cik}/{taxonomy}/{tag}/{}"
                )
            )
        )


def test_config_dir_resolution_is_repository_relative() -> None:
    assert reference_config_dir(ROOT) == ROOT / "config" / "lake"
    assert reference_config_dir(ROOT / "config" / "lake") == ROOT / "config" / "lake"


def _defaults(**overrides: str) -> dict[str, object]:
    values: dict[str, object] = {
        "schema_version": 1,
        "user_agent": "FCA market lake ingest AdminContact@example.com",
        "classification_url": "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqtraded.txt",
        "company_facts_url_template": (
            "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:0>10}.json"
        ),
        "company_concept_url_template": (
            "https://data.sec.gov/api/xbrl/companyconcept/"
            "CIK{cik:0>10}/{taxonomy}/{tag}.json"
        ),
        "bulk_company_facts_url": "https://www.sec.gov/Archives/edgar/companyfacts.zip",
        "http": {
            "requests_per_second": 10,
            "timeout_seconds": 30,
            "retry": {
                "max_attempts": 5,
                "honor_retry_after": True,
                "backoff_seconds": 0.5,
                "max_backoff_seconds": 30.0,
            },
        },
    }
    values.update(overrides)
    return values
