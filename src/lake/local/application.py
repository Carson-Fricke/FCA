"""Local-only composition for the market-lake HTTP application."""

from fastapi import FastAPI

from lake.http.application import create_app
from lake.http.routes.ensure import EnsureJobLauncher
from lake.local.ensure import LocalEnsureLauncher, ensure_deadline_seconds
from lake.local.state import LocalCatalogRepository, LocalFileIssuer, LocalLayout


def create_local_app(
    *,
    layout: LocalLayout,
    bearer_token: str,
    job_launcher: EnsureJobLauncher | None = None,
    ensure_wait_deadline_seconds: float | None = None,
) -> FastAPI:
    """Build the API over local files and catalog state without Azure clients.

    A real on-demand ensure launcher is installed by default so a request for
    symbols the local lake has not fetched yet triggers an Alpaca backfill
    instead of the previous no-op that could never satisfy coverage.
    """
    if not bearer_token.strip():
        raise ValueError("LAKE_API__BEARER_TOKEN must not be empty")
    layout.initialize()
    launcher = job_launcher if job_launcher is not None else LocalEnsureLauncher(layout)
    deadline = (
        ensure_deadline_seconds()
        if ensure_wait_deadline_seconds is None
        else ensure_wait_deadline_seconds
    )
    return create_app(
        bearer_token=bearer_token,
        catalog_repository=LocalCatalogRepository(layout.catalog),
        sas_issuer=LocalFileIssuer(layout.objects),
        query_file_root=layout.objects,
        job_launcher=launcher,
        ensure_wait_deadline_seconds=deadline,
    )


__all__ = ["create_local_app"]
