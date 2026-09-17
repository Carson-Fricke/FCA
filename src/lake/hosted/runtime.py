"""Hosted Azure composition for the published market-lake API.

The hosted runtime mirrors :mod:`lake.local`: it builds the Azure Table
catalog, durable job repository, identity-based SAS issuer, and bounded
lease-protected Alpaca ingest from non-secret environment configuration so the
published ``lake.app:app`` target serves real coverage and performs real
ensure work. Missing configuration selects the framework defaults instead of
attempting Azure access.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Coroutine, Mapping
from datetime import UTC, datetime
from typing import Any

from azure.data.tables import TableClient
from azure.identity import DefaultAzureCredential

from lake.catalog.azure import AzureTableCatalogRepository, AzureTableJobRepository
from lake.catalog.models import JobRecord, JobState
from lake.configuration import LakeSettings, load_settings
from lake.contracts import AssetClass
from lake.coverage import CoverageSession, equity_sessions
from lake.coverage.application import EnsureJob, EnsureSpec
from lake.http.models import EnsureRequest
from lake.ingestion.azure import AzureAtomicIngestCommitter
from lake.ingestion.equities.transaction import EquityIngestTransaction
from lake.ingestion.leased import LeasedAtomicIngestCommitter
from lake.providers.alpaca import AlpacaClient
from lake.storage.azure import AzureBlobSasIssuer, AzureBlobStore
from lake.storage.leases import AzureBlobLeaseProvider

_DEFAULT_STORAGE_CONTAINER = "lake"
_DEFAULT_COVERAGE_TABLE = "coverage"
_DEFAULT_JOBS_TABLE = "jobs"


class HostedEnsureError(RuntimeError):
    """A hosted ensure request the runtime cannot satisfy."""


def _environment_value(environ: Mapping[str, str], name: str) -> str:
    return (environ.get(name) or "").strip()


class HostedRuntime:
    """Azure-backed catalog, job, query, and ingest boundaries for hosting."""

    def __init__(
        self,
        *,
        settings: LakeSettings,
        catalog: AzureTableCatalogRepository,
        jobs: AzureTableJobRepository,
        sas_issuer: AzureBlobSasIssuer,
        blob_url: str,
        container_name: str,
        credential: DefaultAzureCredential,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.catalog = catalog
        self.jobs = jobs
        self.sas_issuer = sas_issuer
        self._blob_url = blob_url
        self._container_name = container_name
        self._credential = credential
        self._clock = clock or (lambda: datetime.now(UTC))

    @classmethod
    def from_environment(
        cls,
        *,
        environ: Mapping[str, str] | None = None,
        settings: LakeSettings | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> HostedRuntime:
        """Build the runtime from non-secret environment configuration."""
        source = os.environ if environ is None else environ
        account = _environment_value(source, "LAKE_STORAGE_ACCOUNT_NAME")
        if not account:
            raise HostedEnsureError(
                "LAKE_STORAGE_ACCOUNT_NAME is required for the hosted runtime"
            )
        resolved = settings if settings is not None else load_settings(environ=source)
        credential = DefaultAzureCredential(
            managed_identity_client_id=(
                _environment_value(source, "AZURE_CLIENT_ID") or None
            )
        )
        blob_url = f"https://{account}.blob.core.windows.net"
        table_url = f"https://{account}.table.core.windows.net"
        container_name = (
            _environment_value(source, "LAKE_STORAGE_CONTAINER")
            or _DEFAULT_STORAGE_CONTAINER
        )
        catalog = AzureTableCatalogRepository(
            TableClient(
                endpoint=table_url,
                table_name=(
                    _environment_value(source, "LAKE_COVERAGE_TABLE")
                    or _DEFAULT_COVERAGE_TABLE
                ),
                credential=credential,
            )
        )
        jobs = AzureTableJobRepository(
            TableClient(
                endpoint=table_url,
                table_name=(
                    _environment_value(source, "LAKE_JOBS_TABLE") or _DEFAULT_JOBS_TABLE
                ),
                credential=credential,
            )
        )
        sas_issuer = AzureBlobSasIssuer.from_account_url(
            blob_url,
            container_name,
            credential=credential,
            ttl_seconds=resolved.api.sas.ttl_seconds,
        )
        return cls(
            settings=resolved,
            catalog=catalog,
            jobs=jobs,
            sas_issuer=sas_issuer,
            blob_url=blob_url,
            container_name=container_name,
            credential=credential,
            clock=clock,
        )

    def ensure_callback(
        self,
    ) -> Callable[[EnsureRequest], Coroutine[Any, Any, None]]:
        """Return the file-query ensure callback used by the query route."""

        async def callback(request: EnsureRequest) -> None:
            await self.ensure_sessions(request)

        return callback

    def job_launcher(
        self,
    ) -> Callable[[EnsureJob], Coroutine[Any, Any, None]]:
        """Return the ensure-route launcher that runs bounded ingest work."""

        async def launch(job: EnsureJob) -> None:
            await self._run_job(job)

        return launch

    async def ensure_sessions(self, request: EnsureSpec | EnsureRequest) -> None:
        """Ingest every planned equity session in one bounded request range."""
        if request.asset_class is not AssetClass.EQUITY:
            raise HostedEnsureError(
                "hosted ensure supports equity sessions only, not"
                f" {request.asset_class.value}"
            )
        self._require_provider_credentials()
        sessions = equity_sessions(
            request.start_date,
            request.end_date,
            as_of=self._clock(),
        )
        await self._ingest_sessions(request.symbol.strip().upper(), sessions)

    async def _run_job(self, job: EnsureJob) -> None:
        await self.ensure_sessions(job.request)
        record = self.jobs.get(job.job_id)
        self.jobs.upsert(
            JobRecord(
                job_id=job.job_id,
                state=JobState.SUCCEEDED,
                created_at=(record.created_at if record is not None else self._clock()),
                updated_at=self._clock(),
            )
        )

    def _require_provider_credentials(self) -> None:
        if (
            self.settings.provider.api_key is None
            or self.settings.provider.api_secret is None
        ):
            raise HostedEnsureError(
                "LAKE_PROVIDER__API_KEY and LAKE_PROVIDER__API_SECRET are"
                " required for hosted ingest"
            )

    async def _ingest_sessions(
        self,
        symbol: str,
        sessions: tuple[CoverageSession, ...],
    ) -> None:
        if not sessions:
            return
        client = AlpacaClient.from_settings(self.settings.provider)
        try:
            transaction = EquityIngestTransaction(
                client=client,
                committer=self._committer(),
            )
            for session in sessions:
                await transaction.run(symbol, session)
        finally:
            await client.aclose()

    def _committer(self) -> LeasedAtomicIngestCommitter:
        blob_store = AzureBlobStore.from_account_url(
            self._blob_url,
            self._container_name,
            credential=self._credential,
        )
        leases = AzureBlobLeaseProvider.from_account_url(
            self._blob_url,
            self._container_name,
            credential=self._credential,
        )
        return LeasedAtomicIngestCommitter(
            AzureAtomicIngestCommitter(
                object_store=blob_store,
                catalog=self.catalog,
            ),
            leases,
        )


def hosted_overrides(*, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return :func:`create_app` overrides when hosted configuration exists."""
    source = os.environ if environ is None else environ
    if not _environment_value(source, "LAKE_STORAGE_ACCOUNT_NAME"):
        return {}
    runtime = HostedRuntime.from_environment(environ=source)
    return {
        "catalog_repository": runtime.catalog,
        "job_repository": runtime.jobs,
        "sas_issuer": runtime.sas_issuer,
        "ensure_callback": runtime.ensure_callback(),
        "job_launcher": runtime.job_launcher(),
    }


__all__ = [
    "HostedEnsureError",
    "HostedRuntime",
    "hosted_overrides",
]
