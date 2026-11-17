"""Gym Accounts, GST ledger, expense tracking, and receivables aging service."""

from __future__ import annotations

import secrets
from typing import Any
from urllib.parse import quote

from ..config import timestamp_ist_str, today_ist_str
from ..database import get_db
from ..schemas import AccountsSummaryResponse, ExpenseCreateRequest
from .membership_service import days_between_iso


def record_expense(req: ExpenseCreateRequest, recorded_by: str = "AI_Agent") -> dict[str, Any]:
    """Persist an operating expense into the studio accounts ledger."""
    exp_date = req.expense_date or today_ist_str()
    expense_id = f"EXP-{secrets.token_hex(4).upper()}"
    now_str = timestamp_ist_str()

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO expenses (
                expense_id, category, amount, vendor, payment_mode,
                expense_date, notes, recorded_by, created_at_ist
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                expense_id,
                req.category,
                req.amount,
                req.vendor.strip(),
                req.payment_mode,
                exp_date,
                req.notes.strip(),
                recorded_by,
                now_str,
            ),
        )
        row = conn.execute("SELECT * FROM expenses WHERE expense_id = ?", (expense_id,)).fetchone()
    return dict(row)


def list_expenses(limit: int = 100) -> list[dict[str, Any]]:
    """Return recent operating expenses ordered newest first."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM expenses ORDER BY expense_date DESC, created_at_ist DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_accounts_summary() -> AccountsSummaryResponse:
    """Compute complete studio P&L, CGST/SGST liability, and receivables aging."""
    today = today_ist_str()
    with get_db() as conn:
        payments = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM payments WHERE status = 'Confirmed' ORDER BY created_at_ist DESC"
            ).fetchall()
        ]
        expenses = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM expenses ORDER BY expense_date DESC, created_at_ist DESC"
            ).fetchall()
        ]
        pending_members = [
            dict(r)
            for r in conn.execute(
                "SELECT member_id, name, phone, plan, fee, start_date FROM members WHERE payment_status = 'Pending'"
            ).fetchall()
        ]

    gross = round(sum(float(p["amount_paid"] or 0) for p in payments), 2)
    total_gst = round(sum(float(p["gst_amount"] or 0) for p in payments), 2)
    cgst = round(total_gst / 2.0, 2)
    sgst = round(total_gst - cgst, 2)
    net_rev = round(gross - total_gst, 2)
    collections_today = round(
        sum(float(p["amount_paid"] or 0) for p in payments if p["payment_date"] == today),
        2,
    )

    revenue_by_plan: dict[str, float] = {}
    for p in payments:
        plan = p["plan"]
        revenue_by_plan[plan] = round(revenue_by_plan.get(plan, 0.0) + float(p["amount_paid"] or 0), 2)

    total_expenses = round(sum(float(e["amount"] or 0) for e in expenses), 2)
    expenses_today = round(
        sum(float(e["amount"] or 0) for e in expenses if e["expense_date"] == today),
        2,
    )
    expenses_by_category: dict[str, float] = {}
    for e in expenses:
        cat = e["category"]
        expenses_by_category[cat] = round(expenses_by_category.get(cat, 0.0) + float(e["amount"] or 0), 2)

    net_profit = round(net_rev - total_expenses, 2)
    profit_margin_pct = round((net_profit / net_rev) * 100.0, 2) if net_rev > 0 else 0.0

    aging = {"0_7_days": 0.0, "8_30_days": 0.0, "over_30_days": 0.0}
    pending_total = 0.0
    for m in pending_members:
        amt = float(m["fee"] or 0)
        pending_total += amt
        age = max(0, days_between_iso(today, m["start_date"]))
        if age <= 7:
            aging["0_7_days"] = round(aging["0_7_days"] + amt, 2)
        elif age <= 30:
            aging["8_30_days"] = round(aging["8_30_days"] + amt, 2)
        else:
            aging["over_30_days"] = round(aging["over_30_days"] + amt, 2)

    return AccountsSummaryResponse(
        as_of_ist=timestamp_ist_str(),
        gross_collections=gross,
        total_gst_collected=total_gst,
        cgst_collected=cgst,
        sgst_collected=sgst,
        net_revenue_ex_gst=net_rev,
        total_expenses=total_expenses,
        net_profit=net_profit,
        profit_margin_pct=profit_margin_pct,
        collections_today=collections_today,
        expenses_today=expenses_today,
        pending_receivables_amount=round(pending_total, 2),
        pending_receivables_count=len(pending_members),
        receivables_aging=aging,
        revenue_by_plan=revenue_by_plan,
        expenses_by_category=expenses_by_category,
        recent_payments=payments[:15],
        recent_expenses=expenses[:15],
    )


def delete_expense(expense_id: str) -> dict[str, Any]:
    """Delete an operating expense entry by ID."""
    with get_db() as conn:
        existing = conn.execute(
            "SELECT expense_id FROM expenses WHERE expense_id = ?",
            (expense_id.strip(),),
        ).fetchone()
        if not existing:
            raise KeyError(f"Expense {expense_id} not found.")
        conn.execute("DELETE FROM expenses WHERE expense_id = ?", (expense_id.strip(),))
    return {"deleted": True, "expense_id": expense_id.strip()}


def list_pending_receivables() -> dict[str, Any]:
    """Return detailed aging and WhatsApp recovery details for pending receivables."""
    today = today_ist_str()
    with get_db() as conn:
        pending_members = [
            dict(r)
            for r in conn.execute(
                """
                SELECT member_id, name, phone, email, plan, fee, start_date, expiry_date, last_reminder_date
                FROM members
                WHERE payment_status = 'Pending'
                ORDER BY start_date ASC
                """
            ).fetchall()
        ]

    aging_totals = {"0_7_days": 0.0, "8_30_days": 0.0, "over_30_days": 0.0}
    items: list[dict[str, Any]] = []
    total_pending = 0.0

    for m in pending_members:
        amount_due = round(float(m["fee"] or 0), 2)
        total_pending += amount_due
        days_overdue = max(0, days_between_iso(today, m["start_date"]))
        if days_overdue <= 7:
            bucket = "0_7_days"
        elif days_overdue <= 30:
            bucket = "8_30_days"
        else:
            bucket = "over_30_days"
        aging_totals[bucket] = round(aging_totals[bucket] + amount_due, 2)

        reminder_msg = (
            f"Hi {m['name']}, friendly reminder from Crush Lifestyle Fitness Studio: "
            f"your {m['plan']} membership fee of Rs.{int(amount_due)} ({m['member_id']}) is pending. "
            f"Please settle via UPI or reception desk."
        )
        member_email = (m.get("email") or "").strip()
        mailto_url = (
            f"mailto:{member_email}?subject={quote('Crush Lifestyle Fitness Studio Fee Reminder')}&body={quote(reminder_msg)}"
            if member_email
            else ""
        )
        items.append(
            {
                "member_id": m["member_id"],
                "name": m["name"],
                "phone": m["phone"],
                "email": member_email,
                "plan": m["plan"],
                "start_date": m["start_date"],
                "expiry_date": m["expiry_date"],
                "last_reminder_date": m["last_reminder_date"],
                "amount_due": amount_due,
                "days_overdue": days_overdue,
                "aging_bucket": bucket,
                "message": reminder_msg,
                "mailto_reminder_url": mailto_url,
                "whatsapp_reminder_url": f"https://wa.me/{m['phone']}?text={quote(reminder_msg)}",
            }
        )

    return {
        "as_of_ist": timestamp_ist_str(),
        "count": len(items),
        "total_pending_amount": round(total_pending, 2),
        "aging_totals": aging_totals,
        "receivables": items,
    }
