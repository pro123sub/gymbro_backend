"""Inbound n8n webhook receivers and executive analytics endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Query, status

from ..database import (
    TAB_NAME_LOOKUP,
    get_db,
    get_master_db_status,
    get_sheets_workbook_snapshot,
)
from ..schemas import (
    ExpenseCreateRequest,
    GymSettingsUpdateRequest,
    MembershipPlanCreateRequest,
    MembershipPlanUpdateRequest,
    N8NFollowUpLogPayload,
    N8NMemberSyncPayload,
    SheetsRowAppendRequest,
    SheetsSyncPullRequest,
)
from ..services import accounts_service, ai_agent_service, membership_service, n8n_client
from ..services.n8n_client import log_automation_event

router = APIRouter(tags=["n8n Webhooks & Analytics"])


@router.post("/api/v1/webhooks/n8n/member-registered")
def n8n_member_registered(payload: N8NMemberSyncPayload):
    """Inbound sync endpoint for n8n GM-01 Form Trigger submissions."""
    try:
        member = membership_service.sync_member_from_n8n(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log_automation_event(
        workflow="GM-01",
        event_type="N8N_INBOUND_MEMBER_SYNC",
        member_id=member.member_id,
        status="RECEIVED",
        payload=payload.model_dump(),
        response_status=200,
        details=f"Synced member {member.name} from n8n Form Trigger.",
    )
    return {"synced": True, "member": member}


@router.post("/api/v1/webhooks/n8n/followup-logged")
def n8n_followup_logged(payload: N8NFollowUpLogPayload):
    """Inbound callback from n8n GM-02 confirming WhatsApp template dispatch."""
    try:
        res = membership_service.record_followup_callback(payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    log_automation_event(
        workflow="GM-02",
        event_type=payload.followup_type,
        member_id=payload.member_id,
        status=payload.status,
        payload=payload.model_dump(),
        response_status=200,
        details=payload.message[:250],
    )
    return res


@router.get("/api/v1/analytics/dashboard")
def get_executive_dashboard():
    """Return real-time Cult.fit-grade studio KPIs and follow-up counts."""
    return membership_service.get_dashboard_analytics()


@router.get("/api/v1/plans")
def list_membership_plans():
    """Return active membership plans configured in the studio database."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM membership_plans WHERE is_active = 1 ORDER BY duration_days ASC"
        ).fetchall()
    return {"plans": [dict(r) for r in rows]}


@router.post("/api/v1/plans", status_code=status.HTTP_201_CREATED)
def create_membership_plan(payload: MembershipPlanCreateRequest):
    """Create a new custom or studio membership plan."""
    try:
        plan = membership_service.create_membership_plan(payload)
        return {"message": "Membership plan created.", "plan": plan}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/api/v1/plans/{plan_id}")
def update_membership_plan(plan_id: str, payload: MembershipPlanUpdateRequest):
    """Update pricing, duration, freeze quota, or active flag for a membership plan."""
    try:
        plan = membership_service.update_membership_plan(plan_id, payload)
        return {"message": "Membership plan updated.", "plan": plan}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/v1/settings")
def get_gym_settings():
    """Return persistent studio configuration key-value settings."""
    return {"settings": membership_service.get_gym_settings()}


@router.put("/api/v1/settings")
def update_gym_settings(payload: GymSettingsUpdateRequest):
    """Update persistent studio configuration key-value settings."""
    return {"message": "Studio settings updated.", "settings": membership_service.update_gym_settings(payload)}


@router.get("/api/v1/automation-logs")
def list_automation_logs(
    workflow: str | None = Query(default=None),
    log_status: str | None = Query(default=None, alias="status"),
    member_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    """Return filterable automation execution logs."""
    logs = membership_service.list_automation_logs(
        workflow=workflow,
        status=log_status,
        member_id=member_id,
        limit=limit,
    )
    return {"count": len(logs), "logs": logs}


@router.get("/api/v1/sheets/workbook-export")
def get_full_sheets_workbook_export():
    """Export all 6 Google Sheets tabs in exact column structure."""
    return membership_service.export_full_workbook()


@router.get("/api/v1/sheets/status")
def get_google_sheets_storage_status():
    """Return Google Sheets workbook backend configuration and row counts per tab."""
    snapshot = get_sheets_workbook_snapshot()
    return {
        "storage_backend": snapshot["storage_backend"],
        "google_sheets_mode": snapshot["google_sheets_mode"],
        "spreadsheet_id": snapshot["spreadsheet_id"],
        "spreadsheet_name": snapshot["spreadsheet_name"],
        "workbook_path": snapshot["workbook_path"],
        "updated_at_ist": snapshot["updated_at_ist"],
        "counts": snapshot["counts"],
        "tabs": list(snapshot["tabs"].keys()),
    }


@router.get("/api/v1/sheets/tabs/{tab_name}")
def read_google_sheets_tab(tab_name: str):
    """Read all rows from any Google Sheets workbook tab."""
    canonical = TAB_NAME_LOOKUP.get(tab_name.strip().lower())
    if not canonical:
        raise HTTPException(status_code=404, detail=f"Google Sheets tab '{tab_name}' not found.")
    snapshot = get_sheets_workbook_snapshot()
    rows = snapshot["tabs"].get(canonical, [])
    return {"tab": canonical, "count": len(rows), "rows": rows}


@router.post("/api/v1/sheets/tabs/{tab_name}/append", status_code=status.HTTP_201_CREATED)
def append_google_sheets_tab_row(tab_name: str, payload: SheetsRowAppendRequest):
    """Append or upsert a structured row directly into a Google Sheets workbook tab."""
    canonical = TAB_NAME_LOOKUP.get(tab_name.strip().lower())
    if not canonical:
        raise HTTPException(status_code=404, detail=f"Google Sheets tab '{tab_name}' not found.")

    row_data = payload.row
    if not row_data:
        raise HTTPException(status_code=400, detail="row payload cannot be empty.")

    try:
        if canonical == "Members":
            member = membership_service.sync_member_from_n8n(N8NMemberSyncPayload(**row_data))
            return {"tab": canonical, "row": membership_service.member_to_sheets_row(member)}
        if canonical == "Expenses":
            expense = accounts_service.record_expense(
                ExpenseCreateRequest(**row_data), recorded_by="Google_Sheets_Append"
            )
            return {"tab": canonical, "row": expense}
        if canonical == "Membership Plans":
            plan = membership_service.create_membership_plan(MembershipPlanCreateRequest(**row_data))
            return {"tab": canonical, "row": plan}
        if canonical == "Gym Settings":
            if "setting_key" in row_data and "setting_value" in row_data:
                settings_map = {str(row_data["setting_key"]): str(row_data["setting_value"])}
            else:
                settings_map = {str(k): str(v) for k, v in row_data.items()}
            updated = membership_service.update_gym_settings(GymSettingsUpdateRequest(settings=settings_map))
            return {"tab": canonical, "settings": updated}
        if canonical == "Automation Log":
            log_id = log_automation_event(
                workflow=str(row_data.get("workflow") or "SHEETS"),
                event_type=str(row_data.get("event_type") or "MANUAL_APPEND"),
                member_id=str(row_data.get("member_id") or "STUDIO"),
                status=str(row_data.get("status") or "LOGGED"),
                payload=row_data,
                response_status=200,
                details=str(row_data.get("details") or ""),
            )
            return {"tab": canonical, "log_id": log_id, "row": row_data}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    raise HTTPException(status_code=400, detail=f"Direct append is not enabled for tab '{canonical}'.")


@router.post("/api/v1/sheets/sync/push")
async def push_google_sheets_workbook():
    """Flush and push the active workbook snapshot to Google Sheets."""
    return await n8n_client.sync_sheets_with_remote(direction="push")


@router.post("/api/v1/sheets/sync/pull")
async def pull_google_sheets_workbook(payload: SheetsSyncPullRequest | None = Body(default=None)):
    """Pull/hydrate tabs from an incoming Google Sheets snapshot or configured bridge."""
    return await n8n_client.sync_sheets_with_remote(
        direction="pull",
        workbook_payload=payload.workbook if payload else None,
    )


@router.get("/api/v1/database/master-status")
def read_master_database_status():
    """Return Render PostgreSQL Master DB connection health, write pipeline, and row counts."""
    return get_master_db_status()


@router.get("/api/v1/analytics/trends")
def get_automated_trends():
    """Compute automated studio health score, cohort/package trends, churn radar, and insights."""
    return membership_service.get_automated_trends_and_analysis()


@router.get("/api/v1/sheets/executive-view")
def get_executive_google_sheets_presentation():
    """Return formatted gym-admin presentation tabs built from the Master Database."""
    return membership_service.get_executive_sheets_presentation()


@router.post("/api/v1/automations/master-sync-and-analyze")
async def run_master_sync_trends_and_followup_pipeline():
    """Execute end-to-end automation: Master PostgreSQL -> Autopilot Follow-Ups -> Trends -> Executive Sheets."""
    autopilot = ai_agent_service.run_full_gym_autopilot()
    trends = membership_service.get_automated_trends_and_analysis()
    executive_sheets = membership_service.get_executive_sheets_presentation()
    sheets_push = await n8n_client.sync_sheets_with_remote(direction="push")
    master_status = get_master_db_status()
    return {
        "master_database": master_status,
        "autopilot": autopilot,
        "trends": trends,
        "executive_sheets_counts": executive_sheets["counts"],
        "sheets_sync": sheets_push,
    }
