"""Payment ledger, renewal, and Razorpay webhook endpoints."""

from __future__ import annotations

import json

from fastapi import APIRouter, Header, HTTPException, Request

from ..config import get_settings
from ..database import get_db
from ..schemas import PaymentRecordRequest, PaymentRefundRequest
from ..services import membership_service
from ..services.n8n_client import dispatch_to_n8n, verify_hmac_signature

router = APIRouter(prefix="/api/v1/payments", tags=["Payments"])


@router.get("/{payment_id}/invoice")
def get_gst_tax_invoice(payment_id: str):
    """Generate a structured Indian GST Tax Invoice (CGST + SGST) for a payment."""
    try:
        return membership_service.get_payment_invoice(payment_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{payment_id}/refund")
def refund_member_payment(payment_id: str, payload: PaymentRefundRequest):
    """Mark a confirmed payment transaction as Refunded."""
    try:
        payment = membership_service.refund_payment(payment_id, reason=payload.reason)
        return {"message": "Payment marked as refunded.", "payment": payment}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("")
async def record_payment_and_renew(payload: PaymentRecordRequest):
    """Record a payment, auto-extend expiry if renewing, generate GST invoice, and notify GM-03."""
    try:
        result = membership_service.record_payment(payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    settings = get_settings()
    if not result.get("already_processed"):
        result["n8n_sync"] = await dispatch_to_n8n(
            webhook_url=settings.n8n_gm03_payment_webhook_url,
            workflow="GM-03",
            event_type="PAYMENT_RECORDED",
            member_id=payload.member_id,
            payload={
                "member_id": payload.member_id,
                "amount_paid": payload.amount_paid,
                "payment_mode": payload.payment_mode,
                "transaction_ref": result["payment"]["transaction_ref"],
                "plan": result["member"].plan,
                "source": "fastapi_backend",
            },
        )
    return result


@router.post("/webhook/razorpay")
async def handle_razorpay_webhook(
    request: Request,
    x_razorpay_signature: str = Header(default=""),
):
    """Verify Razorpay webhook signature and settle membership renewal idempotently."""
    raw_bytes = await request.body()
    settings = get_settings()
    if settings.razorpay_webhook_secret and not verify_hmac_signature(
        raw_bytes, x_razorpay_signature, settings.razorpay_webhook_secret
    ):
        raise HTTPException(status_code=401, detail="Invalid Razorpay webhook signature.")

    body = json.loads(raw_bytes.decode("utf-8") or "{}")
    if body.get("event") != "payment_link.paid":
        return {"ignored": True, "event": body.get("event")}

    entity = ((body.get("payload") or {}).get("payment_link") or {}).get("entity") or {}
    member_id = str(entity.get("reference_id") or "").strip()
    amount_paid = float(entity.get("amount_paid") or entity.get("amount") or 0) / 100.0
    tx_ref = str(entity.get("id") or "").strip()
    if not member_id or amount_paid <= 0:
        raise HTTPException(status_code=400, detail="Malformed Razorpay payment_link.paid entity.")

    req = PaymentRecordRequest(
        member_id=member_id,
        amount_paid=amount_paid,
        payment_mode="Razorpay",
        transaction_ref=tx_ref,
        extend_membership=True,
        notes="Razorpay payment_link.paid webhook",
    )
    try:
        return membership_service.record_payment(req)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("")
def list_payments(member_id: str | None = None):
    """List recent payment transactions and GST invoices."""
    with get_db() as conn:
        if member_id:
            rows = conn.execute(
                "SELECT * FROM payments WHERE member_id = ? ORDER BY created_at_ist DESC",
                (member_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM payments ORDER BY created_at_ist DESC LIMIT 100"
            ).fetchall()
    return {"count": len(rows), "payments": [dict(r) for r in rows]}
