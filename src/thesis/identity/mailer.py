"""Invite delivery port and an Azure Communication Services email adapter.

Thesis invites are single-use links whose raw token exists only in the emailed
URL, so delivery is a narrow port: :class:`Mailer` sends one invite and returns
nothing, mirroring the structural ``InviteMailer`` protocol in
:mod:`thesis.identity.invites`.  :class:`AcsEmailMailer` implements the port
against the Azure Communication Services Email REST API over an injected
transport, so unit tests never touch the network, and :class:`RecordingMailer`
and :class:`NullMailer` stand in for local development.

Credentials are Key Vault-only.  The mailer receives an already-resolved ACS
connection string as constructor input; it never reads a file, an environment
variable, or Key Vault itself, and it never embeds the secret in an error.
:class:`MailerError` carries the provider and the HTTP status/reason only, so a
failed send can be diagnosed without leaking the invite token or the full
invite link.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Final, Protocol, cast, runtime_checkable
from urllib.parse import urlsplit

import httpx

_PROVIDER: Final = "azure-communication-services-email"
_ACS_API_VERSION: Final = "2023-03-31"
_ACS_SEND_PATH: Final = f"/emails:send?api-version={_ACS_API_VERSION}"
_INVITE_SUBJECT: Final = "Your FCA Thesis invitation"

type Clock = Callable[[], datetime]


@runtime_checkable
class Mailer(Protocol):
    """Deliver one invite link to a recipient.

    The signature is structurally identical to the ``InviteMailer`` protocol
    declared by :mod:`thesis.identity.invites`, so a concrete mailer can be
    injected wherever the invite lifecycle expects one.
    """

    def send_invite(
        self,
        *,
        to: str,
        invite_link: str,
        expires_at: datetime,
    ) -> None:
        """Send ``invite_link`` to ``to``; raise :class:`MailerError` on failure."""


class MailerError(RuntimeError):
    """An actionable invite-delivery failure that never leaks the invite.

    The message names the provider and, for a rejected request, the HTTP
    status and reason phrase.  It never includes the recipient, the invite
    link, or the invite token, so a failed send is safe to log or surface to
    an operator.
    """

    def __init__(
        self,
        *,
        provider: str,
        status_code: int | None,
        reason: str,
    ) -> None:
        self.provider = provider
        self.status_code = status_code
        self.reason = reason
        if status_code is None:
            detail = f"transport failure ({reason})"
        else:
            detail = f"HTTP {status_code} ({reason})"
        super().__init__(f"{provider} invite delivery failed: {detail}")


class MailerResponse(Protocol):
    """Response surface required by the ACS mailer and test fakes."""

    status_code: int
    reason_phrase: str


class MailerTransport(Protocol):
    """Request surface implemented by ``httpx.Client`` and test fakes."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        content: bytes,
        timeout: float,
    ) -> MailerResponse:
        """Send one synchronous HTTP request."""


@dataclass(frozen=True)
class SentInvite:
    """One invite captured by :class:`RecordingMailer`."""

    to: str
    invite_link: str
    expires_at: datetime


@dataclass
class RecordingMailer:
    """Deterministic in-memory mailer for local development and tests."""

    sent: list[SentInvite] = field(default_factory=list)

    def send_invite(
        self,
        *,
        to: str,
        invite_link: str,
        expires_at: datetime,
    ) -> None:
        """Record the invite without contacting any provider."""
        self.sent.append(
            SentInvite(to=to, invite_link=invite_link, expires_at=expires_at)
        )


class NullMailer:
    """Mailer that discards every invite; used where delivery is disabled."""

    def send_invite(
        self,
        *,
        to: str,
        invite_link: str,
        expires_at: datetime,
    ) -> None:
        """Accept the invite and return, sending nothing."""
        return None


class AcsEmailMailer:
    """Send invite emails through the Azure Communication Services REST API.

    ``connection_string`` is the operator-provisioned ACS connection string
    resolved from Key Vault by the caller; ``endpoint`` can override the
    endpoint embedded in it.  ``transport`` is optional in application code and
    can be an ``httpx.Client``; tests inject a fake.  The mailer only closes a
    default client it created itself.
    """

    def __init__(
        self,
        *,
        connection_string: str,
        sender_address: str,
        endpoint: str | None = None,
        transport: MailerTransport | None = None,
        timeout_seconds: float = 30.0,
        clock: Clock | None = None,
    ) -> None:
        embedded_endpoint, access_key = _parse_connection_string(connection_string)
        resolved_endpoint = embedded_endpoint if endpoint is None else endpoint
        if not resolved_endpoint.strip():
            raise ValueError("ACS endpoint must be a non-empty URL")
        if not sender_address.strip():
            raise ValueError("ACS sender address must be a non-empty address")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        self._endpoint = resolved_endpoint.strip().rstrip("/")
        self._sender_address = sender_address.strip()
        self._signing_key = _decode_access_key(access_key)
        self._timeout = float(timeout_seconds)
        self._clock = _utc_now if clock is None else clock
        self._default_client: httpx.Client | None = None

        if transport is None:
            self._default_client = httpx.Client(timeout=self._timeout)
            self._transport: MailerTransport = cast(
                MailerTransport, self._default_client
            )
        else:
            self._transport = transport

    def __enter__(self) -> AcsEmailMailer:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the internally-created HTTP client, if there is one."""
        if self._default_client is not None:
            client = self._default_client
            self._default_client = None
            client.close()

    def send_invite(
        self,
        *,
        to: str,
        invite_link: str,
        expires_at: datetime,
    ) -> None:
        """POST the invite to the ACS Email service, raising on any non-2xx."""
        body = _invite_body(
            sender_address=self._sender_address,
            to=to,
            invite_link=invite_link,
            expires_at=expires_at,
        )
        url = f"{self._endpoint}{_ACS_SEND_PATH}"
        host = urlsplit(self._endpoint).netloc
        headers = self._signed_headers(body=body, host=host)

        try:
            response = self._transport.request(
                "POST",
                url,
                headers=headers,
                content=body,
                timeout=self._timeout,
            )
        except (httpx.HTTPError, OSError, TimeoutError) as error:
            raise MailerError(
                provider=_PROVIDER,
                status_code=None,
                reason=type(error).__name__,
            ) from error

        if not 200 <= response.status_code < 300:
            raise MailerError(
                provider=_PROVIDER,
                status_code=response.status_code,
                reason=_reason_phrase(response),
            )

    def _signed_headers(self, *, body: bytes, host: str) -> dict[str, str]:
        """Build HMAC-signed headers for one ACS Email send request."""
        now = self._clock().astimezone(UTC)
        date = format_datetime(now, usegmt=True)
        content_hash = base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")
        string_to_sign = f"POST\n{_ACS_SEND_PATH}\n{date};{host};{content_hash}"
        signature = base64.b64encode(
            hmac.new(
                self._signing_key,
                string_to_sign.encode("ascii"),
                hashlib.sha256,
            ).digest()
        ).decode("ascii")
        return {
            "Content-Type": "application/json",
            "x-ms-date": date,
            "x-ms-content-sha256": content_hash,
            "Authorization": (
                "HMAC-SHA256 "
                "SignedHeaders=x-ms-date;host;x-ms-content-sha256"
                f"&Signature={signature}"
            ),
        }


def _invite_body(
    *,
    sender_address: str,
    to: str,
    invite_link: str,
    expires_at: datetime,
) -> bytes:
    """Serialize the ACS Email payload deterministically as UTF-8 JSON."""
    payload = {
        "senderAddress": sender_address,
        "recipients": {"to": [{"address": to}]},
        "content": {
            "subject": _INVITE_SUBJECT,
            "plainText": (
                "You have been invited to FCA Thesis.\n\n"
                f"Open this single-use link to accept:\n{invite_link}\n\n"
                f"This invitation expires at {expires_at.isoformat()}."
            ),
        },
    }
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def _parse_connection_string(value: str) -> tuple[str, str]:
    """Extract the endpoint and access key from an ACS connection string.

    The secret is never included in the raised message.
    """
    fields: dict[str, str] = {}
    for part in value.split(";"):
        key, separator, raw = part.partition("=")
        if separator:
            fields[key.strip().lower()] = raw.strip()
    endpoint = fields.get("endpoint", "")
    access_key = fields.get("accesskey", "")
    if not endpoint or not access_key:
        raise ValueError(
            "ACS connection string must contain 'endpoint' and 'accesskey'"
        )
    return endpoint, access_key


def _decode_access_key(access_key: str) -> bytes:
    """Decode the base64 ACS access key without echoing it on failure."""
    try:
        return base64.b64decode(access_key)
    except ValueError as error:
        raise ValueError(
            "ACS connection string 'accesskey' is not valid base64"
        ) from error


def _reason_phrase(response: MailerResponse) -> str:
    reason = getattr(response, "reason_phrase", "")
    return reason if isinstance(reason, str) and reason else "request rejected"


def _utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "AcsEmailMailer",
    "Mailer",
    "MailerError",
    "MailerResponse",
    "MailerTransport",
    "NullMailer",
    "RecordingMailer",
    "SentInvite",
]
