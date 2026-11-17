"""Smart Cult.fit-grade retention & follow-up orchestration endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Query

from ..config import get_settings, timestamp_ist_str, today_ist_str
from ..database import get_db
from ..schemas import FollowUpDispatchRequest, N8NFollowUpLogPayload
from ..services import membership_service
from ..services.n8n_client import dispatch_to_n8n

router = APIRouter(prefix="/api/v1/follow-ups", tags=["Follow-Ups"])


@router.get("/due")
def get_due_follow_ups(
    include_reminded_today: bool = Query(default=False),
):
    """Return prioritized Cult.fit-style follow-ups (Onboarding, Dues, Expiry, Inactivity, Win-Back)."""
    items = membership_service.evaluate_follow_ups(
        include_already_reminded_today=include_reminded_today
    )
    return {
        "as_of_ist": timestamp_ist_str(),
        "count": len(items),
        "follow_ups": items,
    }


@router.post("/dispatch")
async def dispatch_due_follow_ups(payload: FollowUpDispatchRequest):
    """Dispatch due follow-ups to n8n GM-02 webhook and record follow-up logs."""
    all_due = membership_service.evaluate_follow_ups(include_already_reminded_today=False)

    filtered = all_due
    if payload.member_ids:
        id_set = set(payload.member_ids)
        filtered = [i for i in filtered if i.member_id in id_set]
    if payload.followup_types:
        type_set = set(payload.followup_types)
        filtered = [i for i in filtered if i.followup_type in type_set]

    if payload.dry_run:
        return {
            "dry_run": True,
            "would_dispatch_count": len(filtered),
            "follow_ups": filtered,
        }

    settings = get_settings()
    today = today_ist_str()
    dispatched_results = []

    for item in filtered:
        n8n_res = await dispatch_to_n8n(
            webhook_url=settings.n8n_gm02_dispatch_webhook_url,
            workflow="GM-02",
            event_type=item.followup_type,
            member_id=item.member_id,
            payload=item.model_dump(),
        )
        log_res = membership_service.record_followup_callback(
            N8NFollowUpLogPayload(
                member_id=item.member_id,
                followup_type=item.followup_type,
                channel=item.channel,
                status=n8n_res.get("status", "LOGGED"),
                message=item.message_text,
                last_reminder_date=today,
            )
        )
        dispatched_results.append(
            {
                "member_id": item.member_id,
                "followup_type": item.followup_type,
                "n8n": n8n_res,
                "follow_up_log": log_res,
            }
        )

    return {
        "dry_run": False,
        "dispatched_count": len(dispatched_results),
        "results": dispatched_results,
    }


@router.get("/history")
def list_follow_up_history():
    """Return recent follow-up execution logs."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM follow_up_logs ORDER BY dispatched_at_ist DESC LIMIT 100"
        ).fetchall()
    return {"count": len(rows), "logs": [dict(r) for r in rows]}
