"""Enterprise n8n workflow control-plane, scheduler, and signed callback endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request, status
from pydantic import ValidationError

from ..config import get_settings
from ..schemas import (
    AutomationScheduleCreateRequest,
    AutomationScheduleUpdateRequest,
    N8NFlowDeployRequest,
    N8NFlowGenerateRequest,
    N8NWorkflowCallbackPayload,
    WorkflowConfigUpdateRequest,
    WorkflowTriggerRequest,
)
from ..services import n8n_client

router = APIRouter(tags=["Enterprise Automation & Scheduling"])


@router.get("/api/v1/automations/workflows")
def list_registered_workflows():
    """List GM-01 through GM-05 workflows with cron, webhook readiness, and run telemetry."""
    workflows = n8n_client.list_workflows()
    return {"count": len(workflows), "workflows": workflows}


@router.patch("/api/v1/automations/workflows/{workflow_code}")
async def update_workflow_configuration(workflow_code: str, payload: WorkflowConfigUpdateRequest):
    """Update workflow cron, webhook path, or enable/disable state (and sync n8n REST API)."""
    try:
        wf = await n8n_client.update_workflow_registry(workflow_code.upper(), payload)
        return {"message": "Workflow configuration updated.", "workflow": wf}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/v1/automations/workflows/{workflow_code}/trigger")
async def trigger_automation_workflow(workflow_code: str, payload: WorkflowTriggerRequest):
    """Trigger an n8n workflow on demand via API (with optional local pipeline + signed webhook)."""
    try:
        return await n8n_client.trigger_workflow(workflow_code.upper(), payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/v1/automations/schedules", status_code=status.HTTP_201_CREATED)
def create_automation_schedule(payload: AutomationScheduleCreateRequest):
    """Create a cron, interval_minutes, or one_time automation schedule for any GM workflow."""
    try:
        job = n8n_client.create_schedule(payload)
        return {"message": "Automation schedule created.", "schedule": job}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/v1/automations/schedules")
def list_automation_schedules(
    schedule_status: str | None = Query(default=None, alias="status"),
):
    """List scheduled automation jobs with optional ACTIVE/PAUSED/CANCELLED/COMPLETED filter."""
    jobs = n8n_client.list_schedules(status=schedule_status)
    return {"count": len(jobs), "schedules": jobs}


@router.patch("/api/v1/automations/schedules/{job_id}")
def update_automation_schedule(job_id: str, payload: AutomationScheduleUpdateRequest):
    """Pause, resume, cancel, or modify an existing scheduled automation job."""
    try:
        job = n8n_client.update_schedule(job_id, payload)
        return {"message": "Automation schedule updated.", "schedule": job}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/v1/automations/schedules/{job_id}/run-now")
async def run_schedule_now(job_id: str):
    """Execute a scheduled automation job immediately via API."""
    try:
        return await n8n_client.run_scheduled_job_now(job_id, triggered_by="API_RunNow")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/v1/automations/schedules/tick")
async def tick_scheduled_automations():
    """Evaluate due ACTIVE schedules in IST and execute all overdue jobs."""
    return await n8n_client.tick_due_schedules()


@router.post("/api/v1/automations/logs/{log_id}/retry")
async def retry_failed_automation_log(log_id: str):
    """Replay a previously logged webhook payload from automation_logs to n8n."""
    try:
        return await n8n_client.retry_automation_event(log_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/v1/webhooks/n8n/workflow-callback")
async def handle_n8n_workflow_callback(
    request: Request,
    x_crush_signature: str = Header(default=""),
):
    """Receive signed execution callbacks from n8n back into the FastAPI control plane."""
    raw_bytes = await request.body()
    settings = get_settings()
    if settings.n8n_webhook_secret and not n8n_client.verify_hmac_signature(
        raw_bytes, x_crush_signature, settings.n8n_webhook_secret
    ):
        raise HTTPException(status_code=401, detail="Invalid X-Crush-Signature webhook signature.")

    try:
        payload = N8NWorkflowCallbackPayload.model_validate_json(raw_bytes or b"{}")
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    return n8n_client.record_workflow_callback(payload)


@router.get("/api/v1/automations/n8n/env-checklist")
def get_n8n_env_checklist():
    """Return readiness checklist of all .env variables for Google Sheets & auto n8n deployment."""
    return n8n_client.get_env_readiness_checklist()


@router.post("/api/v1/automations/n8n/generate")
def generate_custom_n8n_workflows(payload: N8NFlowGenerateRequest | None = Body(default=None)):
    """Generate customized GM-01..GM-05 workflow JSONs configured with studio Sheet ID, URLs & schedules."""
    req = payload or N8NFlowGenerateRequest()
    try:
        return n8n_client.generate_configured_n8n_workflows(
            workflow_codes=req.workflow_codes,
            spreadsheet_id=req.spreadsheet_id,
            fastapi_url=req.fastapi_url,
            whatsapp_phone_number_id=req.whatsapp_phone_number_id,
            inject_credentials=req.inject_credentials,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/v1/automations/n8n/deploy")
async def deploy_workflows_to_n8n_instance(payload: N8NFlowDeployRequest | None = Body(default=None)):
    """Auto-deploy customized GM-01..GM-05 workflows to n8n REST API (or return dry_run plan)."""
    req = payload or N8NFlowDeployRequest()
    try:
        return await n8n_client.deploy_workflows_to_n8n(
            workflow_codes=req.workflow_codes,
            activate=req.activate,
            dry_run=req.dry_run,
            spreadsheet_id=req.spreadsheet_id,
            fastapi_url=req.fastapi_url,
            whatsapp_phone_number_id=req.whatsapp_phone_number_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
