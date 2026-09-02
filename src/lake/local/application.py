"""Local-only composition for the market-lake HTTP application."""

from fastapi import FastAPI

from lake.http.application import create_app
from lake.local.state import LocalCatalogRepository, LocalFileIssuer, LocalLayout


def create_local_app(*, layout: LocalLayout, bearer_token: str) -> FastAPI:
    """Build the API over local files and catalog state without Azure clients."""
    if not bearer_token.strip():
        raise ValueError("LAKE_API__BEARER_TOKEN must not be empty")
    layout.initialize()
    return create_app(
        bearer_token=bearer_token,
        catalog_repository=LocalCatalogRepository(layout.catalog),
        sas_issuer=LocalFileIssuer(layout.objects),
        query_file_root=layout.objects,
    )


__all__ = ["create_local_app"]
