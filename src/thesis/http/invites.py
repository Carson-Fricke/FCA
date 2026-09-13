"""Invite lifecycle HTTP surface for Thesis.

The router exposes the operator-facing invite lifecycle over an injected
:class:`~thesis.identity.invites.InviteService`:

* ``POST /v1/invites`` creates one invite and returns its one-time signup link
  exactly once.  The response never repeats the link on any later call.
* ``GET /v1/invites`` lists invites and never returns token material.
* ``POST /v1/invites/{invite_id}/revoke`` invalidates an invite before it is
  redeemed.

Role enforcement is an explicit seam rather than an embedded policy.  The
router records the actor through the injectable ``actor`` callable and adds no
authorization dependency of its own, so the coordinator (and T314's audit,
revocation, and rate-limit hardening) can compose the required role guard at
``include_router(..., dependencies=[...])`` time without changing this module.
A missing or weak guard is therefore a visible composition choice, not hidden
behavior.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from thesis.identity.invites import (
    InviteDeliveryError,
    InviteListingUnavailableError,
    InviteService,
)
from thesis.identity.models import AccountRole, Invite


class CreateInviteRequest(BaseModel):
    """One invite creation request; the raw link is returned on success only."""

    model_config = ConfigDict(extra="forbid")

    email: str | None = None
    role: AccountRole = "member"
    expires_in_days: int = Field(default=7, ge=1, le=365)
    note: str | None = Field(default=None, max_length=2_048)


def _invite_payload(invite: Invite) -> dict[str, object]:
    """Shape one invite for the API, omitting the persisted token hash."""
    return {
        "id": invite.id,
        "email": invite.bound_email,
        "role": invite.role,
        "created_by": invite.created_by,
        "created_at": invite.created_at.isoformat(),
        "expires_at": invite.expires_at.isoformat(),
        "consumed_at": (
            invite.consumed_at.isoformat() if invite.consumed_at is not None else None
        ),
        "revoked_at": (
            invite.revoked_at.isoformat() if invite.revoked_at is not None else None
        ),
        "note": invite.note,
    }


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _default_actor(request: Request) -> str:
    """Record an anonymous caller as ``operator`` when no actor is injected."""
    del request
    return "operator"


def create_invites_router(
    service: InviteService,
    *,
    invite_link_base: str,
    clock: Callable[[], datetime] | None = None,
    actor: Callable[[Request], str] | None = None,
) -> APIRouter:
    """Create the invite lifecycle router over the injected service.

    ``invite_link_base`` is the SPA signup URL the one-time token is appended
    to.  ``actor`` resolves the recorded ``created_by`` from the request; when
    omitted every invite is attributed to ``operator``.  ``clock`` supplies the
    aware instant used for revocation.
    """
    base = invite_link_base.strip()
    if not base:
        raise ValueError("invite_link_base must be a non-empty URL")
    now = clock or _utc_now
    resolve_actor = actor or _default_actor

    router = APIRouter(prefix="/v1/invites", tags=["invites"])

    @router.post("", status_code=status.HTTP_201_CREATED)
    async def create_invite(
        payload: CreateInviteRequest, request: Request
    ) -> dict[str, object]:
        try:
            issued = service.create_invite(
                email=payload.email,
                role=payload.role,
                expires_in=timedelta(days=payload.expires_in_days),
                note=payload.note,
                created_by=resolve_actor(request),
                invite_link_base=base,
            )
        except InviteDeliveryError as error:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "code": "invite_delivery_failed",
                    "message": "the invitation could not be delivered",
                },
            ) from error
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "invalid_invite", "message": str(error)},
            ) from error
        return {
            "invite": _invite_payload(issued.invite),
            "invite_link": issued.invite_link,
        }

    @router.get("")
    async def list_invites() -> dict[str, object]:
        try:
            invites = service.list_invites()
        except InviteListingUnavailableError as error:
            # The composed store cannot enumerate invites; fail with a shaped
            # error instead of leaking an unhandled 500 from the endpoint.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "invite_listing_unavailable",
                    "message": "the configured invite store cannot list invites",
                },
            ) from error
        return {"items": [_invite_payload(invite) for invite in invites]}

    @router.post("/{invite_id}/revoke")
    async def revoke_invite(invite_id: str) -> dict[str, str]:
        if not service.revoke_invite(invite_id, at=now()):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "invite_not_revocable",
                    "message": "no revocable invite was found for that id",
                },
            )
        return {"status": "revoked"}

    return router


__all__ = ["CreateInviteRequest", "create_invites_router"]
