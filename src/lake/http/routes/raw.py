"""Authenticated raw-object metadata routes for the market-lake API."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Annotated
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field

from lake.catalog.models import CoverageRecord, CoverageStatus
from lake.catalog.ports import CatalogRepository
from lake.contracts import AssetClass, DataType, partition_path
from lake.http.models import StrictModel
from lake.http.routes.catalog import get_catalog_repository
from lake.http.routes.query import get_query_sas_issuer
from lake.storage.ports import SasIssuer

_PARTITION_PATH = re.compile(
    r"date=(?P<session_date>\d{4}-\d{2}-\d{2})/"
    r"asset_class=(?P<asset_class>[^/]+)/"
    r"data_type=(?P<data_type>[^/]+)/"
    r"(?P<symbol>[^/]+)\.parquet",
    re.ASCII,
)


class RawObjectRequest(StrictModel):
    """One canonical catalog object path requested for direct read access."""

    object_path: Annotated[str, Field(min_length=1, max_length=512)]


class RawObjectResponse(StrictModel):
    """Catalog metadata and a read-only URL for one known lake object."""

    asset_class: AssetClass
    data_type: DataType
    symbol: str
    session_date: date
    status: CoverageStatus
    object_path: str
    url: str
    sas_url: str
    row_count: int
    byte_size: int
    checksum: str | None = None


@dataclass(frozen=True, slots=True)
class _CatalogObject:
    asset_class: AssetClass
    data_type: DataType
    symbol: str
    session_date: date
    object_path: str


def create_raw_router() -> APIRouter:
    """Create the raw-object router without binding it to Azure storage."""
    router = APIRouter(prefix="/v1", tags=["raw"])

    @router.get(
        "/raw",
        response_model=RawObjectResponse,
        responses={
            404: {"description": "Object path is not authorized by the catalog"},
            503: {"description": "SAS issuance is not configured"},
        },
    )
    async def raw_object(
        request: Annotated[RawObjectRequest, Query()],
        catalog: Annotated[CatalogRepository, Depends(get_catalog_repository)],
        sas_issuer: Annotated[SasIssuer | None, Depends(get_query_sas_issuer)],
    ) -> RawObjectResponse:
        """Issue read access only for an exact, already-cataloged object path."""
        try:
            requested = _catalog_object(request.object_path)
        except (UnicodeError, ValueError) as error:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "invalid_object_path",
                    "message": "object_path must be a canonical lake partition path",
                },
            ) from error

        record = catalog.get(
            requested.asset_class,
            requested.data_type,
            requested.symbol,
            requested.session_date,
        )
        if record is None or not _authorizes(record, requested):
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "raw_object_not_found",
                    "message": "the requested object is not available in the catalog",
                },
            )
        if sas_issuer is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "sas_unavailable",
                    "message": "raw object SAS issuance is not configured",
                },
            )

        url = sas_issuer.issue(requested.object_path)
        return RawObjectResponse(
            asset_class=record.asset_class,
            data_type=record.data_type,
            symbol=record.symbol,
            session_date=record.session_date,
            status=record.status,
            object_path=requested.object_path,
            url=url,
            sas_url=url,
            row_count=record.row_count,
            byte_size=record.byte_size,
            checksum=record.checksum,
        )

    return router


def _catalog_object(object_path: str) -> _CatalogObject:
    if (
        "\\" in object_path
        or object_path.startswith("/")
        or any(part in {"", ".", ".."} for part in object_path.split("/"))
    ):
        raise ValueError("object path must be a relative POSIX path")
    match = _PARTITION_PATH.fullmatch(object_path)
    if match is None:
        raise ValueError("object path does not match the partition contract")

    session_date = date.fromisoformat(match.group("session_date"))
    asset_class = AssetClass(match.group("asset_class"))
    data_type = DataType(match.group("data_type"))
    symbol = unquote(match.group("symbol"), errors="strict")
    if len(symbol) > 64 or any(
        part in {"", ".", ".."} for part in symbol.replace("\\", "/").split("/")
    ):
        raise ValueError("object path contains an invalid symbol")

    canonical_path = partition_path(
        session_date=session_date,
        asset_class=asset_class,
        data_type=data_type,
        symbol=symbol,
    )
    if object_path != canonical_path:
        raise ValueError("object path is not canonical")
    return _CatalogObject(
        asset_class=asset_class,
        data_type=data_type,
        symbol=symbol,
        session_date=session_date,
        object_path=canonical_path,
    )


def _authorizes(record: CoverageRecord, requested: _CatalogObject) -> bool:
    return (
        record.asset_class is requested.asset_class
        and record.data_type is requested.data_type
        and record.symbol == requested.symbol
        and record.session_date == requested.session_date
        and record.object_path == requested.object_path
    )


__all__ = [
    "RawObjectRequest",
    "RawObjectResponse",
    "create_raw_router",
]
