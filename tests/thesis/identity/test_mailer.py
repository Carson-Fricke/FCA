"""Offline tests for the invite mailer port and ACS adapter.

Every test injects a fake transport and a fixed clock, so nothing here talks
to Azure, reads a wall clock, or needs a credential.  The invite token in the
fixtures is the value that must never appear in a raised error.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from thesis.identity.mailer import (
    AcsEmailMailer,
    Mailer,
    MailerError,
    NullMailer,
    RecordingMailer,
)

_T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_EXPIRES_AT = _T0 + timedelta(hours=24)
_TOKEN = "SECRET-INVITE-TOKEN-7f3a"
_INVITE_LINK = f"https://thesis.example.com/invite?token={_TOKEN}"
_RECIPIENT = "invitee@example.com"
_SENDER = "DoNotReply@thesis.example.com"
_ENDPOINT = "https://fca-thesis-acs.communication.azure.com"
_ACCESS_KEY = "dGVzdC1hY2Nlc3Mta2V5"
_CONNECTION_STRING = f"endpoint={_ENDPOINT};accesskey={_ACCESS_KEY}"


class FakeResponse:
    """Minimal ``MailerResponse`` stand-in."""

    def __init__(self, status_code: int, reason_phrase: str) -> None:
        self.status_code = status_code
        self.reason_phrase = reason_phrase


class FakeTransport:
    """Records each request and returns a scripted response or raises."""

    def __init__(
        self,
        *,
        status_code: int = 202,
        reason_phrase: str = "Accepted",
        error: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self.reason_phrase = reason_phrase
        self.error = error
        self.requests: list[dict[str, object]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        content: bytes,
        timeout: float,
    ) -> FakeResponse:
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "content": content,
                "timeout": timeout,
            }
        )
        if self.error is not None:
            raise self.error
        return FakeResponse(self.status_code, self.reason_phrase)


def _mailer(transport: FakeTransport) -> AcsEmailMailer:
    return AcsEmailMailer(
        connection_string=_CONNECTION_STRING,
        sender_address=_SENDER,
        transport=transport,
        clock=lambda: _T0,
    )


def test_acs_mailer_and_recording_mailer_satisfy_the_mailer_port() -> None:
    assert isinstance(_mailer(FakeTransport()), Mailer)
    assert isinstance(RecordingMailer(), Mailer)
    assert isinstance(NullMailer(), Mailer)


def test_send_invite_posts_the_signed_acs_payload() -> None:
    transport = FakeTransport()

    _mailer(transport).send_invite(
        to=_RECIPIENT,
        invite_link=_INVITE_LINK,
        expires_at=_EXPIRES_AT,
    )

    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request["method"] == "POST"
    assert request["url"] == f"{_ENDPOINT}/emails:send?api-version=2023-03-31"

    headers = request["headers"]
    assert isinstance(headers, dict)
    assert headers["Content-Type"] == "application/json"
    assert headers["x-ms-date"] == "Thu, 01 Jan 2026 12:00:00 GMT"
    assert headers["x-ms-content-sha256"]
    assert str(headers["Authorization"]).startswith("HMAC-SHA256 ")

    body = request["content"]
    assert isinstance(body, bytes)
    payload = json.loads(body)
    assert payload["senderAddress"] == _SENDER
    assert payload["recipients"] == {"to": [{"address": _RECIPIENT}]}
    assert _INVITE_LINK in payload["content"]["plainText"]
    assert _TOKEN not in payload["content"]["subject"]


@pytest.mark.parametrize(
    ("status_code", "reason"),
    [(400, "Bad Request"), (403, "Forbidden"), (500, "Server Error")],
)
def test_rejected_send_raises_an_actionable_error_without_the_token(
    status_code: int,
    reason: str,
) -> None:
    transport = FakeTransport(status_code=status_code, reason_phrase=reason)

    with pytest.raises(MailerError) as raised:
        _mailer(transport).send_invite(
            to=_RECIPIENT,
            invite_link=_INVITE_LINK,
            expires_at=_EXPIRES_AT,
        )

    error = raised.value
    message = str(error)
    assert error.status_code == status_code
    assert error.provider == "azure-communication-services-email"
    assert str(status_code) in message
    assert reason in message
    assert _TOKEN not in message
    assert _INVITE_LINK not in message
    assert _RECIPIENT not in message


def test_transport_failure_raises_an_error_without_the_token() -> None:
    transport = FakeTransport(error=httpx.ConnectError("connection reset"))

    with pytest.raises(MailerError) as raised:
        _mailer(transport).send_invite(
            to=_RECIPIENT,
            invite_link=_INVITE_LINK,
            expires_at=_EXPIRES_AT,
        )

    message = str(raised.value)
    assert "ConnectError" in message
    assert _TOKEN not in message
    assert _INVITE_LINK not in message


def test_recording_mailer_stores_the_invite() -> None:
    mailer = RecordingMailer()

    mailer.send_invite(
        to=_RECIPIENT,
        invite_link=_INVITE_LINK,
        expires_at=_EXPIRES_AT,
    )

    assert len(mailer.sent) == 1
    sent = mailer.sent[0]
    assert sent.to == _RECIPIENT
    assert sent.invite_link == _INVITE_LINK
    assert sent.expires_at == _EXPIRES_AT


def test_null_mailer_discards_the_invite() -> None:
    assert (
        NullMailer().send_invite(
            to=_RECIPIENT,
            invite_link=_INVITE_LINK,
            expires_at=_EXPIRES_AT,
        )
        is None
    )


def test_connection_string_and_sender_are_validated_without_echoing_secrets() -> None:
    with pytest.raises(ValueError, match="endpoint"):
        AcsEmailMailer(connection_string="accesskey=only", sender_address=_SENDER)
    with pytest.raises(ValueError, match="sender address"):
        AcsEmailMailer(
            connection_string=_CONNECTION_STRING,
            sender_address="",
        )
