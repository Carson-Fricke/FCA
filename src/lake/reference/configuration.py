"""Typed settings for point-in-time reference dataset ingestion.

Settings follow the market-lake pattern: strict non-secret YAML defaults in
``config/lake/reference.yaml`` with ``LAKE_REFERENCE__`` environment overrides
taking precedence. The SEC fair-access policy caps requests per second; the
User-Agent must follow the SEC ``Sample Company Name AdminContact@domain.com``
format and is not a secret.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from lake.configuration.loader import load_yaml_mapping, validate_yaml_settings

_POSITIVE = Annotated[int, Field(gt=0)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ReferenceEnvironmentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        case_sensitive=False,
        env_ignore_empty=False,
        env_nested_delimiter="__",
        env_prefix="LAKE_REFERENCE__",
        extra="forbid",
    )


class ReferenceRetrySettings(_StrictModel):
    max_attempts: Annotated[int, Field(ge=1, le=20)]
    honor_retry_after: bool
    backoff_seconds: Annotated[float, Field(ge=0)]
    max_backoff_seconds: Annotated[float, Field(gt=0)]


class ReferenceHttpSettings(_StrictModel):
    # SEC fair-access policy: no more than 10 requests per second.
    requests_per_second: Annotated[float, Field(gt=0, le=10)]
    timeout_seconds: Annotated[int, Field(gt=0, le=3600)]
    retry: ReferenceRetrySettings


class ReferenceSettings(_ReferenceEnvironmentSettings):
    """Complete typed representation of ``config/lake/reference.yaml``."""

    schema_version: Literal[1]
    user_agent: str
    classification_url: str
    company_facts_url_template: str
    company_concept_url_template: str
    bulk_company_facts_url: str
    http: ReferenceHttpSettings

    @field_validator("user_agent")
    @classmethod
    def _require_descriptive_user_agent(cls, value: str) -> str:
        if not value.strip() or "@" not in value:
            raise ValueError(
                "reference.user_agent must follow the SEC format "
                "'Sample Company Name AdminContact@domain.com'"
            )
        return value

    @field_validator("company_facts_url_template")
    @classmethod
    def _require_cik_placeholder(cls, value: str) -> str:
        if "{cik" not in value:
            raise ValueError(
                "company_facts_url_template must contain the {cik} placeholder"
            )
        cls._check_template(value, ("cik",))
        return value

    @field_validator("company_concept_url_template")
    @classmethod
    def _require_concept_placeholders(cls, value: str) -> str:
        for placeholder in ("cik", "taxonomy", "tag"):
            if f"{{{placeholder}" not in value:
                raise ValueError(
                    "company_concept_url_template must contain the "
                    f"{{{placeholder}}} placeholder"
                )
        cls._check_template(value, ("cik", "taxonomy", "tag"))
        return value

    @staticmethod
    def _check_template(template: str, placeholders: tuple[str, ...]) -> None:
        values = dict.fromkeys(placeholders, "x")
        try:
            template.format(**values)
        except (KeyError, IndexError, ValueError) as error:
            raise ValueError(f"invalid URL template: {template!r}") from error


def reference_config_dir(root: str | Path | None = None) -> Path:
    """Resolve the reference config directory without machine-specific paths."""
    if root is None:
        configured_root = Path(__file__).resolve().parents[3]
        root_path = configured_root
    else:
        root_path = Path(root)
    if root_path.name == "lake" and root_path.parent.name == "config":
        return root_path
    return root_path / "config" / "lake"


def load_reference_settings(
    root: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> ReferenceSettings:
    """Load and validate reference settings with environment precedence."""
    path = reference_config_dir(root) / "reference.yaml"
    defaults = load_yaml_mapping(path)
    return validate_yaml_settings(ReferenceSettings, defaults, environ)


__all__ = [
    "ReferenceHttpSettings",
    "ReferenceRetrySettings",
    "ReferenceSettings",
    "load_reference_settings",
    "reference_config_dir",
]
