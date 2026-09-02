"""Strict request and response models for the market-lake API boundary."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lake.contracts import AssetClass, DataType


class StrictModel(BaseModel):
    """Base model that rejects unknown request fields."""

    model_config = ConfigDict(extra="forbid")


Symbol = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$",
    ),
]


class DateRangeRequest(StrictModel):
    """A closed, chronologically ordered session-date range."""

    start_date: date
    end_date: date

    @model_validator(mode="after")
    def _ordered_dates(self) -> DateRangeRequest:
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        return self


class CoverageRequest(DateRangeRequest):
    """Validated filters reserved for the catalog coverage endpoint."""

    asset_class: AssetClass
    data_type: DataType
    symbol: Symbol | None = None


class EnsureRequest(DateRangeRequest):
    """Validated input for a future coverage ensure request."""

    asset_class: AssetClass
    data_type: DataType
    symbol: Symbol


class HealthResponse(StrictModel):
    status: Literal["ok"]


class AuthCheckResponse(StrictModel):
    status: Literal["ok"]


class ErrorBody(StrictModel):
    code: str
    message: str
    details: object | None = None


class ErrorResponse(StrictModel):
    error: ErrorBody
