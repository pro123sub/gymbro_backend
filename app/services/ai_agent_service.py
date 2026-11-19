"""Autonomous AI Gym Manager & CFO Agent for Crush Lifestyle Fitness Studio."""

from __future__ import annotations

import json
import re
import secrets
from typing import Any

import httpx

from ..config import get_settings, timestamp_ist_str, today_ist_str
from ..database import get_db
from ..schemas import (
    VALID_EXPENSE_CATEGORIES,
    AgentChatRequest,
    AgentChatResponse,
    AgentToolExecution,
    AttendanceCheckInRequest,
    ExpenseCreateRequest,
    FreezeMembershipRequest,
    MemberCreateRequest,
    N8NFollowUpLogPayload,
    PaymentRecordRequest,
    normalize_indian_mobile,
)
from . import accounts_service, membership_service
from .n8n_client import dispatch_to_n8n, log_automation_event

MEMBER_ALLOWED_TOOLS = {
    "lookup_member_360",
    "checkin_member_attendance",
    "freeze_member_plan",
    "unfreeze_member_plan",
    "get_streak_leaderboard",
}

AGENT_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_accounts_and_pnl_summary",
            "description": "Retrieve complete gym CFO accounts, gross collections, CGST/SGST liability, expenses, net profit, and pending receivables aging.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_gym_expense",
            "description": "Record a studio operating expense such as Rent, Trainer Salaries, Electricity & Utilities, Equipment Maintenance, or Marketing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": sorted(VALID_EXPENSE_CATEGORIES)},
                    "amount": {"type": "number"},
                    "vendor": {"type": "string"},
                    "payment_mode": {"type": "string", "enum": ["UPI", "Cash", "Card", "Bank Transfer"]},
                    "notes": {"type": "string"},
                },
                "required": ["category", "amount"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_member_360",
            "description": "Lookup a gym member by member_id (MEM-XXXXXXXX), phone number, or name.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "register_new_member",
            "description": "Register a new gym member with phone normalization, MEM-XXXXXXXX generation, expiry calculation, and GST receipt.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "phone": {"type": "string"},
                    "plan": {"type": "string", "enum": ["Monthly", "Quarterly", "Half-Yearly", "Yearly"]},
                    "fee": {"type": "number"},
                    "payment_status": {"type": "string", "enum": ["Paid", "Pending"]},
                    "start_date": {"type": "string"},
                },
                "required": ["name", "phone", "plan", "fee", "payment_status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_member_payment",
            "description": "Record a payment or renewal for a member by member_id or phone and update Google Sheets + GST invoice.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member_id": {"type": "string"},
                    "amount_paid": {"type": "number"},
                    "payment_mode": {"type": "string", "enum": ["UPI", "Cash", "Card", "Razorpay", "Bank Transfer"]},
                },
                "required": ["member_id", "amount_paid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "freeze_member_plan",
            "description": "Pause/freeze an active member's plan within quota and extend their expiry_date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member_id": {"type": "string"},
                    "freeze_days": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["member_id", "freeze_days"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unfreeze_member_plan",
            "description": "Resume/unfreeze a frozen member's plan early, reclaiming unused freeze days from expiry_date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["member_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "checkin_member_attendance",
            "description": "Check in a member by member_id or phone and update consecutive workout streak.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member_id": {"type": "string"},
                    "phone": {"type": "string"},
                    "workout_type": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_streak_leaderboard",
            "description": "Fetch the top studio workout streak leaderboard ranked by current_streak and longest_streak.",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "evaluate_and_dispatch_followups",
            "description": "Evaluate Cult.fit-grade retention follow-ups (Onboarding, Pending Dues, Expiry T-7/T-3/T-0, Inactivity Drop-Off, Win-Back) and mark logs.",
            "parameters": {
                "type": "object",
                "properties": {"dispatch": {"type": "boolean"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_full_gym_autopilot",
            "description": "Execute the full autonomous cycle: refresh Expired/Unfrozen statuses, evaluate & log retention follow-ups, and generate CFO Accounts + KPI report.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def _resolve_member_id(identifier: str) -> str | None:
    """Resolve MEM-XXXXXXXX, phone number, or partial name to a member_id."""
    q = (identifier or "").strip()
    if not q:
        return None
    with get_db() as conn:
        if q.upper().startswith("MEM-"):
            row = conn.execute("SELECT member_id FROM members WHERE UPPER(member_id) = ?", (q.upper(),)).fetchone()
            if row:
                return row["member_id"]
        digits = re.sub(r"\D", "", q)
        if len(digits) >= 10:
            try:
                norm = normalize_indian_mobile(digits)
                row = conn.execute("SELECT member_id FROM members WHERE phone = ?", (norm,)).fetchone()
                if row:
                    return row["member_id"]
            except ValueError:
                pass
        row = conn.execute(
            "SELECT member_id FROM members WHERE LOWER(name) LIKE ? ORDER BY created_at_ist DESC LIMIT 1",
            (f"%{q.lower()}%",),
        ).fetchone()
        return row["member_id"] if row else None


def run_full_gym_autopilot() -> dict[str, Any]:
    """Run status housekeeping, follow-up evaluation, and CFO financial summary."""
    today = today_ist_str()
    now_str = timestamp_ist_str()
    status_updates: list[dict[str, str]] = []

    with get_db() as conn:
        rows = conn.execute("SELECT * FROM members").fetchall()
        for r in rows:
            if r["member_status"] == "Frozen" and r["frozen_until"] and r["frozen_until"] <= today:
                new_status = "Expired" if r["expiry_date"] < today else "Active"
                conn.execute(
                    "UPDATE members SET member_status=?, frozen_until='', updated_at_ist=? WHERE member_id=?",
                    (new_status, now_str, r["member_id"]),
                )
                status_updates.append({"member_id": r["member_id"], "status": new_status})
            elif r["member_status"] == "Active" and r["expiry_date"] < today:
                conn.execute(
                    "UPDATE members SET member_status='Expired', updated_at_ist=? WHERE member_id=?",
                    (now_str, r["member_id"]),
                )
                status_updates.append({"member_id": r["member_id"], "status": "Expired"})

    due_followups = membership_service.evaluate_follow_ups(include_already_reminded_today=False)
    for item in due_followups:
        membership_service.record_followup_callback(
            N8NFollowUpLogPayload(
                member_id=item.member_id,
                followup_type=item.followup_type,
                channel=item.channel,
                status="QUEUED_BY_AUTOPILOT",
                message=item.message_text,
                last_reminder_date=today,
            )
        )

    dashboard = membership_service.get_dashboard_analytics()
    accounts = accounts_service.get_accounts_summary()

    return {
        "as_of_ist": now_str,
        "status_transitions": status_updates,
        "followups_queued": [f.model_dump() for f in due_followups],
        "followups_queued_count": len(due_followups),
        "operations_kpis": dashboard.model_dump(exclude={"recent_automation_logs"}),
        "accounts_summary": accounts.model_dump(exclude={"recent_payments", "recent_expenses"}),
    }


def _execute_tool(
    tool_name: str,
    arguments: dict[str, Any],
    actor_role: str,
    actor_phone: str,
    scoped_member_id: str | None,
) -> dict[str, Any]:
    """Execute one agent tool with role-based scoping."""
    if actor_role == "member" and tool_name not in MEMBER_ALLOWED_TOOLS:
        return {"error": f"Tool '{tool_name}' is restricted to studio staff/owner/autopilot."}

    try:
        if tool_name == "get_accounts_and_pnl_summary":
            return accounts_service.get_accounts_summary().model_dump()

        if tool_name == "record_gym_expense":
            req = ExpenseCreateRequest(**arguments)
            return {"expense": accounts_service.record_expense(req, recorded_by=f"AI_Agent({actor_role})")}

        if tool_name == "lookup_member_360":
            query = scoped_member_id or actor_phone or str(arguments.get("query") or "")
            mid = _resolve_member_id(query)
            if not mid:
                return {"error": f"No member found matching '{query}'."}
            return membership_service.get_member_360(mid) or {"error": "Member not found."}

        if tool_name == "register_new_member":
            req = MemberCreateRequest(
                name=arguments["name"],
                phone=arguments["phone"],
                plan=arguments["plan"],
                fee=float(arguments["fee"]),
                start_date=arguments.get("start_date") or today_ist_str(),
                payment_status=arguments.get("payment_status", "Paid"),
                sync_to_n8n=False,
            )
            member, receipt = membership_service.register_member(req)
            return {
                "member": member.model_dump(),
                "google_sheets_row": membership_service.member_to_sheets_row(member),
                "payment_receipt": receipt,
            }

        if tool_name == "record_member_payment":
            mid = _resolve_member_id(str(arguments.get("member_id") or ""))
            if not mid:
                return {"error": "Member not found for payment."}
            req = PaymentRecordRequest(
                member_id=mid,
                amount_paid=float(arguments["amount_paid"]),
                payment_mode=arguments.get("payment_mode", "UPI"),
                extend_membership=True,
                notes="Processed by Crush AI Agent",
            )
            res = membership_service.record_payment(req)
            res["member"] = res["member"].model_dump()
            return res

        if tool_name == "freeze_member_plan":
            target = scoped_member_id or _resolve_member_id(str(arguments.get("member_id") or actor_phone))
            if not target:
                return {"error": "Member not found to freeze."}
            req = FreezeMembershipRequest(
                freeze_days=int(arguments["freeze_days"]),
                reason=str(arguments.get("reason") or "Requested via AI Agent"),
            )
            res = membership_service.freeze_membership(target, req)
            res["member"] = res["member"].model_dump()
            return res

        if tool_name == "unfreeze_member_plan":
            target = scoped_member_id or _resolve_member_id(str(arguments.get("member_id") or actor_phone))
            if not target:
                return {"error": "Member not found to unfreeze."}
            res = membership_service.unfreeze_membership(
                target,
                reason=str(arguments.get("reason") or "Resumed via AI Agent"),
            )
            res["member"] = res["member"].model_dump()
            return res

        if tool_name == "checkin_member_attendance":
            req = AttendanceCheckInRequest(
                member_id=scoped_member_id or arguments.get("member_id"),
                phone=actor_phone or arguments.get("phone"),
                workout_type=arguments.get("workout_type", "Gym Floor"),
            )
            return membership_service.record_checkin(req)

        if tool_name == "get_streak_leaderboard":
            limit = max(1, min(int(arguments.get("limit") or 10), 50))
            return {"leaderboard": membership_service.get_attendance_leaderboard(limit=limit)}

        if tool_name == "evaluate_and_dispatch_followups":
            due = membership_service.evaluate_follow_ups()
            if arguments.get("dispatch", True):
                today = today_ist_str()
                for item in due:
                    membership_service.record_followup_callback(
                        N8NFollowUpLogPayload(
                            member_id=item.member_id,
                            followup_type=item.followup_type,
                            channel=item.channel,
                            status="DISPATCHED_BY_AI_AGENT",
                            message=item.message_text,
                            last_reminder_date=today,
                        )
                    )
            return {"count": len(due), "follow_ups": [i.model_dump() for i in due]}

        if tool_name == "run_full_gym_autopilot":
            return run_full_gym_autopilot()

        return {"error": f"Unknown tool '{tool_name}'."}
    except Exception as exc:
        return {"error": str(exc)}


def _infer_plan_and_fee(text: str) -> tuple[str, float]:
    lower = text.lower()
    if "year" in lower or "annual" in lower:
        return "Yearly", 13500.0
    if "half" in lower or "6 month" in lower:
        return "Half-Yearly", 7500.0
    if "quarter" in lower or "3 month" in lower:
        return "Quarterly", 4000.0
    return "Monthly", 1500.0


def _infer_expense_category(text: str) -> str:
    lower = text.lower()
    if "rent" in lower:
        return "Rent"
    if "salary" in lower or "trainer" in lower or "payroll" in lower:
        return "Trainer Salaries"
    if "electric" in lower or "power" in lower or "water" in lower or "utility" in lower:
        return "Electricity & Utilities"
    if "equip" in lower or "repair" in lower or "treadmill" in lower:
        return "Equipment Maintenance"
    if "market" in lower or "ad" in lower or "instagram" in lower:
        return "Marketing"
    if "supplement" in lower or "protein" in lower:
        return "Supplements & Inventory"
    if "software" in lower or "saas" in lower or "railway" in lower or "n8n" in lower:
        return "Software & SaaS"
    return "Other"


def _deterministic_react_plan(req: AgentChatRequest) -> tuple[str, list[tuple[str, dict[str, Any]]]]:
    """Deterministic ReAct intent & entity router when no external LLM key is active."""
    msg = req.message.strip()
    lower = msg.lower()
    mem_match = re.search(r"\bMEM-[A-Z0-9]{8}\b", msg, re.IGNORECASE)
    phone_match = re.search(r"(?<!\d)(?:\+?91[\s-]?)?[6-9](?:[\s-]?\d){9}(?!\d)", msg)
    amt_match = re.search(r"(?:rs\.?|inr|₹)\s*(\d+(?:\.\d+)?)|\b(\d{3,6})\b", msg, re.IGNORECASE)
    extracted_amount = float(amt_match.group(1) or amt_match.group(2)) if amt_match else None

    if any(k in lower for k in ("autopilot", "daily cycle", "run full", "nightly", "morning sweep")):
        return "AUTOPILOT", [("run_full_gym_autopilot", {})]

    if any(k in lower for k in ("expense", "spent", "paid rent", "electricity", "salary", "utilities")):
        if extracted_amount and extracted_amount > 0:
            return "RECORD_EXPENSE", [(
                "record_gym_expense",
                {
                    "category": _infer_expense_category(msg),
                    "amount": extracted_amount,
                    "notes": msg,
                    "payment_mode": "UPI",
                },
            ), ("get_accounts_and_pnl_summary", {})]
        return "ACCOUNTS_PNL", [("get_accounts_and_pnl_summary", {})]

    if any(k in lower for k in ("account", "p&l", "profit", "gst", "revenue", "ledger", "receivable", "cfo", "finance")):
        return "ACCOUNTS_PNL", [("get_accounts_and_pnl_summary", {})]

    if any(k in lower for k in ("follow-up", "followup", "reminder", "retention", "due", "churn", "winback")):
        return "FOLLOWUP_SWEEP", [("evaluate_and_dispatch_followups", {"dispatch": True})]

    if any(k in lower for k in ("unfreeze", "resume")):
        target = req.member_id or (mem_match.group(0).upper() if mem_match else "") or req.actor_phone
        return "UNFREEZE_PLAN", [("unfreeze_member_plan", {"member_id": target, "reason": msg})]

    if any(k in lower for k in ("freeze", "pause", "vacation", "hold")):
        days_match = re.search(r"(\d+)\s*day", lower)
        freeze_days = int(days_match.group(1)) if days_match else 7
        target = req.member_id or (mem_match.group(0).upper() if mem_match else "") or req.actor_phone
        return "FREEZE_PLAN", [("freeze_member_plan", {"member_id": target, "freeze_days": freeze_days, "reason": msg})]

    if any(k in lower for k in ("leaderboard", "top streak", "ranking", "rankings")):
        return "LEADERBOARD", [("get_streak_leaderboard", {"limit": 10})]

    if any(k in lower for k in ("check-in", "checkin", "attendance", "streak")):
        return "CHECKIN", [(
            "checkin_member_attendance",
            {
                "member_id": req.member_id or (mem_match.group(0).upper() if mem_match else None),
                "phone": (phone_match.group(0) if phone_match else None) or req.actor_phone or None,
            },
        )]

    if any(k in lower for k in ("register", "add member", "new member", "join", "enroll")) and phone_match:
        plan, default_fee = _infer_plan_and_fee(msg)
        name_part = re.sub(r"^(?:register|add member|new member|enroll)\s+", "", msg, flags=re.IGNORECASE)
        name_clean = re.split(r"[,|\-]|\b(?:phone|mobile|\+?91|monthly|quarterly|yearly|half)\b", name_part, flags=re.IGNORECASE)[0].strip()
        return "REGISTER_MEMBER", [(
            "register_new_member",
            {
                "name": name_clean or "Walk-in Member",
                "phone": phone_match.group(0),
                "plan": plan,
                "fee": extracted_amount if extracted_amount is not None else default_fee,
                "payment_status": "Pending" if "pending" in lower else "Paid",
            },
        )]

    if any(k in lower for k in ("payment", "paid", "renew", "settle")):
        target = req.member_id or (mem_match.group(0).upper() if mem_match else "") or (phone_match.group(0) if phone_match else "")
        return "RECORD_PAYMENT", [(
            "record_member_payment",
            {"member_id": target, "amount_paid": extracted_amount or 1500.0, "payment_mode": "UPI"},
        )]

    if mem_match or phone_match or req.member_id:
        q = req.member_id or (mem_match.group(0).upper() if mem_match else phone_match.group(0))
        return "MEMBER_LOOKUP", [("lookup_member_360", {"query": q})]

    return "AUTOPILOT", [("run_full_gym_autopilot", {})]


TOOL_TO_INTENT: dict[str, str] = {
    "register_new_member": "REGISTER_MEMBER",
    "record_gym_expense": "RECORD_EXPENSE",
    "get_accounts_and_pnl_summary": "ACCOUNTS_PNL",
    "evaluate_and_dispatch_followups": "FOLLOWUP_SWEEP",
    "freeze_member_plan": "FREEZE_PLAN",
    "unfreeze_member_plan": "UNFREEZE_PLAN",
    "get_streak_leaderboard": "LEADERBOARD",
    "checkin_member_attendance": "CHECKIN",
    "record_member_payment": "RECORD_PAYMENT",
    "lookup_member_360": "MEMBER_LOOKUP",
    "run_full_gym_autopilot": "AUTOPILOT",
}


async def _try_llm_tool_plan(req: AgentChatRequest) -> tuple[str, list[AgentToolExecution], str] | None:
    """Attempt live OpenAI-compatible function calling if LLM_API_KEY is configured."""
    settings = get_settings()
    if not settings.llm_api_key.strip():
        return None

    system_prompt = (
        "You are the Autonomous AI Gym Manager & CFO for Crush Lifestyle Fitness Studio (Asia/Kolkata). "
        "Always call tools to read or mutate members, attendance, follow-ups, or accounting ledgers before answering. "
        f"Current caller role: {req.actor_role}. Today in IST: {today_ist_str()}."
    )
    allowed = [t for t in AGENT_TOOL_SCHEMAS if req.actor_role != "member" or t["function"]["name"] in MEMBER_ALLOWED_TOOLS]
    headers = {"Authorization": f"Bearer {settings.llm_api_key.strip()}", "Content-Type": "application/json"}
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": req.message},
    ]

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{settings.llm_base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json={"model": settings.llm_model, "messages": messages, "tools": allowed, "tool_choice": "auto"},
            )
            if resp.status_code >= 300:
                return None
            msg = resp.json()["choices"][0]["message"]
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                return ("LLM_DIRECT", [], msg.get("content") or "Done.")

            messages.append(msg)
            executions: list[AgentToolExecution] = []
            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                fn_args = json.loads(tc["function"].get("arguments") or "{}")
                out = _execute_tool(fn_name, fn_args, req.actor_role, req.actor_phone, req.member_id)
                executions.append(AgentToolExecution(tool_name=fn_name, arguments=fn_args, result=out))
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(out, default=str)})

            primary_tool = next(
                (ex.tool_name for ex in executions if ex.tool_name != "lookup_member_360"),
                executions[0].tool_name if executions else "",
            )
            semantic_intent = TOOL_TO_INTENT.get(primary_tool, "LLM_TOOL_CALL")

            final_resp = await client.post(
                f"{settings.llm_base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json={"model": settings.llm_model, "messages": messages},
            )
            reply_text = (
                final_resp.json()["choices"][0]["message"]["content"]
                if final_resp.status_code < 300
                else "Tools executed successfully."
            )
            return (semantic_intent, executions, reply_text)
    except Exception:
        return None


def _format_deterministic_reply(intent: str, executions: list[AgentToolExecution]) -> str:
    """Produce a concise, actionable executive summary from deterministic tool outputs."""
    if not executions:
        return "No actions were triggered."
    first_res = executions[0].result
    if "error" in first_res:
        return f"Action blocked: {first_res['error']}"

    if intent == "AUTOPILOT":
        kpis = first_res["operations_kpis"]
        acc = first_res["accounts_summary"]
        return (
            f"Autopilot completed ({first_res['as_of_ist']}): "
            f"{kpis['active_members']} Active, {kpis['expiring_in_7_days']} Expiring <=7d, "
            f"{len(first_res['status_transitions'])} status update(s), {first_res['followups_queued_count']} follow-up(s) queued. "
            f"Accounts: Gross Rs.{acc['gross_collections']}, GST Rs.{acc['total_gst_collected']}, "
            f"Expenses Rs.{acc['total_expenses']}, Net Profit Rs.{acc['net_profit']}."
        )
    if intent in ("ACCOUNTS_PNL", "RECORD_EXPENSE"):
        acc = executions[-1].result
        prefix = ""
        if intent == "RECORD_EXPENSE":
            exp = executions[0].result["expense"]
            prefix = f"Recorded expense {exp['expense_id']} ({exp['category']} Rs.{exp['amount']}). "
        return (
            f"{prefix}CFO Ledger: Gross Collections Rs.{acc['gross_collections']} "
            f"(CGST Rs.{acc['cgst_collected']} + SGST Rs.{acc['sgst_collected']}), "
            f"Net Revenue Rs.{acc['net_revenue_ex_gst']}, Expenses Rs.{acc['total_expenses']}, "
            f"Net Profit Rs.{acc['net_profit']} ({acc['profit_margin_pct']}% margin), "
            f"Pending Receivables Rs.{acc['pending_receivables_amount']} ({acc['pending_receivables_count']} members)."
        )
    if intent == "FOLLOWUP_SWEEP":
        return f"Evaluated and logged {first_res['count']} prioritized retention follow-up(s)."
    if intent == "REGISTER_MEMBER":
        m = first_res["member"]
        return f"Registered {m['name']} ({m['member_id']}) on {m['plan']} plan until {m['expiry_date']} ({m['payment_status']})."
    if intent == "RECORD_PAYMENT":
        m = first_res["member"]
        p = first_res["payment"]
        return f"Confirmed {p['invoice_number']} for {m['name']} ({m['member_id']}). New expiry: {m['expiry_date']}."
    if intent == "FREEZE_PLAN":
        return f"Membership frozen until {first_res['frozen_until']}. Extended expiry: {first_res['new_expiry']}."
    if intent == "UNFREEZE_PLAN":
        return (
            f"Membership unfrozen for {first_res['member_id']} "
            f"(reclaimed {first_res['reclaimed_freeze_days']} freeze day(s)). "
            f"Adjusted expiry: {first_res['new_expiry']}."
        )
    if intent == "LEADERBOARD":
        rows = first_res.get("leaderboard") or []
        if not rows:
            return "No member streaks recorded yet."
        top = ", ".join(f"#{r['rank']} {r['name']} ({r['current_streak']}d streak)" for r in rows[:5])
        return f"Top workout streaks: {top}."
    if intent == "CHECKIN":
        return f"Check-in recorded for {first_res['member_name']}! Current workout streak: {first_res['current_streak']} day(s)."
    if intent == "MEMBER_LOOKUP":
        m = first_res["member"]
        return f"{m['name']} ({m['member_id']}): {m['plan']} | Status: {m['member_status']} | Expiry: {m['expiry_date']} | Payment: {m['payment_status']} | Streak: {m['current_streak']}d."
    return json.dumps(first_res, default=str)


async def run_agent_turn(req: AgentChatRequest) -> AgentChatResponse:
    """Execute one complete AI Gym Manager turn, persist audit trail, and sync n8n."""
    llm_result = await _try_llm_tool_plan(req)
    if llm_result is not None:
        intent, executions, reply = llm_result
    else:
        intent, plan = _deterministic_react_plan(req)
        executions = []
        for tool_name, args in plan:
            out = _execute_tool(tool_name, args, req.actor_role, req.actor_phone, req.member_id)
            executions.append(AgentToolExecution(tool_name=tool_name, arguments=args, result=out))
        reply = _format_deterministic_reply(intent, executions)

    sheets_sync: dict[str, Any] = {"members_rows": [], "automation_log": {}}
    for ex in executions:
        if "google_sheets_row" in ex.result:
            sheets_sync["members_rows"].append(ex.result["google_sheets_row"])

    run_id = f"AGT-{secrets.token_hex(4).upper()}"
    now_str = timestamp_ist_str()
    sheets_sync["automation_log"] = {
        "log_id": run_id,
        "timestamp": now_str,
        "workflow": "GM-05",
        "member_id": req.member_id or "STUDIO",
        "action": intent,
        "status": "COMPLETED",
        "details": reply[:450],
    }

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_runs (
                run_id, actor_role, actor_phone, prompt, intent,
                tools_called_json, reply, sheets_sync_json, created_at_ist
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                req.actor_role,
                req.actor_phone,
                req.message,
                intent,
                json.dumps([e.model_dump() for e in executions], default=str),
                reply,
                json.dumps(sheets_sync, default=str),
                now_str,
            ),
        )

    n8n_dispatch: dict[str, Any] = {"dispatched": False, "status": "SKIPPED"}
    if req.sync_to_n8n:
        settings = get_settings()
        n8n_dispatch = await dispatch_to_n8n(
            webhook_url=settings.n8n_gm05_agent_webhook_url,
            workflow="GM-05",
            event_type=intent,
            member_id=req.member_id or "STUDIO",
            payload={
                "run_id": run_id,
                "intent": intent,
                "reply": reply,
                "google_sheets_sync": sheets_sync,
            },
        )
    else:
        log_automation_event(
            workflow="GM-05",
            event_type=intent,
            member_id=req.member_id or "STUDIO",
            status="LOCAL_ONLY",
            payload={"run_id": run_id, "intent": intent},
            details=reply[:300],
        )

    return AgentChatResponse(
        run_id=run_id,
        intent=intent,
        reply=reply,
        tools_executed=executions,
        google_sheets_sync=sheets_sync,
        n8n_dispatch=n8n_dispatch,
        timestamp_ist=now_str,
    )
