"""Focused tests for the readable Azure blob adapter."""

from __future__ import annotations

from typing import Any, cast

import pytest
from azure.core.exceptions import ResourceNotFoundError

import lake.storage.azure as storage_module
from lake.storage import AzureBlobStore, AzureReadableBlobStore, ReadableBlobStore


class FakeCredential:
    pass


class FakeBlob:
    def __init__(self, container: FakeContainer, path: str) -> None:
        self._container = container
        self.path = path
        self.uploads: list[tuple[bytes, bool]] = []

    def upload_blob(self, payload: bytes, *, overwrite: bool) -> None:
        self.uploads.append((payload, overwrite))
        self._container.payloads[self.path] = payload


class FakeDownloader:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def readall(self) -> bytes:
        return self._payload


class FakeBlobProperties:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeContainer:
    def __init__(self) -> None:
        self.payloads: dict[str, bytes] = {}
        self.blobs: dict[str, FakeBlob] = {}
        self.listings: list[str | None] = []
        self.deletions: list[str] = []

    def get_blob_client(self, path: str) -> FakeBlob:
        return self.blobs.setdefault(path, FakeBlob(self, path))

    def download_blob(self, path: str) -> FakeDownloader:
        try:
            return FakeDownloader(self.payloads[path])
        except KeyError:
            raise ResourceNotFoundError("missing") from None

    def list_blobs(self, *, name_starts_with: str | None = None) -> list:
        self.listings.append(name_starts_with)
        return [
            FakeBlobProperties(path)
            for path in sorted(self.payloads)
            if name_starts_with is None or path.startswith(name_starts_with)
        ]

    def delete_blob(self, blob: str) -> None:
        self.deletions.append(blob)
        self.payloads.pop(blob, None)


class FakeBlobService:
    account_name = "lakeaccount"
    url = "https://lakeaccount.blob.core.windows.net/"

    def get_container_client(self, name: str) -> FakeContainer:
        assert name == "lake"
        return FakeContainer()


def test_readable_blob_store_replaces_reads_lists_and_deletes_prefixes() -> None:
    container = FakeContainer()
    adapter = AzureReadableBlobStore(cast(Any, container))

    adapter.replace("backtests/run-1/actions.parquet", b"actions")
    adapter.replace("backtests/run-1/fills.parquet", b"fills")
    adapter.replace("backtests/run-2/fills.parquet", b"other")

    assert adapter.read("backtests/run-1/actions.parquet") == b"actions"
    assert adapter.list_prefix("backtests/run-1/") == (
        "backtests/run-1/actions.parquet",
        "backtests/run-1/fills.parquet",
    )
    assert adapter.delete_prefix("backtests/run-1/") == 2
    assert adapter.list_prefix("backtests/run-1/") == ()
    assert adapter.list_prefix("backtests/") == ("backtests/run-2/fills.parquet",)
    assert container.listings == [
        "backtests/run-1/",
        "backtests/run-1/",
        "backtests/run-1/",
        "backtests/",
    ]
    assert container.deletions == [
        "backtests/run-1/actions.parquet",
        "backtests/run-1/fills.parquet",
    ]
    assert isinstance(adapter, AzureBlobStore)
    assert isinstance(adapter, ReadableBlobStore)


def test_readable_blob_store_missing_reads_raise_key_error() -> None:
    adapter = AzureReadableBlobStore(cast(Any, FakeContainer()))

    with pytest.raises(KeyError, match="object not found"):
        adapter.read("backtests/run-1/fills.parquet")


def test_readable_blob_store_rejects_paths_that_are_not_relative_objects() -> None:
    adapter = AzureReadableBlobStore(cast(Any, FakeContainer()))

    for path in ("", "/lake/file", "../file", "lake/../file", "lake\\file"):
        with pytest.raises(ValueError, match="relative POSIX"):
            adapter.read(path)


def test_readable_factory_passes_caller_credential_to_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = FakeCredential()
    service = FakeBlobService()
    calls: list[tuple[str, object]] = []

    class FakeBlobServiceClient:
        def __init__(self, *, account_url: str, credential: object) -> None:
            calls.append((account_url, credential))

        def get_container_client(self, name: str) -> FakeContainer:
            return service.get_container_client(name)

    monkeypatch.setattr(storage_module, "BlobServiceClient", FakeBlobServiceClient)

    adapter = AzureReadableBlobStore.from_account_url(
        "https://lakeaccount.blob.core.windows.net",
        "lake",
        credential=cast(Any, credential),
    )
    adapter.replace("file.parquet", b"payload")

    assert isinstance(adapter, AzureReadableBlobStore)
    assert adapter.read("file.parquet") == b"payload"
    assert calls == [("https://lakeaccount.blob.core.windows.net", credential)]
