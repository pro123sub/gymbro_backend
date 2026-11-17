"""Member lifecycle endpoints: registration, 360 profile, freeze, and Sheets projection."""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Query, status

from ..config import get_settings
from ..schemas import FreezeMembershipRequest, MemberCreateRequest, MemberRenewRequest, MemberUpdateRequest
from ..services import membership_service
from ..services.n8n_client import dispatch_to_n8n

router = APIRouter(prefix="/api/v1/members", tags=["Members"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_member(payload: MemberCreateRequest):
    """Register a new gym member, compute expiry, and optionally sync to n8n/Google Sheets."""
    member, payment_receipt = membership_service.register_member(payload)
    sheets_row = membership_service.member_to_sheets_row(member)

    n8n_sync = {"dispatched": False, "status": "SKIPPED"}
    if payload.sync_to_n8n:
        settings = get_settings()
        n8n_sync = await dispatch_to_n8n(
            webhook_url=settings.n8n_gm01_sync_webhook_url,
            workflow="GM-01",
            event_type="MEMBER_REGISTERED",
            member_id=member.member_id,
            payload={"member": member.model_dump(), "google_sheets_row": sheets_row},
        )

    return {
        "message": "Member registered successfully!",
        "member": member,
        "google_sheets_row": sheets_row,
        "payment_receipt": payment_receipt,
        "n8n_sync": n8n_sync,
    }


@router.get("")
def list_all_members(
    member_status: str | None = Query(default=None, description="Active, Expiring Soon, Expired, Frozen, or Pending"),
    plan: str | None = Query(default=None),
    search: str | None = Query(default=None),
):
    """List members with optional status, plan, or search filters."""
    items = membership_service.list_members(status=member_status, plan=plan, search=search)
    return {"count": len(items), "members": items}


@router.get("/{member_id}")
def get_member_profile(member_id: str):
    """Retrieve a member's 360-degree profile (details, payments, attendance, freezes, follow-ups)."""
    profile = membership_service.get_member_360(member_id)
    if not profile:
        raise HTTPException(status_code=404, detail=f"Member {member_id} not found.")
    return profile


@router.get("/{member_id}/sheets-row")
def get_member_sheets_projection(member_id: str):
    """Return the exact 18-column Google Sheets dictionary for n8n sync."""
    profile = membership_service.get_member_360(member_id)
    if not profile:
        raise HTTPException(status_code=404, detail=f"Member {member_id} not found.")
    return profile["google_sheets_row"]


@router.post("/{member_id}/freeze")
def freeze_member_plan(member_id: str, payload: FreezeMembershipRequest):
    """Freeze/pause an active membership (Cult.fit style) and extend expiry_date."""
    try:
        return membership_service.freeze_membership(member_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/{member_id}")
def update_member_profile(member_id: str, payload: MemberUpdateRequest):
    """Apply partial updates to a member's profile, trainer, batch, or status."""
    try:
        member = membership_service.update_member(member_id, payload)
        return {
            "message": "Member updated successfully.",
            "member": member,
            "google_sheets_row": membership_service.member_to_sheets_row(member),
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{member_id}/unfreeze")
def unfreeze_member_plan(member_id: str, payload: dict[str, str] | None = Body(default=None)):
    """Resume a frozen membership early and reclaim unused freeze days."""
    reason = str((payload or {}).get("reason") or "Resumed early").strip() or "Resumed early"
    try:
        return membership_service.unfreeze_membership(member_id, reason=reason)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{member_id}/renew")
async def renew_member_subscription(member_id: str, payload: MemberRenewRequest):
    """Renew or upgrade a member's subscription and optionally sync to n8n GM-03."""
    try:
        result = membership_service.renew_member(member_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if payload.sync_to_n8n and not result.get("already_processed"):
        settings = get_settings()
        result["n8n_sync"] = await dispatch_to_n8n(
            webhook_url=settings.n8n_gm03_payment_webhook_url,
            workflow="GM-03",
            event_type="MEMBERSHIP_RENEWED",
            member_id=member_id,
            payload={
                "member_id": member_id,
                "amount_paid": payload.amount_paid,
                "payment_mode": payload.payment_mode,
                "transaction_ref": result["payment"]["transaction_ref"],
                "plan": result["member"].plan,
                "source": "fastapi_backend_renew",
            },
        )
    else:
        result["n8n_sync"] = {"dispatched": False, "status": "SKIPPED"}
    return result


@router.delete("/{member_id}")
def delete_or_archive_member(member_id: str, hard: bool = Query(default=False)):
    """Soft-archive (Cancelled) or permanently delete a member record."""
    try:
        return membership_service.archive_or_delete_member(member_id, hard=hard)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
