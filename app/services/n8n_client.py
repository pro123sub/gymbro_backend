"""Async HTTP client for dispatching signed webhooks to n8n on Railway."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import httpx

from ..config import get_settings, now_ist, timestamp_ist_str
from ..database import (
    get_db,
    get_sheets_workbook_snapshot,
    load_sheets_workbook_snapshot,
    save_sheets_workbook_snapshot,
)
from ..schemas import (
    AutomationScheduleCreateRequest,
    AutomationScheduleUpdateRequest,
    N8NWorkflowCallbackPayload,
    WorkflowConfigUpdateRequest,
    WorkflowTriggerRequest,
)


def sign_payload(raw_bytes: bytes, secret: str) -> str:
    """Compute HMAC-SHA256 hex digest for webhook verification."""
    return hmac.new(secret.encode("utf-8"), raw_bytes, hashlib.sha256).hexdigest()


def verify_hmac_signature(raw_bytes: bytes, signature: str, secret: str) -> bool:
    """Constant-time verification of an incoming HMAC-SHA256 webhook signature."""
    if not secret:
        return True
    expected = sign_payload(raw_bytes, secret)
    return hmac.compare_digest(expected, (signature or "").strip())


def log_automation_event(
    workflow: str,
    event_type: str,
    member_id: str,
    status: str,
    payload: dict[str, Any],
    response_status: int | None = None,
    details: str = "",
) -> str:
    """Persist an automation audit record in SQLite."""
    log_id = f"AUTO-{secrets.token_hex(4).upper()}"
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO automation_logs
                (log_id, workflow, event_type, member_id, status, payload_json, response_status, details, created_at_ist)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                log_id,
                workflow,
                event_type,
                member_id,
                status,
                json.dumps(payload, ensure_ascii=False),
                response_status,
                details,
                timestamp_ist_str(),
            ),
        )
    return log_id


async def dispatch_to_n8n(
    webhook_url: str,
    workflow: str,
    event_type: str,
    member_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Send an asynchronous JSON payload to an n8n webhook endpoint with audit logging."""
    settings = get_settings()
    url = (webhook_url or "").strip()
    if not url:
        log_id = log_automation_event(
            workflow=workflow,
            event_type=event_type,
            member_id=member_id,
            status="SKIPPED",
            payload=payload,
            response_status=None,
            details="Webhook URL not configured; logged locally.",
        )
        return {"dispatched": False, "status": "SKIPPED", "log_id": log_id}

    body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if settings.n8n_webhook_secret:
        headers["X-Crush-Signature"] = sign_payload(body_bytes, settings.n8n_webhook_secret)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, content=body_bytes, headers=headers)
        ok = 200 <= resp.status_code < 300
        status_label = "SENT" if ok else "FAILED"
        log_id = log_automation_event(
            workflow=workflow,
            event_type=event_type,
            member_id=member_id,
            status=status_label,
            payload=payload,
            response_status=resp.status_code,
            details=resp.text[:500],
        )
        return {
            "dispatched": ok,
            "status": status_label,
            "http_status": resp.status_code,
            "log_id": log_id,
        }
    except Exception as exc:
        log_id = log_automation_event(
            workflow=workflow,
            event_type=event_type,
            member_id=member_id,
            status="ERROR",
            payload=payload,
            response_status=None,
            details=str(exc),
        )
        return {
            "dispatched": False,
            "status": "ERROR",
            "error": str(exc),
            "log_id": log_id,
        }


def get_webhook_url_for_workflow(workflow_code: str) -> str:
    """Resolve the configured n8n webhook URL for GM-01 through GM-05."""
    settings = get_settings()
    mapping = {
        "GM-01": settings.n8n_gm01_sync_webhook_url,
        "GM-02": settings.n8n_gm02_dispatch_webhook_url,
        "GM-03": settings.n8n_gm03_payment_webhook_url,
        "GM-04": settings.n8n_gm04_digest_webhook_url,
        "GM-05": settings.n8n_gm05_agent_webhook_url,
    }
    return (mapping.get(workflow_code.upper(), "") or "").strip()


def _parse_cron_field(field: str, min_val: int, max_val: int, allow_sunday_7: bool = False) -> set[int]:
    """Parse a single 5-field cron component into allowed integer values."""
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"Empty cron segment in '{field}'.")
        step = 1
        base = part
        if "/" in part:
            base, step_str = part.split("/", 1)
            step = int(step_str)
            if step <= 0:
                raise ValueError(f"Cron step must be >= 1 in '{field}'.")
        if base == "*":
            start, end = min_val, max_val
        elif "-" in base:
            s_str, e_str = base.split("-", 1)
            start, end = int(s_str), int(e_str)
        else:
            start = end = int(base)

        if start > end or start < min_val or end > (7 if allow_sunday_7 else max_val):
            raise ValueError(f"Cron value out of range [{min_val}..{max_val}] in '{field}'.")
        for val in range(start, end + 1, step):
            values.add(0 if (allow_sunday_7 and val == 7) else val)
    return values


def _compute_next_cron_ist(cron_expr: str) -> str:
    """Compute the next Asia/Kolkata execution timestamp for a standard 5-field cron."""
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        raise ValueError("cron_expression must have exactly 5 fields: '<min> <hour> <dom> <month> <dow>'.")
    minutes = _parse_cron_field(fields[0], 0, 59)
    hours = _parse_cron_field(fields[1], 0, 23)
    doms = _parse_cron_field(fields[2], 1, 31)
    months = _parse_cron_field(fields[3], 1, 12)
    dows = _parse_cron_field(fields[4], 0, 6, allow_sunday_7=True)

    probe = now_ist().replace(second=0, microsecond=0) + timedelta(minutes=1)
    # Scan hour-by-hour up to 366 days so long-horizon crons stay fast in pure stdlib Python.
    for _ in range(366 * 24):
        cron_dow = (probe.weekday() + 1) % 7
        if (
            probe.month in months
            and probe.day in doms
            and cron_dow in dows
            and probe.hour in hours
        ):
            for minute in sorted(minutes):
                if minute >= probe.minute:
                    return probe.replace(minute=minute).strftime("%Y-%m-%d %H:%M:%S IST")
        probe = (probe + timedelta(hours=1)).replace(minute=0)
    raise ValueError(f"No upcoming execution found within 366 days for cron '{cron_expr}'.")


def _enrich_workflow_row(row: Any) -> dict[str, Any]:
    """Attach runtime readiness metadata to a workflow_registry row."""
    settings = get_settings()
    d = dict(row)
    url = get_webhook_url_for_workflow(d["workflow_code"])
    d["is_enabled"] = bool(d["is_enabled"])
    d["configured_webhook_url"] = url
    d["webhook_ready"] = bool(url)
    d["n8n_api_ready"] = bool(settings.n8n_base_url.strip() and settings.n8n_api_key.strip() and d["n8n_workflow_id"].strip())
    return d


def list_workflows() -> list[dict[str, Any]]:
    """Return all registered GM workflows ordered by workflow_code."""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM workflow_registry ORDER BY workflow_code ASC").fetchall()
    return [_enrich_workflow_row(r) for r in rows]


async def update_workflow_registry(workflow_code: str, req: WorkflowConfigUpdateRequest) -> dict[str, Any]:
    """Update workflow config locally and optionally activate/deactivate on remote n8n REST API."""
    code = workflow_code.strip().upper()
    now_str = timestamp_ist_str()

    if req.cron_expression is not None and req.cron_expression.strip():
        _compute_next_cron_ist(req.cron_expression.strip())

    with get_db() as conn:
        existing = conn.execute("SELECT * FROM workflow_registry WHERE workflow_code = ?", (code,)).fetchone()
        if not existing:
            raise KeyError(f"Workflow {code} not found.")

        updates: dict[str, Any] = {"updated_at_ist": now_str}
        if req.cron_expression is not None:
            updates["cron_expression"] = req.cron_expression.strip()
        if req.is_enabled is not None:
            updates["is_enabled"] = 1 if req.is_enabled else 0
        if req.n8n_workflow_id is not None:
            updates["n8n_workflow_id"] = req.n8n_workflow_id.strip()
        if req.webhook_path is not None:
            updates["webhook_path"] = req.webhook_path.strip()

        clauses = [f"{col} = ?" for col in updates]
        params = list(updates.values()) + [code]
        conn.execute(f"UPDATE workflow_registry SET {', '.join(clauses)} WHERE workflow_code = ?", params)
        updated = conn.execute("SELECT * FROM workflow_registry WHERE workflow_code = ?", (code,)).fetchone()

    result = _enrich_workflow_row(updated)
    settings = get_settings()
    if (
        req.is_enabled is not None
        and settings.n8n_base_url.strip()
        and settings.n8n_api_key.strip()
        and result["n8n_workflow_id"]
    ):
        action = "activate" if req.is_enabled else "deactivate"
        remote_url = f"{settings.n8n_base_url.rstrip('/')}/api/v1/workflows/{result['n8n_workflow_id']}/{action}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(remote_url, headers={"X-N8N-API-KEY": settings.n8n_api_key.strip()})
            result["n8n_remote_sync"] = {"synced": 200 <= resp.status_code < 300, "http_status": resp.status_code}
        except Exception as exc:
            result["n8n_remote_sync"] = {"synced": False, "error": str(exc)}
    return result


async def trigger_workflow(workflow_code: str, req: WorkflowTriggerRequest) -> dict[str, Any]:
    """Execute workflow business logic locally and/or dispatch to n8n webhook."""
    from ..config import today_ist_str
    from ..schemas import (
        AgentChatRequest,
        N8NFollowUpLogPayload,
        N8NMemberSyncPayload,
        PaymentRecordRequest,
    )
    from . import ai_agent_service, membership_service

    code = workflow_code.strip().upper()
    now_str = timestamp_ist_str()

    with get_db() as conn:
        wf = conn.execute("SELECT * FROM workflow_registry WHERE workflow_code = ?", (code,)).fetchone()
        if not wf:
            raise KeyError(f"Workflow {code} not found.")
        if not wf["is_enabled"]:
            raise ValueError(f"Workflow {code} is currently disabled.")

    local_res: dict[str, Any] = {"executed": False}
    if req.execute_local_pipeline:
        if code == "GM-01":
            if "phone" in req.payload and "name" in req.payload:
                member = membership_service.sync_member_from_n8n(N8NMemberSyncPayload(**req.payload))
                local_res = {"executed": True, "member": member.model_dump()}
            else:
                local_res = {"executed": True, "note": "Provide name + phone payload to sync member, or submit Form Trigger."}
        elif code == "GM-02":
            due = membership_service.evaluate_follow_ups()
            if req.payload.get("record_logs", True):
                today = today_ist_str()
                for item in due:
                    membership_service.record_followup_callback(
                        N8NFollowUpLogPayload(
                            member_id=item.member_id,
                            followup_type=item.followup_type,
                            channel=item.channel,
                            status="QUEUED_BY_CONTROL_PLANE",
                            message=item.message_text,
                            last_reminder_date=today,
                        )
                    )
            local_res = {"executed": True, "due_count": len(due), "follow_ups": [i.model_dump() for i in due]}
        elif code == "GM-03":
            if "member_id" in req.payload and "amount_paid" in req.payload:
                res = membership_service.record_payment(PaymentRecordRequest(**req.payload))
                if hasattr(res.get("member"), "model_dump"):
                    res["member"] = res["member"].model_dump()
                local_res = {"executed": True, **res}
            else:
                local_res = {"executed": True, "note": "Provide member_id + amount_paid to settle payment locally."}
        elif code == "GM-04":
            local_res = {"executed": True, **ai_agent_service.run_full_gym_autopilot()}
        elif code == "GM-05":
            turn = await ai_agent_service.run_agent_turn(
                AgentChatRequest(
                    message=str(req.payload.get("message") or "Run full gym autopilot"),
                    actor_role=str(req.payload.get("actor_role") or "owner"),
                    actor_phone=str(req.payload.get("actor_phone") or ""),
                    member_id=req.payload.get("member_id"),
                    sync_to_n8n=False,
                )
            )
            local_res = {"executed": True, **turn.model_dump()}

    n8n_res: dict[str, Any] = {"dispatched": False, "status": "SKIPPED"}
    if req.dispatch_n8n_webhook:
        n8n_res = await dispatch_to_n8n(
            webhook_url=get_webhook_url_for_workflow(code),
            workflow=code,
            event_type=f"API_TRIGGER:{req.triggered_by}",
            member_id=str(req.payload.get("member_id") or "STUDIO"),
            payload={
                "workflow_code": code,
                "triggered_by": req.triggered_by,
                "payload": req.payload,
                "local_result": local_res,
            },
        )

    final_status = "FAILED" if n8n_res.get("status") in ("FAILED", "ERROR") else "COMPLETED"
    with get_db() as conn:
        conn.execute(
            """
            UPDATE workflow_registry
            SET last_triggered_at_ist=?, last_status=?, total_runs=total_runs + 1, updated_at_ist=?
            WHERE workflow_code=?
            """,
            (now_str, final_status, now_str, code),
        )

    return {
        "workflow_code": code,
        "triggered_by": req.triggered_by,
        "local_result": local_res,
        "n8n_webhook": n8n_res,
        "timestamp_ist": now_str,
    }


def _decode_schedule_row(row: Any) -> dict[str, Any]:
    d = dict(row)
    d["payload"] = json.loads(d.pop("payload_json", "{}") or "{}")
    return d


def create_schedule(req: AutomationScheduleCreateRequest) -> dict[str, Any]:
    """Persist a recurring (cron / interval) or one-time schedule for an n8n workflow."""
    now_str = timestamp_ist_str()
    cron_expr = req.cron_expression.strip()
    run_at = req.run_at_ist.strip()

    if req.schedule_type == "cron":
        if not cron_expr:
            raise ValueError("cron_expression is required when schedule_type='cron'.")
        next_run = run_at or _compute_next_cron_ist(cron_expr)
    elif req.schedule_type == "one_time":
        if not run_at:
            raise ValueError("run_at_ist is required when schedule_type='one_time'.")
        next_run = run_at
    else:
        if req.interval_minutes <= 0:
            raise ValueError("interval_minutes must be > 0 when schedule_type='interval_minutes'.")
        next_run = run_at or (now_ist() + timedelta(minutes=req.interval_minutes)).strftime("%Y-%m-%d %H:%M:%S IST")

    job_id = f"SCH-{secrets.token_hex(4).upper()}"
    with get_db() as conn:
        wf = conn.execute(
            "SELECT 1 FROM workflow_registry WHERE workflow_code = ?",
            (req.workflow_code,),
        ).fetchone()
        if not wf:
            raise KeyError(f"Workflow {req.workflow_code} not found.")
        conn.execute(
            """
            INSERT INTO scheduled_automation_jobs (
                job_id, workflow_code, job_name, schedule_type, cron_expression,
                run_at_ist, interval_minutes, payload_json, status,
                last_run_at_ist, next_run_at_ist, run_count, created_by,
                created_at_ist, updated_at_ist
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', '', ?, 0, ?, ?, ?)
            """,
            (
                job_id,
                req.workflow_code,
                req.job_name.strip(),
                req.schedule_type,
                cron_expr,
                run_at,
                req.interval_minutes,
                json.dumps(req.payload, ensure_ascii=False),
                next_run,
                req.created_by.strip() or "NextJS_Client",
                now_str,
                now_str,
            ),
        )
        row = conn.execute("SELECT * FROM scheduled_automation_jobs WHERE job_id = ?", (job_id,)).fetchone()
    return _decode_schedule_row(row)


def list_schedules(status: str | None = None) -> list[dict[str, Any]]:
    """List scheduled automation jobs ordered newest first."""
    with get_db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM scheduled_automation_jobs WHERE status = ? ORDER BY created_at_ist DESC",
                (status.strip().upper(),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM scheduled_automation_jobs ORDER BY created_at_ist DESC"
            ).fetchall()
    return [_decode_schedule_row(r) for r in rows]


def update_schedule(job_id: str, req: AutomationScheduleUpdateRequest) -> dict[str, Any]:
    """Pause, resume, cancel, or modify a scheduled workflow job."""
    jid = job_id.strip()
    now_str = timestamp_ist_str()

    with get_db() as conn:
        existing = conn.execute("SELECT * FROM scheduled_automation_jobs WHERE job_id = ?", (jid,)).fetchone()
        if not existing:
            raise KeyError(f"Schedule {jid} not found.")

        updates: dict[str, Any] = {"updated_at_ist": now_str}
        if req.status is not None:
            updates["status"] = req.status
        if req.cron_expression is not None:
            expr = req.cron_expression.strip()
            updates["cron_expression"] = expr
            if expr:
                updates["next_run_at_ist"] = _compute_next_cron_ist(expr)
        if req.run_at_ist is not None:
            updates["run_at_ist"] = req.run_at_ist.strip()
            if req.run_at_ist.strip():
                updates["next_run_at_ist"] = req.run_at_ist.strip()
        if req.interval_minutes is not None:
            updates["interval_minutes"] = req.interval_minutes
            if req.interval_minutes > 0:
                updates["next_run_at_ist"] = (now_ist() + timedelta(minutes=req.interval_minutes)).strftime(
                    "%Y-%m-%d %H:%M:%S IST"
                )
        if req.payload is not None:
            updates["payload_json"] = json.dumps(req.payload, ensure_ascii=False)

        clauses = [f"{col} = ?" for col in updates]
        params = list(updates.values()) + [jid]
        conn.execute(f"UPDATE scheduled_automation_jobs SET {', '.join(clauses)} WHERE job_id = ?", params)
        updated = conn.execute("SELECT * FROM scheduled_automation_jobs WHERE job_id = ?", (jid,)).fetchone()
    return _decode_schedule_row(updated)


async def run_scheduled_job_now(job_id: str, triggered_by: str = "Schedule_Runner") -> dict[str, Any]:
    """Execute a scheduled job immediately and advance its next_run_at_ist."""
    jid = job_id.strip()
    with get_db() as conn:
        job = conn.execute("SELECT * FROM scheduled_automation_jobs WHERE job_id = ?", (jid,)).fetchone()
        if not job:
            raise KeyError(f"Schedule {jid} not found.")
        if job["status"] == "CANCELLED":
            raise ValueError(f"Schedule {jid} is cancelled and cannot run.")

    payload = json.loads(job["payload_json"] or "{}")
    trigger_result = await trigger_workflow(
        job["workflow_code"],
        WorkflowTriggerRequest(
            payload=payload,
            execute_local_pipeline=True,
            dispatch_n8n_webhook=True,
            triggered_by=f"{triggered_by}:{jid}",
        ),
    )

    now_str = timestamp_ist_str()
    next_status = job["status"]
    next_run = job["next_run_at_ist"]
    if job["schedule_type"] == "one_time":
        next_status = "COMPLETED"
        next_run = ""
    elif job["schedule_type"] == "interval_minutes" and int(job["interval_minutes"]) > 0:
        next_run = (now_ist() + timedelta(minutes=int(job["interval_minutes"]))).strftime("%Y-%m-%d %H:%M:%S IST")
    elif job["schedule_type"] == "cron" and job["cron_expression"]:
        next_run = _compute_next_cron_ist(job["cron_expression"])

    with get_db() as conn:
        conn.execute(
            """
            UPDATE scheduled_automation_jobs
            SET status=?, last_run_at_ist=?, next_run_at_ist=?, run_at_ist='',
                run_count=run_count + 1, updated_at_ist=?
            WHERE job_id=?
            """,
            (next_status, now_str, next_run, now_str, jid),
        )
        updated = conn.execute("SELECT * FROM scheduled_automation_jobs WHERE job_id = ?", (jid,)).fetchone()

    return {
        "job_id": jid,
        "workflow_code": job["workflow_code"],
        "schedule": _decode_schedule_row(updated),
        "trigger_result": trigger_result,
    }


async def tick_due_schedules() -> dict[str, Any]:
    """Find and execute all ACTIVE scheduled jobs due as of the current IST timestamp."""
    now_str = timestamp_ist_str()
    with get_db() as conn:
        due_rows = conn.execute(
            """
            SELECT job_id FROM scheduled_automation_jobs
            WHERE status = 'ACTIVE'
              AND (
                (next_run_at_ist != '' AND next_run_at_ist <= ?)
                OR (run_at_ist != '' AND run_at_ist <= ?)
              )
            ORDER BY created_at_ist ASC
            """,
            (now_str, now_str),
        ).fetchall()

    executions = []
    for r in due_rows:
        executions.append(await run_scheduled_job_now(r["job_id"], triggered_by="Schedule_Tick"))
    return {
        "checked_at_ist": now_str,
        "executed_count": len(executions),
        "executions": executions,
    }


async def retry_automation_event(log_id: str) -> dict[str, Any]:
    """Replay a historical automation log event through n8n webhook dispatch."""
    lid = log_id.strip()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM automation_logs WHERE log_id = ?", (lid,)).fetchone()
        if not row:
            raise KeyError(f"Automation log {lid} not found.")

    payload = json.loads(row["payload_json"] or "{}")
    url = get_webhook_url_for_workflow(row["workflow"])
    res = await dispatch_to_n8n(
        webhook_url=url,
        workflow=row["workflow"],
        event_type=f"RETRY:{row['event_type']}",
        member_id=row["member_id"] or "STUDIO",
        payload=payload,
    )
    return {"retried_log_id": lid, "workflow": row["workflow"], "result": res}


def record_workflow_callback(payload: N8NWorkflowCallbackPayload) -> dict[str, Any]:
    """Record an inbound workflow execution completion callback from n8n."""
    now_str = timestamp_ist_str()
    with get_db() as conn:
        existing = conn.execute(
            "SELECT 1 FROM workflow_registry WHERE workflow_code = ?",
            (payload.workflow_code,),
        ).fetchone()
        if not existing:
            raise KeyError(f"Workflow {payload.workflow_code} not found.")
        conn.execute(
            """
            UPDATE workflow_registry
            SET last_triggered_at_ist=?, last_status=?, total_runs=total_runs + 1, updated_at_ist=?
            WHERE workflow_code=?
            """,
            (now_str, payload.status, now_str, payload.workflow_code),
        )

    log_id = log_automation_event(
        workflow=payload.workflow_code,
        event_type=f"N8N_CALLBACK:{payload.execution_id or 'WEBHOOK'}",
        member_id=payload.member_id or "STUDIO",
        status=payload.status,
        payload=payload.metrics,
        response_status=200,
        details=payload.summary[:400],
    )
    return {
        "acknowledged": True,
        "log_id": log_id,
        "workflow_code": payload.workflow_code,
        "status": payload.status,
        "timestamp_ist": now_str,
    }


def _resolve_workflows_dir() -> Path:
    """Resolve workflows directory inside backend/, Docker /app/workflows, or parent repo."""
    candidates = [
        Path(__file__).resolve().parents[2] / "workflows",
        Path(__file__).resolve().parents[3] / "workflows",
        Path(__file__).resolve().parents[1] / "workflows",
    ]
    for cand in candidates:
        if cand.exists():
            return cand
    return candidates[0]


WORKFLOWS_DIR = _resolve_workflows_dir()
GM_TEMPLATE_FILES: dict[str, str] = {
    "GM-01": "GM-01-member-registration.json",
    "GM-02": "GM-02-expiry-and-followup-cadence.json",
    "GM-03": "GM-03-payment-and-renewal-webhook.json",
    "GM-04": "GM-04-attendance-streak-and-owner-digest.json",
    "GM-05": "GM-05-ai-gym-manager-agent.json",
}


def get_env_readiness_checklist() -> dict[str, Any]:
    """Inspect .env variables and report readiness for Google Sheets DB & auto n8n deployment."""
    settings = get_settings()

    def _is_set(val: str, placeholder_prefix: str = "REPLACE_WITH") -> bool:
        cleaned = (val or "").strip()
        return bool(cleaned and not cleaned.startswith(placeholder_prefix) and "your-" not in cleaned.lower())

    missing_for_live_n8n_deploy: list[str] = []
    if not _is_set(settings.n8n_base_url):
        missing_for_live_n8n_deploy.append("N8N_BASE_URL")
    if not _is_set(settings.n8n_api_key):
        missing_for_live_n8n_deploy.append("N8N_API_KEY")
    if not _is_set(settings.google_sheets_spreadsheet_id):
        missing_for_live_n8n_deploy.append("GOOGLE_SHEETS_SPREADSHEET_ID")

    return {
        "ready_for_local_sheets_mode": True,
        "ready_for_live_n8n_deploy": len(missing_for_live_n8n_deploy) == 0,
        "missing_for_live_n8n_deploy": missing_for_live_n8n_deploy,
        "google_sheets_database": {
            "GYMBRO_STORAGE_BACKEND": settings.storage_backend,
            "GOOGLE_SHEETS_MODE": settings.google_sheets_mode,
            "GOOGLE_SHEETS_SPREADSHEET_ID": {
                "configured": _is_set(settings.google_sheets_spreadsheet_id),
                "value": settings.google_sheets_spreadsheet_id,
            },
            "GOOGLE_SHEETS_SPREADSHEET_NAME": settings.google_sheets_spreadsheet_name,
            "GOOGLE_SHEETS_WORKBOOK_PATH": settings.google_sheets_workbook_path,
            "GOOGLE_SHEETS_BRIDGE_WEBHOOK_URL": _is_set(settings.google_sheets_bridge_webhook_url),
            "GOOGLE_API_KEY": _is_set(settings.google_api_key),
            "GOOGLE_OAUTH_ACCESS_TOKEN": _is_set(settings.google_oauth_access_token),
        },
        "n8n_control_plane": {
            "N8N_BASE_URL": _is_set(settings.n8n_base_url),
            "N8N_API_KEY": _is_set(settings.n8n_api_key),
            "N8N_GOOGLE_SHEETS_CREDENTIAL_ID": _is_set(settings.n8n_google_sheets_credential_id),
            "N8N_WHATSAPP_CREDENTIAL_ID": _is_set(settings.n8n_whatsapp_credential_id),
            "N8N_WEBHOOK_SECRET": _is_set(settings.n8n_webhook_secret),
            "N8N_GM01_SYNC_WEBHOOK_URL": _is_set(settings.n8n_gm01_sync_webhook_url),
            "N8N_GM02_DISPATCH_WEBHOOK_URL": _is_set(settings.n8n_gm02_dispatch_webhook_url),
            "N8N_GM03_PAYMENT_WEBHOOK_URL": _is_set(settings.n8n_gm03_payment_webhook_url),
            "N8N_GM04_DIGEST_WEBHOOK_URL": _is_set(settings.n8n_gm04_digest_webhook_url),
            "N8N_GM05_AGENT_WEBHOOK_URL": _is_set(settings.n8n_gm05_agent_webhook_url),
        },
        "whatsapp_and_payments": {
            "WHATSAPP_PHONE_NUMBER_ID": _is_set(settings.whatsapp_phone_number_id),
            "RAZORPAY_WEBHOOK_SECRET": _is_set(settings.razorpay_webhook_secret),
        },
        "ai_agent_and_nextjs": {
            "FASTAPI_PUBLIC_URL": settings.fastapi_public_url,
            "CORS_ORIGINS": settings.cors_origins,
            "LLM_API_KEY": _is_set(settings.llm_api_key),
            "LLM_MODEL": settings.llm_model,
        },
    }


def _replace_placeholders_recursive(
    obj: Any,
    sheet_id: str,
    wa_phone_id: str,
    fastapi_url: str,
) -> Any:
    if isinstance(obj, str):
        updated = obj.replace("REPLACE_WITH_GOOGLE_SHEET_ID", sheet_id)
        updated = updated.replace("REPLACE_WITH_WHATSAPP_PHONE_NUMBER_ID", wa_phone_id)
        if fastapi_url and "http://localhost:8000" in updated:
            updated = updated.replace("http://localhost:8000", fastapi_url.rstrip("/"))
        return updated
    if isinstance(obj, list):
        return [_replace_placeholders_recursive(item, sheet_id, wa_phone_id, fastapi_url) for item in obj]
    if isinstance(obj, dict):
        return {k: _replace_placeholders_recursive(v, sheet_id, wa_phone_id, fastapi_url) for k, v in obj.items()}
    return obj


def generate_configured_n8n_workflows(
    workflow_codes: list[str] | None = None,
    spreadsheet_id: str | None = None,
    fastapi_url: str | None = None,
    whatsapp_phone_number_id: str | None = None,
    inject_credentials: bool = False,
) -> dict[str, Any]:
    """Generate customized n8n workflow JSONs (GM-01..GM-05) from .env and workflow_registry."""
    settings = get_settings()
    target_sheet_id = (spreadsheet_id or settings.google_sheets_spreadsheet_id or "REPLACE_WITH_GOOGLE_SHEET_ID").strip()
    target_fastapi = (fastapi_url or settings.fastapi_public_url or "http://localhost:8000").strip()
    target_wa = (whatsapp_phone_number_id or settings.whatsapp_phone_number_id or "REPLACE_WITH_WHATSAPP_PHONE_NUMBER_ID").strip()

    codes = [c.upper() for c in (workflow_codes or list(GM_TEMPLATE_FILES.keys()))]
    registry_by_code = {w["workflow_code"]: w for w in list_workflows()}

    generated: dict[str, Any] = {}
    for code in codes:
        fname = GM_TEMPLATE_FILES.get(code)
        if not fname:
            raise KeyError(f"Unknown workflow code '{code}'.")
        template_path = WORKFLOWS_DIR / fname
        if not template_path.exists():
            raise FileNotFoundError(f"Workflow template {template_path} not found.")

        raw_wf = json.loads(template_path.read_text(encoding="utf-8"))
        wf = _replace_placeholders_recursive(raw_wf, target_sheet_id, target_wa, target_fastapi)

        reg = registry_by_code.get(code) or {}
        custom_webhook_slug = (reg.get("webhook_path") or "").replace("/webhook/", "").lstrip("/")
        custom_cron = (reg.get("cron_expression") or "").strip()

        for node in wf.get("nodes") or []:
            node_type = node.get("type", "")
            params = node.setdefault("parameters", {})
            if node_type in ("n8n-nodes-base.webhook", "n8n-nodes-base.formTrigger") and custom_webhook_slug:
                params["path"] = custom_webhook_slug
            if node_type == "n8n-nodes-base.scheduleTrigger" and custom_cron:
                rules = params.get("rule", {}).get("interval", [])
                if rules and isinstance(rules, list) and isinstance(rules[0], dict):
                    rules[0]["field"] = "cronExpression"
                    rules[0]["expression"] = custom_cron
            if inject_credentials:
                if node_type == "n8n-nodes-base.googleSheets" and settings.n8n_google_sheets_credential_id.strip():
                    node["credentials"] = {
                        "googleSheetsOAuth2Api": {
                            "id": settings.n8n_google_sheets_credential_id.strip(),
                            "name": settings.n8n_google_sheets_credential_name,
                        }
                    }
                if node_type == "n8n-nodes-base.gmail" and settings.n8n_gmail_credential_id.strip():
                    node["credentials"] = {
                        "gmailOAuth2": {
                            "id": settings.n8n_gmail_credential_id.strip(),
                            "name": settings.n8n_gmail_credential_name,
                        }
                    }
                if node_type == "n8n-nodes-base.whatsApp" and settings.n8n_whatsapp_credential_id.strip():
                    node["credentials"] = {
                        "whatsAppApi": {
                            "id": settings.n8n_whatsapp_credential_id.strip(),
                            "name": settings.n8n_whatsapp_credential_name,
                        }
                    }

        generated[code] = wf

    return {
        "spreadsheet_id": target_sheet_id,
        "fastapi_url": target_fastapi,
        "whatsapp_phone_number_id": target_wa,
        "credentials_injected": inject_credentials,
        "count": len(generated),
        "workflows": generated,
    }


def _to_n8n_api_payload(wf: dict[str, Any]) -> dict[str, Any]:
    """Strip read-only/export-only top-level fields rejected by n8n Public REST API v1."""
    allowed_settings = ("executionOrder", "timezone", "saveManualExecutions", "callerPolicy", "errorWorkflow")
    clean_settings = {
        k: v
        for k, v in (wf.get("settings") or {}).items()
        if k in allowed_settings
    }
    return {
        "name": wf["name"],
        "nodes": wf["nodes"],
        "connections": wf["connections"],
        "settings": clean_settings,
    }


async def deploy_workflows_to_n8n(
    workflow_codes: list[str] | None = None,
    activate: bool = False,
    dry_run: bool = False,
    spreadsheet_id: str | None = None,
    fastapi_url: str | None = None,
    whatsapp_phone_number_id: str | None = None,
) -> dict[str, Any]:
    """Automatically generate and deploy GM workflows to n8n REST API using N8N_API_KEY."""
    settings = get_settings()
    checklist = get_env_readiness_checklist()
    has_n8n_auth = bool(
        settings.n8n_base_url.strip()
        and "your-" not in settings.n8n_base_url.lower()
        and settings.n8n_api_key.strip()
    )
    gen = generate_configured_n8n_workflows(
        workflow_codes=workflow_codes,
        spreadsheet_id=spreadsheet_id,
        fastapi_url=fastapi_url,
        whatsapp_phone_number_id=whatsapp_phone_number_id,
        inject_credentials=bool(not dry_run and has_n8n_auth),
    )

    registry_by_code = {w["workflow_code"]: w for w in list_workflows()}
    base_url = settings.n8n_base_url.rstrip("/")

    if dry_run or not has_n8n_auth:
        planned = []
        for code, wf in gen["workflows"].items():
            existing_id = (registry_by_code.get(code) or {}).get("n8n_workflow_id", "")
            method = "PUT" if existing_id else "POST"
            endpoint = f"{base_url}/api/v1/workflows/{existing_id}" if existing_id else f"{base_url}/api/v1/workflows"
            planned.append(
                {
                    "workflow_code": code,
                    "method": method,
                    "endpoint": endpoint,
                    "activate_after_deploy": activate,
                    "workflow_name": wf.get("name"),
                    "node_count": len(wf.get("nodes") or []),
                    "workflow_payload": _to_n8n_api_payload(wf),
                }
            )
        return {
            "mode": "dry_run",
            "deployed": False,
            "missing_env_vars": checklist["missing_for_live_n8n_deploy"],
            "spreadsheet_id": gen["spreadsheet_id"],
            "fastapi_url": gen["fastapi_url"],
            "planned_count": len(planned),
            "planned_deployments": planned,
        }

    headers = {
        "X-N8N-API-KEY": settings.n8n_api_key.strip(),
        "Content-Type": "application/json",
    }
    deployed_results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=20.0) as client:
        remote_by_name: dict[str, str] = {}
        cred_by_type: dict[str, dict[str, str]] = {}
        try:
            list_resp = await client.get(f"{base_url}/api/v1/workflows?limit=250", headers=headers)
            if 200 <= list_resp.status_code < 300:
                for item in list_resp.json().get("data") or []:
                    if item.get("name") and item.get("id"):
                        remote_by_name[str(item["name"])] = str(item["id"])
        except Exception:
            pass
        try:
            cred_resp = await client.get(f"{base_url}/api/v1/credentials?limit=100", headers=headers)
            if 200 <= cred_resp.status_code < 300:
                for cred in cred_resp.json().get("data") or []:
                    ctype = str(cred.get("type") or "")
                    if ctype and cred.get("id") and ctype not in cred_by_type:
                        cred_by_type[ctype] = {"id": str(cred["id"]), "name": str(cred.get("name") or ctype)}
        except Exception:
            pass

        for code, wf in gen["workflows"].items():
            for node in wf.get("nodes") or []:
                ntype = node.get("type", "")
                if ntype == "n8n-nodes-base.googleSheets" and "googleSheetsOAuth2Api" not in (node.get("credentials") or {}):
                    if "googleSheetsOAuth2Api" in cred_by_type:
                        node["credentials"] = {"googleSheetsOAuth2Api": cred_by_type["googleSheetsOAuth2Api"]}
                if ntype == "n8n-nodes-base.gmail" and "gmailOAuth2" not in (node.get("credentials") or {}):
                    if "gmailOAuth2" in cred_by_type:
                        node["credentials"] = {"gmailOAuth2": cred_by_type["gmailOAuth2"]}

            existing_id = (
                remote_by_name.get(str(wf.get("name") or ""))
                or (registry_by_code.get(code) or {}).get("n8n_workflow_id", "")
            ).strip()
            api_payload = _to_n8n_api_payload(wf)
            try:
                if existing_id:
                    resp = await client.put(f"{base_url}/api/v1/workflows/{existing_id}", headers=headers, json=api_payload)
                else:
                    resp = await client.post(f"{base_url}/api/v1/workflows", headers=headers, json=api_payload)

                if 200 <= resp.status_code < 300:
                    remote_obj = resp.json()
                    remote_id = str(remote_obj.get("id") or existing_id)
                    activated = False
                    if activate and remote_id:
                        act_resp = await client.post(f"{base_url}/api/v1/workflows/{remote_id}/activate", headers=headers)
                        activated = 200 <= act_resp.status_code < 300

                    await update_workflow_registry(
                        code,
                        WorkflowConfigUpdateRequest(n8n_workflow_id=remote_id, is_enabled=activated or None),
                    )
                    log_automation_event(
                        workflow=code,
                        event_type="N8N_AUTO_DEPLOY",
                        member_id="STUDIO",
                        status="DEPLOYED",
                        payload={"remote_id": remote_id, "activated": activated},
                        response_status=resp.status_code,
                        details=f"Deployed {code} to n8n ({remote_id}).",
                    )
                    deployed_results.append(
                        {"workflow_code": code, "status": "DEPLOYED", "n8n_workflow_id": remote_id, "activated": activated}
                    )
                else:
                    deployed_results.append(
                        {"workflow_code": code, "status": "FAILED", "http_status": resp.status_code, "error": resp.text[:300]}
                    )
            except Exception as exc:
                deployed_results.append({"workflow_code": code, "status": "ERROR", "error": str(exc)})

    return {
        "mode": "live",
        "deployed": all(r["status"] == "DEPLOYED" for r in deployed_results),
        "n8n_base_url": base_url,
        "results": deployed_results,
    }


async def sync_sheets_with_remote(
    direction: Literal["push", "pull"] = "push",
    workbook_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Push the local Google Sheets mirror to Google Sheets or pull/hydrate from Google Sheets."""
    settings = get_settings()
    if direction == "pull":
        if workbook_payload is not None:
            snapshot = load_sheets_workbook_snapshot(workbook_payload)
            return {
                "direction": "pull",
                "source": "request_payload",
                "counts": snapshot["counts"],
                "workbook_path": snapshot["workbook_path"],
                "updated_at_ist": snapshot["updated_at_ist"],
            }
        if settings.google_sheets_bridge_webhook_url.strip():
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    settings.google_sheets_bridge_webhook_url.strip(),
                    json={"action": "pull", "spreadsheet_id": settings.google_sheets_spreadsheet_id},
                )
            if 200 <= resp.status_code < 300:
                snapshot = load_sheets_workbook_snapshot(resp.json())
                return {
                    "direction": "pull",
                    "source": "n8n_bridge",
                    "counts": snapshot["counts"],
                    "workbook_path": snapshot["workbook_path"],
                }
        snapshot = load_sheets_workbook_snapshot(None)
        return {
            "direction": "pull",
            "source": "local_workbook_file",
            "counts": snapshot["counts"],
            "workbook_path": snapshot["workbook_path"],
        }

    # direction == "push"
    snapshot = save_sheets_workbook_snapshot()
    bridge_url = settings.google_sheets_bridge_webhook_url.strip()
    if bridge_url:
        dispatch_res = await dispatch_to_n8n(
            webhook_url=bridge_url,
            workflow="SHEETS-SYNC",
            event_type="WORKBOOK_PUSH",
            member_id="STUDIO",
            payload={
                "spreadsheet_id": settings.google_sheets_spreadsheet_id,
                "tabs": snapshot["tabs"],
            },
        )
        return {
            "direction": "push",
            "mode": settings.google_sheets_mode,
            "synced_remote": dispatch_res.get("dispatched", False),
            "remote_dispatch": dispatch_res,
            "counts": snapshot["counts"],
            "workbook_path": snapshot["workbook_path"],
        }

    if (
        settings.google_sheets_mode == "google_api"
        and settings.google_oauth_access_token.strip()
        and settings.google_sheets_spreadsheet_id
        and not settings.google_sheets_spreadsheet_id.startswith("REPLACE_WITH")
    ):
        value_ranges = []
        for tab_name in ("Members", "Membership Plans", "Payments", "Attendance", "Automation Log", "Gym Settings"):
            rows = snapshot["tabs"].get(tab_name) or []
            if rows:
                headers_row = list(rows[0].keys())
                values = [headers_row] + [[row.get(col, "") for col in headers_row] for row in rows]
                value_ranges.append({"range": f"'{tab_name}'!A1", "values": values})
        if value_ranges:
            api_url = f"https://sheets.googleapis.com/v4/spreadsheets/{settings.google_sheets_spreadsheet_id}/values:batchUpdate"
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    api_url,
                    headers={"Authorization": f"Bearer {settings.google_oauth_access_token.strip()}"},
                    json={"valueInputOption": "USER_ENTERED", "data": value_ranges},
                )
            return {
                "direction": "push",
                "mode": "google_api",
                "synced_remote": 200 <= resp.status_code < 300,
                "http_status": resp.status_code,
                "counts": snapshot["counts"],
                "workbook_path": snapshot["workbook_path"],
            }

    return {
        "direction": "push",
        "mode": "local_workbook_flushed",
        "synced_remote": False,
        "counts": snapshot["counts"],
        "workbook_path": snapshot["workbook_path"],
        "note": "Saved all Google Sheets tabs locally; configure GOOGLE_SHEETS_BRIDGE_WEBHOOK_URL or GOOGLE_OAUTH_ACCESS_TOKEN for remote push.",
    }
