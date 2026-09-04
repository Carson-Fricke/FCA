"""Authenticated HTTP transport for the market-lake API."""

from __future__ import annotations

import time
from collections.abc import Generator
from typing import cast

import httpx
from azure.core.credentials import AccessToken, TokenCredential

from lakeclient.models import (
    CoverageResponse,
    DatasetRequest,
    EnsureAccepted,
    FileQueryResponse,
    JobStatus,
    parse_ensure_response,
    parse_file_query_response,
    parse_job_status,
)


class LakeClientError(RuntimeError):
    """An error response returned by the market-lake API."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: object | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.details = details
        super().__init__(message)


class LakeProtocolError(RuntimeError):
    """A response that does not conform to the market-lake API contract."""


class JobPollingTimeout(TimeoutError):
    """Polling did not observe a terminal job state before its deadline."""

    def __init__(self, job_id: str, last_status: JobStatus) -> None:
        self.job_id = job_id
        self.last_status = last_status
        super().__init__(f"job {job_id!r} did not finish before the polling deadline")


class ManagedIdentityAuth(httpx.Auth):
    """Attach managed-identity tokens and retry once after a rejection.

    Tokens are acquired through ``credential.get_token(audience)`` for every
    request; ``azure-identity`` caches internally so repeated calls do not
    mint new tokens. A 401 response triggers exactly one retry with a freshly
    acquired token before the response is surfaced to the caller.
    """

    def __init__(self, credential: TokenCredential, audience: str) -> None:
        self._credential = credential
        self._audience = audience

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._acquire().token}"
        response = yield request
        if response.status_code == 401:
            request.headers["Authorization"] = f"Bearer {self._acquire().token}"
            yield request

    def _acquire(self) -> AccessToken:
        return self._credential.get_token(self._audience)


class LakeClient:
    """Thin synchronous client for ensure, polling, and file URL responses."""

    def __init__(
        self,
        base_url: str,
        bearer_token: str | None = None,
        *,
        credential: TokenCredential | None = None,
        audience: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float | httpx.Timeout = 30.0,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty string")
        base_url = base_url.rstrip("/") + "/"
        if credential is not None or audience is not None:
            if bearer_token is not None:
                raise ValueError(
                    "provide exactly one of bearer_token or credential with audience"
                )
            if (
                not isinstance(credential, TokenCredential)
                or not isinstance(audience, str)
                or not audience.strip()
            ):
                raise ValueError(
                    "both credential and a non-empty audience are required"
                    " for Entra authentication"
                )
            self._http = httpx.Client(
                base_url=base_url,
                auth=ManagedIdentityAuth(credential, audience),
                timeout=timeout,
                transport=transport,
            )
            return
        if not isinstance(bearer_token, str) or not bearer_token.strip():
            raise ValueError("bearer_token must be a non-empty string")
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {bearer_token.strip()}"},
            timeout=timeout,
            transport=transport,
        )

    def __enter__(self) -> LakeClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP transport."""
        self._http.close()

    def ensure(self, request: DatasetRequest) -> CoverageResponse | EnsureAccepted:
        """Ensure bounded coverage, returning coverage or an asynchronous job."""
        response = self._request(
            "POST",
            "v1/catalog/ensure",
            json_body=request.to_payload(),
        )
        try:
            return parse_ensure_response(
                _response_object(response),
                location=response.headers.get("Location"),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise LakeProtocolError("invalid ensure response") from error

    def job(self, job_id: str) -> JobStatus:
        """Fetch the current state of one ensure job."""
        normalized = _job_id(job_id)
        response = self._request("GET", f"v1/jobs/{normalized}")
        try:
            return parse_job_status(_response_object(response))
        except (KeyError, TypeError, ValueError) as error:
            raise LakeProtocolError("invalid job response") from error

    def wait_for_job(
        self,
        job_id: str,
        *,
        timeout: float = 60.0,
        poll_interval: float = 0.5,
    ) -> JobStatus:
        """Poll until a job reaches any successful or failed terminal state."""
        if timeout < 0:
            raise ValueError("timeout must not be negative")
        if poll_interval < 0:
            raise ValueError("poll_interval must not be negative")
        normalized = _job_id(job_id)
        deadline = time.monotonic() + timeout
        while True:
            status = self.job(normalized)
            if status.is_terminal:
                return status
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise JobPollingTimeout(normalized, status)
            time.sleep(min(poll_interval, remaining))

    def query_files(
        self,
        request: DatasetRequest,
        *,
        ensure: bool = False,
    ) -> FileQueryResponse:
        """Return complete Parquet files and their server-issued read URLs."""
        payload = request.to_payload()
        payload.update({"mode": "file", "ensure": ensure})
        response = self._request("POST", "v1/query", json_body=payload)
        try:
            return parse_file_query_response(_response_object(response))
        except (KeyError, TypeError, ValueError) as error:
            raise LakeProtocolError("invalid file-query response") from error

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: object | None = None,
    ) -> httpx.Response:
        response = self._http.request(method, url, json=json_body)
        if response.is_error:
            raise _api_error(response)
        return response


def _api_error(response: httpx.Response) -> LakeClientError:
    code = "http_error"
    message = response.reason_phrase or "market-lake request failed"
    details: object | None = None
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            if isinstance(error.get("code"), str):
                code = cast(str, error["code"])
            if isinstance(error.get("message"), str):
                message = cast(str, error["message"])
            details = error.get("details")
    return LakeClientError(
        response.status_code,
        code,
        message,
        details=details,
    )


def _response_object(response: httpx.Response) -> dict[str, object]:
    try:
        body = response.json()
    except ValueError as error:
        raise LakeProtocolError("API response is not valid JSON") from error
    if not isinstance(body, dict) or any(not isinstance(key, str) for key in body):
        raise LakeProtocolError("API response must be a JSON object")
    return body


def _job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not job_id.strip():
        raise ValueError("job_id must be a non-empty string")
    return job_id.strip()


__all__ = [
    "JobPollingTimeout",
    "LakeClient",
    "LakeClientError",
    "LakeProtocolError",
    "ManagedIdentityAuth",
]
