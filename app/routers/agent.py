"""Autonomous AI Gym Manager & Accountant agent endpoints."""

from __future__ import annotations

import json

from fastapi import APIRouter

from ..database import get_db
from ..schemas import AgentChatRequest, AgentChatResponse
from ..services import ai_agent_service

router = APIRouter(prefix="/api/v1/agent", tags=["AI Gym Manager Agent"])


@router.post("/chat", response_model=AgentChatResponse)
async def chat_with_ai_gym_manager(payload: AgentChatRequest):
    """Execute an agentic tool-calling turn for memberships, follow-ups, or accounts."""
    return await ai_agent_service.run_agent_turn(payload)


@router.post("/autonomous-cycle", response_model=AgentChatResponse)
async def run_autonomous_cycle():
    """One-shot full gym autopilot triggered by n8n GM-02/GM-04/GM-05 or owner dashboard."""
    req = AgentChatRequest(
        message="Run full gym autopilot: update expired/unfrozen statuses, evaluate and queue retention follow-ups, and compile CFO P&L summary.",
        actor_role="n8n_autopilot",
        sync_to_n8n=True,
    )
    return await ai_agent_service.run_agent_turn(req)


@router.get("/runs")
def list_agent_runs():
    """Return audit history of recent autonomous AI agent executions."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_runs ORDER BY created_at_ist DESC LIMIT 50"
        ).fetchall()
    runs = []
    for r in rows:
        d = dict(r)
        d["tools_called"] = json.loads(d.pop("tools_called_json", "[]"))
        d["sheets_sync"] = json.loads(d.pop("sheets_sync_json", "{}"))
        runs.append(d)
    return {"count": len(runs), "runs": runs}
