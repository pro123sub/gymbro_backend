"""Gym CFO Accounts, GST ledger, expenses, and receivables aging endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from ..schemas import AccountsSummaryResponse, ExpenseCreateRequest
from ..services import accounts_service

router = APIRouter(prefix="/api/v1/accounts", tags=["Accounts & CFO"])


@router.get("/summary", response_model=AccountsSummaryResponse)
def get_accounts_summary():
    """Return studio P&L, CGST/SGST breakdown, expenses, and pending receivables aging."""
    return accounts_service.get_accounts_summary()


@router.get("/receivables")
def get_pending_receivables_detail():
    """Return itemized pending receivables aging buckets and WhatsApp recovery links."""
    return accounts_service.list_pending_receivables()


@router.post("/expenses", status_code=status.HTTP_201_CREATED)
def record_studio_expense(payload: ExpenseCreateRequest):
    """Record an operating expense in the gym accounts ledger."""
    expense = accounts_service.record_expense(payload, recorded_by="Staff_UI")
    return {"message": "Expense recorded successfully.", "expense": expense}


@router.get("/expenses")
def list_studio_expenses():
    """List recent gym operating expenses."""
    expenses = accounts_service.list_expenses()
    return {"count": len(expenses), "expenses": expenses}


@router.delete("/expenses/{expense_id}")
def remove_studio_expense(expense_id: str):
    """Delete an operating expense entry by expense_id."""
    try:
        return accounts_service.delete_expense(expense_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
